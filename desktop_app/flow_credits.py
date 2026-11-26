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


GCP_BENEFITS_URL = "https://me.developers.google.com/benefits"
GCP_BILLING_URL = "https://console.cloud.google.com/billing"
SIGNED_OUT = "未登录，请点「连接」并在弹出的窗口中登录"

# Developer Program benefits page: is the monthly Cloud credit waiting for a billing account?
GCP_BENEFITS_SCRIPT = r"""
(() => {
  const t = (document.body ? document.body.innerText : "").replace(/\s+/g, " ");
  const lower = t.toLowerCase();
  const out = { has_monthly_credit: /每月\s*\$\s*\d+|monthly\s*\$\s*\d+|\$\s*\d+\s*(?:monthly|per month)/i.test(t) };
  if (/set up a google cloud billing account|设置.{0,8}结算账号|建立.{0,8}结算帐户|创建.{0,8}结算账号/i.test(t)) out.state = "needs_billing";
  else if (/claimed|已申领|已领取|已兑换/i.test(t)) out.state = "claimed";
  else if (/\bclaim\b|申领|领取/i.test(lower)) out.state = "claimable";
  else out.state = "unknown";
  return out;
})()
"""

# The Cloud console renders most of its UI inside shadow roots: search those too.
DEEP_JS = r"""
const deepAll = (root, sel, out = []) => {
  root.querySelectorAll(sel).forEach(e => out.push(e));
  root.querySelectorAll("*").forEach(e => { if (e.shadowRoot) deepAll(e.shadowRoot, sel, out); });
  return out;
};
const deepText = () => [document.body, ...deepAll(document, "*").filter(e => e.shadowRoot).map(e => e.shadowRoot)]
  .map(r => r === document.body ? r.innerText : [...r.children].map(c => c.innerText || "").join("\n")).join("\n");
"""

# Billing overview: how many billing accounts can this login see (ids stay in the page)?
GCP_ACCOUNTS_SCRIPT = r"""
(() => {
  DEEP_JS
  const ids = new Set();
  for (const a of deepAll(document, "a[href]")) {
    const m = a.getAttribute("href").match(/\/billing\/([0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6})/i);
    if (m) ids.add(m[1].toUpperCase());
  }
  const t = deepText();
  return { ids: [...ids].slice(0, 3), trial_banner: /免费试用|free trial/i.test(t), text_len: t.length };
})()
""".replace("DEEP_JS", DEEP_JS)

# Credits page of one billing account: rows that carry money amounts. Returns numbers + coarse labels only.
GCP_CREDITS_SCRIPT = r"""
(() => {
  DEEP_JS
  const money = /(?:US\$|\$|€|£|¥|HK\$|NT\$)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|CNY|HKD|TWD)/g;
  const num = s => parseFloat(String(s).replace(/[^\d.]/g, ""));
  const cur = s => (String(s).match(/US\$|HK\$|NT\$|\$|€|£|¥|USD|EUR|CNY|HKD|TWD/) || ["$"])[0].replace("US$", "$");
  const rows = deepAll(document, "tr, [role='row']").map(r => r.innerText.replace(/\s+/g, " ").trim());
  const credits = [];
  const MON = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  const pad = n => String(n).padStart(2, "0");
  const dates = r => {
    const out = [];
    for (const m of r.matchAll(/(\d{4})[-\/.年](\d{1,2})[-\/.月](\d{1,2})日?/g)) out.push(`${m[1]}-${pad(m[2])}-${pad(m[3])}`);
    for (const m of r.matchAll(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}),? (\d{4})/gi))
      out.push(`${m[3]}-${pad(MON[m[1].toLowerCase().slice(0, 3)])}-${pad(m[2])}`);
    return out.sort();
  };
  for (const r of rows) {
    const amounts = r.match(money);
    if (!amounts || !amounts.length) continue;
    const kind = /free trial|免费试用|免費試用|试用赠金/i.test(r) ? "free_trial"
      : /developer program|开发者计划|開發人員計畫|google ai|gemini/i.test(r) ? "developer_program" : "other";
    const expired = /expired|已过期|已到期|used up|已用完/i.test(r);
    const ds = dates(r);
    credits.push({ label: kind === "developer_program" ? "developer_program" : "other", kind,
                   remaining: num(amounts[0]), original: amounts.length > 1 ? num(amounts[1]) : null,
                   currency: cur(amounts[0]), expired, expires: ds.length ? ds[ds.length - 1] : null });
  }
  const t = deepText();
  return { credits: credits.slice(0, 20), no_credits_text: /没有.{0,6}赠送|no credits|no promotional/i.test(t) };
})()
""".replace("DEEP_JS", DEEP_JS)


