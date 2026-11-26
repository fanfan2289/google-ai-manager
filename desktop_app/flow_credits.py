"""Google Flow credit balance via a dedicated, per-account Microsoft Edge profile (Windows).

How it works (the read Flow's own web app makes, as documented by gflow-cli #795 / #1221):
  1. ``login``: open Edge with a private user-data-dir for that account at flow.google.com; the
     user signs in normally (a real browser window, so Google allows the sign-in).
  2. ``read``: start that profile headless with a local DevTools port, open flow.google.com and,
     *inside the page*, take the per-document ``at`` token (``WIZ_global_data.SNlM0e``) and call
     the read-only ``nzlxg`` RPC on ``/_/AiSandboxAngularFrontend/data/batchexecute``; ``[0]`` of
     its reply is the balance the account menu shows. (The older labs.google
     ``/fx/api/auth/session`` → ``aisandbox-pa /v1/credits`` path answers 410 since 2026-10-22.)

The token never leaves the browser page: the page script returns only the balance and an
``email_match`` flag (the expected e-mail is compared inside the page). Nothing is logged or stored
besides the browser profile itself, which lives under %LOCALAPPDATA%\\GoogleAIManager\\flow\\ (not
in the repository). Unofficial interface: it may stop working at any time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

FLOW_URL = "https://flow.google.com/"
FLOW_ORIGIN = "https://flow.google.com"
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
CREATE_NO_WINDOW = 0x08000000

# Runs inside the flow.google.com page. Returns the balance only; the `at` token stays in the page.
PAGE_SCRIPT = r"""
(async (expected) => {
  const out = { ok: false, stage: "shell" };
  const wiz = window.WIZ_global_data || {};
  const at = wiz.SNlM0e;
  out.signed_in = Boolean(at);
  if (!at) return out;
  const pageEmail = String(wiz.oPEP7c || "").toLowerCase();
  out.email_match = expected && pageEmail ? pageEmail === String(expected).toLowerCase() : null;
  out.stage = "rpc";
  const q = new URLSearchParams({ rpcids: "nzlxg", "source-path": "/", hl: "en",
                                  _reqid: String(100000 + Math.floor(Math.random() * 90000)), rt: "c" });
  if (wiz.cfb2h) q.set("bl", wiz.cfb2h);
  if (wiz.FdrFJe) q.set("f.sid", wiz.FdrFJe);
  const body = new URLSearchParams({ "f.req": JSON.stringify([[["nzlxg", "[]", null, "generic"]]]), at });
  const r = await fetch("/_/AiSandboxAngularFrontend/data/batchexecute?" + q.toString(), {
    method: "POST", credentials: "include", cache: "no-store",
    headers: { "content-type": "application/x-www-form-urlencoded;charset=UTF-8", "x-same-domain": "1" },
    body: body.toString() });
  out.status = r.status;
  if (r.status !== 200) return out;
  const text = await r.text();
  const rows = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("[")) continue;
    try {
      const v = JSON.parse(t);
      if (Array.isArray(v)) for (const item of v) if (Array.isArray(item)) rows.push(item);
    } catch (e) {}
  }
  const frame = rows.find(x => x[0] === "wrb.fr" && x[1] === "nzlxg");
  if (!frame) { out.error = "no_frame"; return out; }
  if (typeof frame[2] !== "string") { out.error = "rpc_error"; return out; }
  let payload = null;
  try { payload = JSON.parse(frame[2]); } catch (e) { out.error = "bad_payload"; return out; }
  out.ok = true;
  out.credits = Array.isArray(payload) ? payload[0] : null;
  return out;
})
"""


class FlowError(RuntimeError):
    pass


def check_account(account: Any) -> str:
    account = str(account or "").strip()
    if not ACCOUNT_RE.match(account):
        raise FlowError("账号名非法")
    return account


def profiles_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "GoogleAIManager" / "flow"


def profile_dir(account: str) -> Path:
    return profiles_root() / check_account(account)


def is_connected(account: str) -> bool:
    return (profile_dir(account) / "Local State").is_file()


def find_browser() -> str:
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FlowError("未找到 Microsoft Edge 或 Google Chrome")


def _common_flags(account: str) -> list[str]:
    d = profile_dir(account)
    d.mkdir(parents=True, exist_ok=True)
    return [f"--user-data-dir={d}", "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-background-mode"]


def _profile_pids(d: Path) -> list[int]:
    """PIDs of browser processes started on this dedicated profile (never the user's own browser)."""
    needle = str(d).lower()
    script = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
                          encoding="utf-8", errors="ignore")
    pids = []
    for line in proc.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and f"--user-data-dir={needle}" in cmd.lower() and "--type=" not in cmd:
            pids.append(int(pid))
    return pids


def close_profile_browser(account: str) -> int:
    """Close a browser still running on the account's dedicated profile (e.g. Edge background mode)."""
    d = profile_dir(account)
    pids = _profile_pids(d)
    for pid in pids:  # graceful first, so the browser flushes its cookie store to disk
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], creationflags=CREATE_NO_WINDOW,
                       capture_output=True, check=False)
    deadline = time.time() + 8
    while pids and time.time() < deadline and _profile_pids(d):
        time.sleep(1.0)
    for pid in _profile_pids(d) if pids else []:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                       capture_output=True, check=False)
    if pids:
        time.sleep(1.0)
    return len(pids)


def open_login(account: str) -> dict[str, Any]:
    """Open a normal browser window on the account's dedicated profile so the user can sign in."""
    subprocess.Popen([find_browser(), *_common_flags(account), "--new-window", FLOW_URL])
    return {"ok": True}


