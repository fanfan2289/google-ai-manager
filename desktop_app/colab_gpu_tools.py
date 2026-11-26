#!/usr/bin/env python3
"""Google AI Manager — Native Windows WebView2 Desktop App + System Tray.

Antigravity Tools-style native desktop shell using Windows built-in Edge WebView2
(`pywebview`) + System Tray (`pystray`), managing the WSL2 Colab GPU monitor &
auto-hold/countdown-shutdown backend with zero console windows.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
import pystray
import webview

try:  # optional: Google Flow credits reader (needs websocket-client)
    import flow_credits as flow_reader
except Exception:  # pragma: no cover - missing dependency just disables the feature
    flow_reader = None

FLOW_REFRESH_SECONDS = 30 * 60
GCP_REFRESH_SECONDS = 6 * 3600
_FLOW_LOCK = threading.Lock()

APP_TITLE = "Google AI Manager"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8765
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
EXPECTED_VERSION = "2.6"  # must match APP_VERSION in scripts/colab_gpu_monitor.py
# Google pages the UI may open in the default browser (keep in sync with providers/benefits.py).
EXTERNAL_URL_PREFIXES = (
    "https://accounts.google.com/",
    "https://one.google.com/",
    "https://colab.research.google.com/",
    "https://antigravity.google/",
    "https://labs.google/",
    "https://flow.google.com/",
    "https://developers.google.com/",
    "https://console.cloud.google.com/",
    "https://studio.firebase.google.com/",
    "https://jules.google.com/",
    "https://gemini.google.com/",
    "https://notebooklm.google.com/",
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
LOGS_DIR = PROJECT_DIR / "logs"
ICON_PATH = APP_DIR / "colab_gpu_tools.ico"
DESKTOP_LOG_PATH = LOGS_DIR / "desktop_app.log"
WEBVIEW_STORAGE_DIR = APP_DIR / "webview_data"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
# Must match providers/colab_sessions.py (the backend re-validates everything it receives).
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_BACKEND_PROC: subprocess.Popen[Any] | None = None
_BACKEND_LOG_FH: Any = None


def _log_desktop(msg: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DESKTOP_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _ensure_single_instance() -> Any:
    """Prevent duplicate tray instances; restore existing window if already open."""
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Local\\ColabGPUTools_Desktop_SingleInstance")
    last_err = kernel32.GetLastError()
    if last_err == 183:  # ERROR_ALREADY_EXISTS
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        sys.exit(0)
    return mutex


def _draw_app_icon(dot_color: tuple[int, int, int, int] | None = None, size: int = 64) -> Image.Image:
    """Draw a crisp rounded-square dark blue/emerald GPU chip icon with optional status dot."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer rounded card (gradient approximation: rich indigo-blue fill + subtle border)
    pad = max(2, size // 16)
    r = max(8, size // 4)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=r,
        fill=(18, 36, 74, 255),
        outline=(59, 130, 246, 255),
        width=max(2, size // 24),
    )

    # Inner GPU chip silhouette
    c1 = int(size * 0.27)
    c2 = int(size * 0.73)
    draw.rounded_rectangle(
        [c1, c1, c2, c2],
        radius=max(4, size // 12),
        fill=(16, 185, 129, 255),
        outline=(236, 253, 245, 230),
        width=max(1, size // 32),
    )
    # Inner core square
    m1 = int(size * 0.40)
    m2 = int(size * 0.60)
    draw.rounded_rectangle(
        [m1, m1, m2, m2],
        radius=max(2, size // 24),
        fill=(10, 13, 20, 255),
    )

    # Pins
    pin_w = max(2, size // 24)
    pin_len = max(4, size // 10)
    for frac in (0.38, 0.62):
        pos = int(size * frac)
        draw.line([(pos, c1 - pin_len), (pos, c1)], fill=(147, 197, 253, 255), width=pin_w)
        draw.line([(pos, c2), (pos, c2 + pin_len)], fill=(147, 197, 253, 255), width=pin_w)
        draw.line([(c1 - pin_len, pos), (c1, pos)], fill=(147, 197, 253, 255), width=pin_w)
        draw.line([(c2, pos), (c2 + pin_len, pos)], fill=(147, 197, 253, 255), width=pin_w)

    if dot_color is not None:
        d = int(size * 0.30)
        x0 = size - d - 1
        y0 = size - d - 1
        draw.ellipse([x0 - 2, y0 - 2, size - 1, size - 1], fill=(10, 13, 20, 255))
        draw.ellipse([x0, y0, size - 3, size - 3], fill=dot_color)

    return img


def ensure_ico_file() -> Path:
    if not ICON_PATH.is_file():
        base = _draw_app_icon(dot_color=None, size=256)
        base.save(
            ICON_PATH,
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
    return ICON_PATH


def _http_json(path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any]:
    url = f"{BACKEND_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _check_backend_health() -> dict[str, Any] | None:
    try:
        return _http_json("/api/health", timeout=1.5)
    except Exception:
        return None


def _is_port_open() -> bool:
    try:
        urllib.request.urlopen(f"{BACKEND_URL}/api/state", timeout=1.0).close()
        return True
    except Exception:
        return False


def _to_wsl_path(win_path: Path) -> str:
    drive = win_path.drive.rstrip(":").lower()
    rest = win_path.as_posix().split(":", 1)[-1]
    return f"/mnt/{drive}{rest}"


def ensure_wsl_backend() -> None:
    """Ensure the v2 WSL2 colab_gpu_monitor backend is running silently in the background."""
    global _BACKEND_PROC, _BACKEND_LOG_FH

    health = _check_backend_health()
    if health and health.get("ok") and health.get("version") == EXPECTED_VERSION:
        _log_desktop(f"Reusing running v{EXPECTED_VERSION} WSL backend.")
        return

    # If an older v1 backend is occupying port 8765, cleanly stop it first
    if _is_port_open():
        _log_desktop("Stopping older backend on port 8765...")
        try:
            subprocess.run(
                ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-lc", "pkill -f colab_gpu_monitor.py || true"],
                creationflags=CREATE_NO_WINDOW,
                timeout=8,
                check=False,
            )
        except Exception:
            pass
        # The old backend finishes an in-flight probe (up to ~30 s) and its release checks before it
        # exits; spawning earlier fails with "Address already in use".
        deadline = time.time() + 45.0
        while time.time() < deadline and _is_port_open():
            time.sleep(0.5)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    daemon_log = LOGS_DIR / "wsl_monitor_daemon.log"
    wsl_script = _to_wsl_path(PROJECT_DIR / "scripts" / "colab_gpu_monitor.py")

    _log_desktop(f"Spawning persistent WSL2 backend: {wsl_script}")
    _BACKEND_LOG_FH = open(daemon_log, "a", encoding="utf-8", buffering=1)
    env = dict(os.environ)
    env["WSL_UTF8"] = "1"
    _BACKEND_PROC = subprocess.Popen(
        [
            "wsl.exe",
            "-d",
            "Ubuntu",
            "-e",
            "python3",
            "-u",
            wsl_script,
            "--host",
            BACKEND_HOST,
            "--port",
            str(BACKEND_PORT),
        ],
        creationflags=CREATE_NO_WINDOW,
        stdout=_BACKEND_LOG_FH,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
    )

    deadline = time.time() + 25.0
    while time.time() < deadline:
        h = _check_backend_health()
        if h and h.get("ok"):
            _log_desktop(f"WSL2 backend ready: {h}")
            return
        if _BACKEND_PROC.poll() is not None:
            _log_desktop(f"WSL2 backend exited early with rc={_BACKEND_PROC.returncode}")
            break
        time.sleep(0.35)
    _log_desktop("Timed out waiting for WSL2 backend health check.")


class DesktopController:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.tray: pystray.Icon | None = None
        self._quitting = False
        self._stop_poll = threading.Event()
        self._icon_cache = {
            "ok": _draw_app_icon((16, 185, 129, 255), 64),
            "holding": _draw_app_icon((245, 158, 11, 255), 64),
            "paused": _draw_app_icon((100, 116, 139, 255), 64),
            "error": _draw_app_icon((239, 68, 68, 255), 64),
        }
        self._last_icon_key = "ok"
        self._flow_last: dict[str, float] = {}
        self._gcp_last: dict[str, float] = {}

    # ---- JS Bridge Methods (window.pywebview.api.*) ----
    def open_external_url(self, url: str) -> dict[str, Any]:
        """Open an allow-listed Google page (OAuth consent, benefit pages) in the default browser."""
        url = str(url or "").strip()
        if not url.startswith(EXTERNAL_URL_PREFIXES) or any(c in url for c in '\r\n"<> '):
            return {"ok": False, "error": "only allow-listed Google pages can be opened"}
        os.startfile(url)  # ShellExecute: no WinForms access, safe off the UI thread
        return {"ok": True}

    def open_session_terminal(self, account: str, session: str) -> dict[str, Any]:
        """Open a new console attached to a Colab session (`colab ssh`, or `colab repl` without an SSH key)."""
        account, session = str(account or "").strip(), str(session or "").strip()
        if not (_ACCOUNT_RE.match(account) and _SESSION_RE.match(session)):
            return {"ok": False, "error": "invalid account or session name"}
        netcfg = _to_wsl_path(PROJECT_DIR / "providers" / "netcfg.py")
        a, s = shlex.quote(account), shlex.quote(session)
        script = (
            'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; '
            f'eval "$(python3 {shlex.quote(netcfg)} shell-env 2>/dev/null)" || true; '
            f'echo "Colab 会话 {session}（账号 {account}）"; '
            'if [ -f ~/.ssh/id_ed25519.pub ] || [ -f ~/.ssh/id_ecdsa.pub ]; then '
            f'colab -a {a} ssh -s {s}; '
            'else echo "未找到 SSH 公钥（~/.ssh/id_ed25519.pub），改用 Python REPL（在命令前加 ! 可运行 Shell 命令）。'
            '需要完整终端可先在 WSL 中运行: ssh-keygen -t ed25519"; '
            f'colab -a {a} repl -s {s}; fi; '
            'echo; read -rp "按回车关闭窗口..." _'
        )
        # New console process only; no WinForms access, safe off the UI thread.
        subprocess.Popen(["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-lc", script], creationflags=CREATE_NEW_CONSOLE)
        return {"ok": True}

    def choose_upload_file(self) -> dict[str, Any]:
        """Native open-file dialog (pywebview marshals it to the GUI thread)."""
        if not self.window:
            return {"ok": False, "error": "window not ready"}
        picked = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
        if not picked:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(picked[0])}

    def open_downloads_folder(self) -> dict[str, Any]:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(DOWNLOADS_DIR))
        return {"ok": True}

    # ---- Google Flow credits (experimental; dedicated Edge profile per account) ----
    def flow_status(self, accounts: list[str] | None = None) -> dict[str, Any]:
        if flow_reader is None:
            return {"ok": False, "available": False, "error": "缺少依赖 websocket-client（pip install -r requirements-desktop.txt）"}
        out = {}
        for a in accounts or []:
            try:
                out[str(a)] = flow_reader.is_connected(str(a))
            except Exception:
                out[str(a)] = False
        return {"ok": True, "available": True, "connected": out}

    def flow_connect(self, account: str) -> dict[str, Any]:
        if flow_reader is None:
            return {"ok": False, "error": "缺少依赖 websocket-client"}
        try:
            return flow_reader.open_login(account)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def flow_refresh(self, account: str, *, flow: bool | None = None, gcp: bool | None = None) -> dict[str, Any]:
        """Read Flow credits and/or Cloud credit status (per the settings) and report the numbers."""
        if flow_reader is None:
            return {"ok": False, "error": "缺少依赖 websocket-client"}
        try:
            flow_reader.check_account(account)
            info = _http_json("/api/flow", timeout=10)
        except Exception as exc:
            return {"ok": False, "error": f"无法连接后端：{exc}"}
        targets = {t.get("account"): t.get("email") for t in info.get("targets", [])}
        if account not in targets:
            return {"ok": False, "error": "未知账号"}
        do_flow = bool(info.get("enabled")) if flow is None else flow
        do_gcp = bool(info.get("gcp_enabled")) if gcp is None else gcp
        if not (do_flow or do_gcp):
            return {"ok": False, "error": "请先在「设置 → 实验功能」中开启 Flow 积分或 Cloud 赠金"}
        with _FLOW_LOCK:
            res = flow_reader.read_account(account, targets.get(account), flow=do_flow, gcp=do_gcp)
        now = time.time()
        out: dict[str, Any] = {"ok": True}
        if do_flow:
            self._flow_last[account] = now
            report = {k: res["flow"].get(k) for k in ("ok", "connected", "credits", "email_verified", "error")}
            out["flow"] = report
            try:
                _http_json("/api/flow/report", method="POST", payload={"account": account, **report}, timeout=10)
            except Exception as exc:
                _log_desktop(f"Flow report failed: {type(exc).__name__}")
        if do_gcp:
            self._gcp_last[account] = now
            g = res["gcp"]
            report = {k: g.get(k) for k in ("ok", "connected", "state", "billing_accounts", "dev_remaining",
                                            "other_remaining", "currency", "error")}
            out["gcp"] = report
            try:
                _http_json("/api/gcp/report", method="POST", payload={"account": account, **report}, timeout=10)
            except Exception as exc:
                _log_desktop(f"GCP report failed: {type(exc).__name__}")
        out["ok"] = all(v.get("ok") for k, v in out.items() if isinstance(v, dict))
        return out

    def flow_disconnect(self, account: str) -> dict[str, Any]:
        if flow_reader is None:
            return {"ok": False, "error": "缺少依赖 websocket-client"}
        try:
            with _FLOW_LOCK:
                flow_reader.close_profile_browser(account)
                flow_reader.disconnect(account)
            _http_json("/api/flow/forget", method="POST", payload={"account": account}, timeout=10)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _flow_loop(self) -> None:
        """Every few minutes: when enabled, refresh connected accounts older than FLOW_REFRESH_SECONDS."""
        time.sleep(30)
        while not self._stop_poll.is_set():
            try:
                if flow_reader is not None:
                    info = _http_json("/api/flow", timeout=10)
                    for t in info.get("targets", []):
                        acct = str(t.get("account") or "")
                        if not acct or not flow_reader.is_connected(acct):
                            continue
                        due_flow = bool(info.get("enabled")) and                             time.time() - self._flow_last.get(acct, 0.0) >= FLOW_REFRESH_SECONDS
                        due_gcp = bool(info.get("gcp_enabled")) and                             time.time() - self._gcp_last.get(acct, 0.0) >= GCP_REFRESH_SECONDS
                        if due_flow or due_gcp:
                            self.flow_refresh(acct, flow=due_flow, gcp=due_gcp)
            except Exception as exc:
                _log_desktop(f"Flow loop: {type(exc).__name__}")
            self._stop_poll.wait(300)

    def minimize_to_tray(self) -> dict[str, Any]:
        if self.window:
            self.window.hide()
        return {"ok": True}

    def on_gpu_locked(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.show_window()
        if self.tray:
            gpu = (payload or {}).get("gpu", "GPU")
            try:
                self.tray.notify(f"已锁定 {gpu}，请在倒计时内认领使用或运行命令，超时将自动关机", f"🔒 {APP_TITLE}")
            except Exception:
                pass
        return {"ok": True}

    # ---- Window & Tray Control ----
    def show_window(self, *_args: Any) -> None:
        if self.window:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

    def on_window_closing(self) -> bool:
        if self._quitting:
            return True
        if self.window:
            self.window.hide()
        return False

    def _tray_probe_now(self, *_args: Any) -> None:
        threading.Thread(target=lambda: self._safe_post("/api/probe-now", {}), daemon=True).start()

    def _tray_toggle_pause(self, *_args: Any) -> None:
        threading.Thread(target=lambda: self._safe_post("/api/toggle-pause", {}), daemon=True).start()

    def _tray_stop_all(self, *_args: Any) -> None:
        threading.Thread(target=lambda: self._safe_post("/api/stop-all", {}), daemon=True).start()

    def _safe_post(self, path: str, body: dict[str, Any]) -> None:
        try:
            _http_json(path, method="POST", payload=body, timeout=15.0)
        except Exception:
            pass

    def quit_and_release(self, *_args: Any) -> None:
        self._quitting = True
        self._stop_poll.set()
        try:
            _http_json("/api/shutdown", method="POST", payload={"release_hold": True}, timeout=20.0)
        except Exception:
            pass
        if _BACKEND_PROC is not None and _BACKEND_PROC.poll() is None:
            try:
                _BACKEND_PROC.terminate()
            except Exception:
                pass
        if self.tray:
            self.tray.stop()
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def quit_ui_only(self, *_args: Any) -> None:
        self._quitting = True
        self._stop_poll.set()
        if self.tray:
            self.tray.stop()
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _tray_sync_loop(self) -> None:
        while not self._stop_poll.is_set():
            try:
                st = _http_json("/api/state", timeout=2.5)
                hold = st.get("active_hold")
                acct = st.get("account") or {}
                bal = acct.get("balance")
                bal_str = f"{bal:.2f} CCU" if isinstance(bal, (int, float)) else "-- CCU"

                if st.get("critical_alert"):
                    icon_key = "error"
                    tip = f"{APP_TITLE} | ⚠️ 残留告警 ({bal_str})"
                elif hold:
                    icon_key = "holding"
                    rem = int(hold.get("remaining_s") or 0)
                    mm, ss = divmod(rem, 60)
                    tip = f"{APP_TITLE} | 🔒 已锁 {hold.get('gpu')} 剩余 {mm:02d}:{ss:02d}"
                elif st.get("paused"):
                    icon_key = "paused"
                    tip = f"{APP_TITLE} | 已暂停 ({bal_str})"
                else:
                    icon_key = "ok"
                    gpus = "/".join((st.get("config") or {}).get("enabled_gpus") or [])
                    tip = f"{APP_TITLE} | 监控 {gpus} · {bal_str}"

                if self.tray:
                    self.tray.title = tip[:120]
                    if icon_key != self._last_icon_key:
                        self._last_icon_key = icon_key
                        self.tray.icon = self._icon_cache[icon_key]
            except Exception:
                pass
            self._stop_poll.wait(4.0)

    def start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("打开 Google AI Manager", self.show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🔍 立即探测一轮", self._tray_probe_now),
            pystray.MenuItem("⏯ 暂停 / 恢复监控", self._tray_toggle_pause),
            pystray.MenuItem("🛑 紧急强杀全部 GPU", self._tray_stop_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("仅关闭窗口（后台继续守护）", self.quit_ui_only),
            pystray.MenuItem("退出程序（释放占卡并停监控）", self.quit_and_release),
        )
        self.tray = pystray.Icon(
            "colab_gpu_tools",
            self._icon_cache["ok"],
            f"{APP_TITLE} | 正在连接...",
            menu,
        )
        threading.Thread(target=self.tray.run, name="tray-loop", daemon=True).start()
        threading.Thread(target=self._tray_sync_loop, name="tray-sync", daemon=True).start()
        threading.Thread(target=self._flow_loop, name="flow-credits", daemon=True).start()


class JsBridge:
    """Minimal JS-exposed API object (no unserializable attributes)."""

    def __init__(self, controller: "DesktopController") -> None:
        self._ctrl = controller

    def minimize_to_tray(self) -> dict[str, Any]:
        return self._ctrl.minimize_to_tray()

    def open_external_url(self, url: str) -> dict[str, Any]:
        return self._ctrl.open_external_url(url)

    def open_session_terminal(self, account: str, session: str) -> dict[str, Any]:
        return self._ctrl.open_session_terminal(account, session)

    def choose_upload_file(self) -> dict[str, Any]:
        return self._ctrl.choose_upload_file()

    def open_downloads_folder(self) -> dict[str, Any]:
        return self._ctrl.open_downloads_folder()

    def flow_status(self, accounts: list[str] | None = None) -> dict[str, Any]:
        return self._ctrl.flow_status(accounts)

    def flow_connect(self, account: str) -> dict[str, Any]:
        return self._ctrl.flow_connect(account)

    def flow_refresh(self, account: str) -> dict[str, Any]:
        return self._ctrl.flow_refresh(account)

    def flow_disconnect(self, account: str) -> dict[str, Any]:
        return self._ctrl.flow_disconnect(account)

    def on_gpu_locked(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ctrl.on_gpu_locked(payload)


def _apply_win32_icon_and_focus(ico_path: Path) -> int:
    """Thread-safe Win32 WM_SETICON + ensure window is visible and focused."""
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(30):
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return 0
    try:
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        hicon_big = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass
    try:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return int(hwnd)


def main() -> int:
    try:
        _mutex = _ensure_single_instance()
        ico_path = ensure_ico_file()
        WEBVIEW_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        ensure_wsl_backend()

        ctrl = DesktopController()
        bridge = JsBridge(ctrl)

        window = webview.create_window(
            title=APP_TITLE,
            url=BACKEND_URL,
            js_api=bridge,
            width=1220,
            height=780,
            min_size=(940, 620),
            background_color="#0a0d14",
            resizable=True,
        )
        ctrl.window = window
        window.events.closing += ctrl.on_window_closing

        def _on_gui_ready() -> None:
            hwnd = _apply_win32_icon_and_focus(ico_path)
            ctrl.start_tray()
            _log_desktop(f"GUI ready & system tray started (hwnd={hwnd}).")

        _log_desktop("Starting Edge WebView2 native window...")
        webview.start(
            func=_on_gui_ready,
            gui="edgechromium",
            icon=str(ico_path),
            private_mode=False,
            storage_path=str(WEBVIEW_STORAGE_DIR),
            debug=False,
        )
        return 0
    except Exception:
        _log_desktop("FATAL ERROR:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
