#!/usr/bin/env python3
"""One-click Colab sign-in for an existing colab-ext profile (loopback OAuth, like gcloud).

The colab CLI's own flow uses the Cloud SDK OAuth client with a copy-paste landing page. The
same client also accepts a loopback redirect (that is what `gcloud auth ... login` uses), so this
helper:

  1. builds the consent URL with redirect `http://localhost:<port>/` (PKCE, same scopes as the
     CLI) and stores colab-ext's normal pending-login file for the profile;
  2. prints one JSON line {"event": "url", "url": ..., "port": ...} and waits on 127.0.0.1:<port>
     for Google's redirect (state is checked; times out);
  3. hands the authorization code to the public CLI: `colab account login <profile> --code ...`,
     which exchanges it and stores the token exactly as the copy-paste flow does.

This script never prints or stores a token. If the browser cannot reach the callback, the user can
paste the address-bar URL (it contains ?code=) into the app, which finishes through the same
pending file. Output: JSON lines on stdout (`url`, then `done` or `error`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def _reexec_colab_venv() -> None:
    try:
        import colab_cli  # noqa: F401
        return
    except ImportError:
        py = Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python"
        if py.is_file() and os.path.realpath(py) != os.path.realpath(sys.executable):
            os.execv(str(py), [str(py), *sys.argv])
        raise


from providers import netcfg  # noqa: E402  (stdlib only)

netcfg.apply_proxy_env()
_reexec_colab_venv()

import colab_cli.cli  # noqa: E402, F401  (registers colab-ext hooks)
from colab_cli import auth  # noqa: E402
from colab_cli.common import state  # noqa: E402
from colab_ext import accounts, cli_hooks  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

SUCCESS_HTML = """<!doctype html><meta charset="utf-8"><title>授权成功</title>
<body style="font-family:system-ui;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:48px">✅</div><h2>授权成功</h2>
<p>Google AI Manager 正在完成登录，可以关闭此页面并回到应用。</p></div></body>"""
ERROR_HTML = """<!doctype html><meta charset="utf-8"><title>授权未完成</title>
<body style="font-family:system-ui;background:#0f172a;color:#e2e8f0;text-align:center;padding-top:20vh">
<h2>授权未完成</h2><p>{msg}</p><p>请回到 Google AI Manager 重试。</p></body>"""


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("--email", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    acct = args.account
    reg = accounts.load_registry()
    if acct not in (reg.get("accounts") or {}):
        emit("error", error=f"账号 {acct} 不存在")
        return 2

    srv = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)  # bind first: the port goes into the URL
    port = srv.server_address[1]
    redirect = f"http://localhost:{port}/"
    client_config = cli_hooks.load_colab_client_config(getattr(state, "client_oauth_config", None))
    flow = InstalledAppFlow.from_client_config(client_config, auth.PUBLIC_SCOPES)
    flow.redirect_uri = redirect
    kwargs = {"prompt": "consent select_account"}
    hint = args.email or ((reg.get("accounts") or {}).get(acct) or {}).get("email")
    if hint:
        kwargs["login_hint"] = hint
    url, oauth_state = flow.authorization_url(**kwargs)
    accounts._atomic_write_json(  # same pending-login file the CLI's --url-only step writes
        accounts.oauth_pending_path(acct),
        {
            "account": acct,
            "client_config": client_config,
            "scopes": list(auth.PUBLIC_SCOPES),
            "redirect_uri": redirect,
            "state": oauth_state,
            "code_verifier": getattr(flow, "code_verifier", None),
            "email_hint": hint,
            "created_at": accounts._now_iso(),
        },
        mode=0o600,
    )
    emit("url", url=url, port=port)

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # never log the query string (it carries the code)
            return

        def _page(self, status: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not q:
                self._page(404, ERROR_HTML.format(msg="无效的回调"))
                return
            if (q.get("state") or [""])[0] != oauth_state:
                self._page(400, ERROR_HTML.format(msg="state 不匹配（可能是旧的授权页面）"))
                return
            if q.get("error"):
                result["error"] = (q.get("error") or ["denied"])[0][:80]
                self._page(400, ERROR_HTML.format(msg="你在 Google 页面上取消了授权"))
                return
            code = (q.get("code") or [""])[0]
            if not code:
                self._page(400, ERROR_HTML.format(msg="回调里没有授权码"))
                return
            result["code"] = code
            self._page(200, SUCCESS_HTML)

    srv.RequestHandlerClass = Handler
    srv.timeout = 2
    deadline = time.time() + max(60, args.timeout)
    while not result and time.time() < deadline:
        srv.handle_request()
    srv.server_close()

    if "error" in result:
        emit("error", error=f"授权被取消（{result['error']}）")
        return 3
    if "code" not in result:
        emit("error", error="等待授权超时")
        return 4

    emit("finishing")
    colab_bin = str(Path.home() / ".local/bin/colab") if (Path.home() / ".local/bin/colab").is_file() else "colab"
    proc = subprocess.run([colab_bin, "account", "login", acct, "--code", result["code"]],
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        tail = " ".join(((proc.stderr or proc.stdout) or "").strip().splitlines()[-3:])[:300]
        emit("error", error=f"登录未完成：{tail or proc.returncode}")
        return 5
    emit("done", account=acct)
    return 0


if __name__ == "__main__":
    sys.exit(main())