def disconnect(account: str) -> dict[str, Any]:
    """Delete the account's dedicated browser profile (its Flow / Google sign-in)."""
    d = profile_dir(account)
    if d.exists():
        shutil.rmtree(d)
    return {"ok": True}


def normalize(raw: Any) -> dict[str, Any]:
    """Validate the page result: numbers / short strings only, friendly error otherwise."""
    if not isinstance(raw, dict):
        return {"ok": False, "error": "页面没有返回结果"}

    def num(key: str) -> int | None:
        v = raw.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    def short(key: str) -> str | None:
        v = raw.get(key)
        return v[:40] if isinstance(v, str) and v else None

    status = raw.get("status")
    if raw.get("email_match") is False:
        return {"ok": False, "error": "Flow 登录的 Google 账号与本账号不一致，请重新连接并登录正确的账号"}
    if raw.get("ok") and num("credits") is not None:
        return {"ok": True, "credits": num("credits"), "email_verified": raw.get("email_match") is True}
    if raw.get("stage") == "shell" or raw.get("signed_in") is False:
        return {"ok": False, "error": "未登录 Flow，请点「连接 Flow」并在弹出的窗口中登录"}
    if status in (401, 403):
        return {"ok": False, "error": f"Flow 拒绝访问（HTTP {status}），请重新连接"}
    if raw.get("ok") or raw.get("error") in ("no_frame", "bad_payload"):
        return {"ok": False, "error": "积分接口返回格式已变化"}
    if raw.get("error") == "rpc_error":
        return {"ok": False, "error": "Flow 拒绝了这次查询（可能被限流），稍后再试"}
    return {"ok": False, "error": f"Flow 积分接口暂时不可用（HTTP {status}）"}


class _Cdp:
    def __init__(self, ws_url: str) -> None:
        import websocket  # websocket-client

        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self._id += 1
        my_id = self._id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise FlowError(f"DevTools {method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
        raise FlowError(f"DevTools {method} 超时")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _wait_port(d: Path, proc: subprocess.Popen, timeout: float = 20.0) -> int:
    port_file = d / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_file.is_file():
            first = port_file.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
            if first and first[0].strip().isdigit():
                return int(first[0].strip())
        if proc.poll() is not None and not port_file.is_file():
            break
        time.sleep(0.25)
    raise FlowError("浏览器没有启动调试端口：如果 Flow 登录窗口还开着，请先关闭它再刷新")


def read_credits(account: str, expected_email: str | None = None, *, headless: bool = True) -> dict[str, Any]:
    """Read the Flow balance for `account` through its dedicated browser profile."""
    account = check_account(account)
    if not is_connected(account):
        return {"ok": False, "connected": False, "error": "尚未连接 Flow"}
    d = profile_dir(account)
    close_profile_browser(account)  # a running instance on this profile would swallow our launch
    (d / "DevToolsActivePort").unlink(missing_ok=True)
    flags = [*_common_flags(account), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
             "--disable-extensions", "--mute-audio", "--disable-background-networking"]
    flags += ["--headless=new"] if headless else ["--window-position=-32000,-32000", "--window-size=800,600"]
    proc = subprocess.Popen([find_browser(), *flags, "about:blank"], creationflags=CREATE_NO_WINDOW,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        port = _wait_port(d, proc)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if page is None:
            raise FlowError("浏览器没有可用页面")
        cdp = _Cdp(page["webSocketDebuggerUrl"])
        cdp.call("Page.navigate", {"url": FLOW_URL})
        deadline = time.time() + 40
        state = ""
        while time.time() < deadline:
            res = cdp.call("Runtime.evaluate", {
                "expression": "location.origin + '|' + document.readyState + '|' + Boolean(window.WIZ_global_data)",
                "returnByValue": True})
            state = str((res.get("result") or {}).get("value") or "")
            if state == f"{FLOW_ORIGIN}|complete|true":
                break
            if state.startswith("https://accounts.google.com|complete"):
                return {"ok": False, "connected": True, "error": "未登录 Flow，请点「连接 Flow」并在弹出的窗口中登录"}
            time.sleep(0.5)
        else:
            raise FlowError("打开 flow.google.com 超时（检查网络或代理）")
        res = cdp.call("Runtime.evaluate", {
            "expression": f"({PAGE_SCRIPT})({json.dumps(expected_email or '')})",
            "awaitPromise": True, "returnByValue": True,
        }, timeout=40)
        if res.get("exceptionDetails"):
            return {"ok": False, "connected": True, "error": "页面脚本执行失败（网络或接口变化）"}
        out = normalize((res.get("result") or {}).get("value"))
        return {**out, "connected": True}
    except FlowError as exc:
        return {"ok": False, "connected": True, "error": str(exc)}
    except Exception as exc:  # never include response bodies: only the error type
        return {"ok": False, "connected": True, "error": f"读取失败（{type(exc).__name__}）"}
    finally:
        if cdp is not None:
            try:
                cdp.call("Browser.close", timeout=5)
            except Exception:
                pass
            cdp.close()
        try:
            proc.wait(timeout=8)
        except Exception:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                           capture_output=True, check=False)


if __name__ == "__main__":  # manual test: python flow_credits.py login|read ACCOUNT [expected_email]
    if len(sys.argv) < 3 or sys.argv[1] not in ("login", "read", "read-visible"):
        print("usage: flow_credits.py login|read|read-visible ACCOUNT [expected_email]")
        sys.exit(2)
    acct = sys.argv[2]
    if sys.argv[1] == "login":
        print(open_login(acct))
    else:
        result = read_credits(acct, sys.argv[3] if len(sys.argv) > 3 else None,
                              headless=sys.argv[1] == "read")
        print(json.dumps(result, ensure_ascii=False))
