"""Colab session manager helpers (pure, offline-testable).

The backend lists every live Colab VM of every registered account and lets the UI act on one
session through the public `colab` CLI (`url`, `exec`, `ls`, `download`, `upload`,
`restart-kernel`, `log`). Everything that reaches a subprocess is validated here first and is
always passed as argv elements, never through a shell.

Session sources:
  * ``hold``      – the GPU this app holds (watchdog-protected, see `active_hold`)
  * ``cli``       – tracked by the colab CLI (created with `colab new` / scripts)
  * ``untracked`` – live on the server but unknown to the CLI: opened in the browser, or an
                    orphan left behind by a crashed client. Still billed until stopped.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
MAX_PATH = 500
COLAB_URL_PREFIX = "https://colab.research.google.com/"

# Read-only diagnostics executed in the session kernel (`colab exec`).
GPU_INFO_SCRIPT = r"""
import shutil, subprocess
def run(cmd):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:
        out = f"({exc})"
    print(out or "-")
print("## GPU")
if shutil.which("nvidia-smi"):
    run("nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader")
else:
    print("无 GPU（CPU / TPU 运行时）")
print("## 运行时长")
run("uptime -p")
print("## 内存")
run("free -h | sed -n 2p")
print("## 磁盘 /content")
run("df -h /content | tail -1")
print("## 云盘")
import os
print("已挂载 /content/drive" if os.path.isdir("/content/drive/MyDrive") else "未挂载")
"""


class SessionError(ValueError):
    pass


def check_session_name(name: Any) -> str:
    name = str(name or "").strip()
    if not SESSION_RE.match(name):
        raise SessionError(f"会话名非法：{name!r}")
    return name


def check_account_name(name: Any, known: Iterable[str]) -> str:
    name = str(name or "").strip()
    if not ACCOUNT_RE.match(name) or name not in set(known):
        raise SessionError(f"未知账号：{name!r}")
    return name


def check_remote_path(path: Any, *, default: str = "/content") -> str:
    p = str(path if path not in (None, "") else default).strip()
    if not p or len(p) > MAX_PATH or any(c in p for c in "\0\r\n"):
        raise SessionError("远端路径非法")
    if p.startswith("-"):
        raise SessionError("远端路径不能以 - 开头")
    return p


def download_target(base: Path, account: str, session: str, remote_path: str) -> Path:
    """Local file for `colab download`: <base>/<account>/<session>/<basename>, never outside base."""
    name = PurePosixPath(remote_path.rstrip("/")).name
    if not name or name in (".", "..") or not re.match(r"^[^\\/:*?\"<>|\0]{1,200}$", name):
        raise SessionError("无法确定下载文件名")
    target = (base / account / session / name).resolve()
    if base.resolve() not in target.parents:
        raise SessionError("下载路径非法")
    return target


def clean_cli_output(text: str, limit: int = 20000) -> str:
    """Drop the CLI's `[colab]` progress lines and cap the size."""
    lines = [ln for ln in (text or "").splitlines() if not ln.strip().startswith("[colab]")]
    out = "\n".join(lines).strip()
    return out[-limit:]


GPU_MODEL_RE = re.compile(r"^[A-Za-z][\w .()/-]{1,60},\s*\d+\s*MiB$")


def pick_gpu_model(stdout: str) -> str | None:
    """The `name, memory.total` line printed by nvidia-smi (ignores REPL echoes / progress lines)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if GPU_MODEL_RE.match(line):
            return line
    return None


def parse_session_url(stdout: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(COLAB_URL_PREFIX) and not any(c in line for c in '\r\n"<> '):
            return line
    raise SessionError("colab url 未返回可用链接")


def build_sessions_payload(
    accounts: list[dict[str, Any]],
    *,
    hold: dict[str, Any] | None,
    first_seen: dict[str, float],
    now: float,
) -> dict[str, Any]:
    """Shape per-account `_query_account`-like dicts for the UI (no proxy tokens / URLs).

    `accounts` items: {"account", "balance", "rate_hourly", "usage_error", "assignments": [
    {"name", "endpoint", "hardware", "shape", "variant", ...}], "error"}.
    """
    out_accounts = []
    totals = {"sessions": 0, "untracked": 0, "rate_hourly": 0.0}
    for acct in accounts:
        rows = []
        for a in acct.get("assignments") or []:
            ep = str(a.get("endpoint") or "")
            name = a.get("name")
            tracked = bool(name) and name != "?"
            is_hold = bool(hold) and hold.get("endpoint") == ep and (hold.get("account") in (None, acct["account"]))
            source = "hold" if is_hold else ("cli" if tracked else "untracked")
            seen = first_seen.get(ep)
            row = {
                "endpoint": ep,
                "name": name if tracked else None,
                "tracked": tracked,
                "hardware": a.get("hardware") or "?",
                "shape": a.get("shape") or "",
                "variant": a.get("variant") or "",
                "source": source,
                "first_seen_ts": seen,
                "seen_for_s": int(now - seen) if seen else None,
            }
            if is_hold:
                row["hold"] = {
                    "state": hold.get("state"),
                    "gpu_model": hold.get("gpu_model"),
                    "locked_at_ts": hold.get("locked_at_ts"),
                    "remaining_s": max(0, int(float(hold.get("expires_at_ts") or now) - now)),
                    "drive_status": hold.get("drive_status"),
                    "job_status": hold.get("job_status"),
                }
            rows.append(row)
        rows.sort(key=lambda r: ({"hold": 0, "cli": 1, "untracked": 2}[r["source"]], r["name"] or r["endpoint"]))
        rate = acct.get("rate_hourly")
        totals["sessions"] += len(rows)
        totals["untracked"] += sum(1 for r in rows if r["source"] == "untracked")
        if isinstance(rate, (int, float)):
            totals["rate_hourly"] += float(rate)
        out_accounts.append({
            "account": acct["account"],
            "balance": acct.get("balance"),
            "rate_hourly": rate,
            "usage_error": acct.get("usage_error"),
            "error": acct.get("error"),
            "sessions": rows,
        })
    totals["rate_hourly"] = round(totals["rate_hourly"], 4)
    return {"accounts": out_accounts, "totals": totals}
