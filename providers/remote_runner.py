"""Run one shell command on the held Colab GPU via `scripts/wsl_run_remote.sh`.

Safety: the runner owns every flag that controls billing (session, GPU, account,
`--fallback-gpu none`, the `--max-minutes` watchdog, `--reuse-session`). The user only
supplies the remote shell command, which is passed as a single argv element after `-c`
(never through a local shell), so it cannot add or override runner flags.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

RUN_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wsl_run_remote.sh"
MAX_COMMAND_CHARS = 4000
MIN_MINUTES, MAX_MINUTES = 30, 360
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
GPU_RE = re.compile(r"^[A-Z0-9]{1,12}$")


class RunnerError(ValueError):
    pass


def validate_command(cmd: Any) -> str:
    text = str(cmd or "").replace("\r\n", "\n").strip()
    if not text:
        raise RunnerError("请填写要在远端运行的命令")
    if len(text) > MAX_COMMAND_CHARS:
        raise RunnerError(f"命令过长（最多 {MAX_COMMAND_CHARS} 字符）")
    if "\0" in text:
        raise RunnerError("命令包含非法字符")
    return text


def clamp_minutes(value: Any) -> int:
    try:
        return max(MIN_MINUTES, min(MAX_MINUTES, int(value)))
    except (TypeError, ValueError):
        return 180


def build_runner_argv(
    *,
    session: str,
    gpu: str,
    account: str | None,
    max_minutes: Any,
    command: Any,
    mount_drive: bool = False,
    script: Path | None = None,
) -> list[str]:
    """Full argv for `bash wsl_run_remote.sh ...` that reuses the held session."""
    if not SESSION_RE.match(str(session or "")):
        raise RunnerError(f"会话名非法：{session!r}")
    if not GPU_RE.match(str(gpu or "")):
        raise RunnerError(f"GPU 型号非法：{gpu!r}")
    argv = [
        "bash", str(script or RUN_SCRIPT),
        "--reuse-session",
        "-s", session,
        "-g", gpu,
        "--fallback-gpu", "none",
        "--max-minutes", str(clamp_minutes(max_minutes)),
    ]
    if account:
        argv += ["-a", str(account)]
    if mount_drive:
        argv.append("--mount-drive")
    argv += ["-c", validate_command(command)]
    return argv