class _ProfilePage:
    """One headless browser launch on the account's dedicated profile; several page reads."""

    def __init__(self, account: str, headless: bool = True) -> None:
        self.account = account
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.cdp: _Cdp | None = None

    def __enter__(self) -> "_ProfilePage":
        d = profile_dir(self.account)
        close_profile_browser(self.account)  # a running instance on this profile would swallow our launch
        (d / "DevToolsActivePort").unlink(missing_ok=True)
        flags = [*_common_flags(self.account), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                 "--disable-extensions", "--mute-audio", "--disable-background-networking", "--window-size=1400,1000"]
        flags += ["--headless=new"] if self.headless else ["--window-position=-32000,-32000"]
        self.proc = subprocess.Popen([find_browser(), *flags, "about:blank"], creationflags=CREATE_NO_WINDOW,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port = _wait_port(d, self.proc)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            if page is None:
                raise FlowError("浏览器没有可用页面")
            self.cdp = _Cdp(page["webSocketDebuggerUrl"])
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.cdp is not None:
            try:
                self.cdp.call("Browser.close", timeout=5)
            except Exception:
                pass
            self.cdp.close()
        if self.proc is not None:
            try:
                self.proc.wait(timeout=8)
            except Exception:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                               capture_output=True, check=False)

    def evaluate(self, expression: str, timeout: float = 40.0) -> Any:
        res = self.cdp.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                 "returnByValue": True}, timeout=timeout)
        if res.get("exceptionDetails"):
            raise FlowError("页面脚本执行失败（网络或页面变化）")
        return (res.get("result") or {}).get("value")

    def goto(self, url: str, origin: str, ready_expr: str = "true", timeout: float = 40.0) -> str:
        """Navigate and wait for `origin` + readyState complete + `ready_expr`; returns 'ok' / 'signed_out'."""
        self.cdp.call("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Heavy apps (Cloud console) keep loading forever: "interactive" is good enough there.
            state = str(self.evaluate(
                "location.origin + '|' + (document.readyState === 'loading' ? 'loading' : 'ready') + '|' + "
                f"Boolean({ready_expr})", timeout=10) or "")
            if state == f"{origin}|ready|true":
                return "ok"
            if state.startswith("https://accounts.google.com|ready"):
                return "signed_out"
            time.sleep(0.5)
        raise FlowError(f"打开 {origin} 超时（检查网络或代理）")


def _read_flow(page: _ProfilePage, expected_email: str | None) -> dict[str, Any]:
    if page.goto(FLOW_URL, FLOW_ORIGIN, "window.WIZ_global_data") == "signed_out":
        return {"ok": False, "error": SIGNED_OUT}
    return normalize(page.evaluate(f"({PAGE_SCRIPT})({json.dumps(expected_email or '')})"))


def normalize_gcp(benefits: Any, accounts: Any, credit_pages: list[Any]) -> dict[str, Any]:
    """Combine the three page probes into a small status + numbers record."""
    benefits = benefits if isinstance(benefits, dict) else {}
    accounts = accounts if isinstance(accounts, dict) else {}
    n_billing = len(accounts.get("ids") or [])
    credits: list[dict[str, Any]] = []
    for pg in credit_pages:
        for c in (pg or {}).get("credits") or []:
            if not isinstance(c, dict) or not isinstance(c.get("remaining"), (int, float)):
                continue
            exp = c.get("expires")
            credits.append({
                "kind": c.get("kind") if c.get("kind") in ("developer_program", "free_trial", "other") else "other",
                "expires": exp if isinstance(exp, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", exp) else None,
                "label": "developer_program" if c.get("label") == "developer_program" else "other",
                "remaining": round(float(c["remaining"]), 2),
                "original": round(float(c["original"]), 2) if isinstance(c.get("original"), (int, float)) else None,
                "currency": str(c.get("currency") or "$")[:4],
                "expired": bool(c.get("expired")),
            })
    live = [c for c in credits if not c["expired"]]
    dev = [c for c in live if c["label"] == "developer_program"]
    if any(c["kind"] == "free_trial" for c in live):
        trial_state = "active"
    elif n_billing == 0 and accounts.get("trial_banner"):
        trial_state = "available"
    elif n_billing:
        trial_state = "none"
    else:
        trial_state = "unknown"
    program_state = benefits.get("state") if benefits.get("state") in (
        "needs_billing", "claimable", "claimed", "unknown") else "unknown"
    if n_billing == 0:
        state = "no_billing"
    elif dev:
        state = "active"
    else:
        state = program_state if program_state != "unknown" else "billing_no_program_credit"
    return {
        "ok": True,
        "state": state,
        "program_state": program_state,
        "billing_accounts": n_billing,
        "dev_remaining": round(sum(c["remaining"] for c in dev), 2) if dev else None,
        "other_remaining": round(sum(c["remaining"] for c in live if c["label"] == "other"), 2) if live else None,
        "trial_state": trial_state,
        "currency": (dev or live or [{"currency": "$"}])[0]["currency"],
        "credits": credits[:10],
    }


def _read_gcp(page: _ProfilePage) -> dict[str, Any]:
    if page.goto(GCP_BENEFITS_URL, "https://me.developers.google.com",
                 "document.body && document.body.innerText.length > 200") == "signed_out":
        return {"ok": False, "error": SIGNED_OUT}
    time.sleep(3)  # client-rendered benefit cards
    benefits = page.evaluate(GCP_BENEFITS_SCRIPT)
    if page.goto(GCP_BILLING_URL, "https://console.cloud.google.com", "document.body", timeout=60) == "signed_out":
        return {"ok": False, "error": SIGNED_OUT}
    accounts: dict[str, Any] = {}
    for _ in range(12):  # the console renders its billing account list asynchronously
        time.sleep(1.5)
        accounts = page.evaluate(GCP_ACCOUNTS_SCRIPT) or {}
        if accounts.get("ids"):
            break
    pages: list[Any] = []
    for bid in (accounts.get("ids") or [])[:3]:
        for suffix in ("credits/all", "credits"):
            try:
                page.goto(f"{GCP_BILLING_URL}/{bid}/{suffix}", "https://console.cloud.google.com",
                          "document.body", timeout=60)
            except FlowError:
                continue
            got = None
            for _ in range(12):
                time.sleep(1.5)
                got = page.evaluate(GCP_CREDITS_SCRIPT) or {}
                if got.get("credits") or got.get("no_credits_text"):
                    break
            if got and (got.get("credits") or got.get("no_credits_text")):
                pages.append(got)
                break
    return normalize_gcp(benefits, {"ids": accounts.get("ids") or [], "trial_banner": bool(accounts.get("trial_banner"))},
                         pages)


def read_account(account: str, expected_email: str | None = None, *, flow: bool = True, gcp: bool = False,
                 headless: bool = True) -> dict[str, Any]:
    """Read Flow credits and/or Google Cloud credit status for `account` in one browser launch."""
    account = check_account(account)
    out: dict[str, Any] = {"connected": is_connected(account)}
    if not out["connected"]:
        err = {"ok": False, "connected": False, "error": "尚未连接"}
        return {**out, **({"flow": err} if flow else {}), **({"gcp": err} if gcp else {})}
    try:
        with _ProfilePage(account, headless=headless) as page:
            for key, fn in (("flow", lambda: _read_flow(page, expected_email)), ("gcp", lambda: _read_gcp(page))):
                if not {"flow": flow, "gcp": gcp}[key]:
                    continue
                try:
                    out[key] = {**fn(), "connected": True}
                except FlowError as exc:
                    out[key] = {"ok": False, "connected": True, "error": str(exc)}
                except Exception as exc:  # never include page content: only the error type
                    out[key] = {"ok": False, "connected": True, "error": f"读取失败（{type(exc).__name__}）"}
    except FlowError as exc:
        for key, on in (("flow", flow), ("gcp", gcp)):
            if on:
                out[key] = {"ok": False, "connected": True, "error": str(exc)}
    except Exception as exc:
        for key, on in (("flow", flow), ("gcp", gcp)):
            if on:
                out[key] = {"ok": False, "connected": True, "error": f"读取失败（{type(exc).__name__}）"}
    return out


def read_credits(account: str, expected_email: str | None = None, *, headless: bool = True) -> dict[str, Any]:
    """Flow balance only (kept for the CLI / older callers)."""
    res = read_account(account, expected_email, flow=True, gcp=False, headless=headless)
    return {**res.get("flow", {}), "connected": res["connected"]}


if __name__ == "__main__":  # manual test: python flow_credits.py login|read ACCOUNT [expected_email]
    if len(sys.argv) < 3 or sys.argv[1] not in ("login", "read", "read-visible", "read-gcp"):
        print("usage: flow_credits.py login|read|read-visible|read-gcp ACCOUNT [expected_email]")
        sys.exit(2)
    acct = sys.argv[2]
    if sys.argv[1] == "login":
        print(open_login(acct))
    elif sys.argv[1] == "read-gcp":
        print(json.dumps(read_account(acct, None, flow=False, gcp=True), ensure_ascii=False))
    else:
        result = read_credits(acct, sys.argv[3] if len(sys.argv) > 3 else None,
                              headless=sys.argv[1] == "read")
        print(json.dumps(result, ensure_ascii=False))
