"""User-private application home (`~/.google-ai-manager/`) and safe JSON helpers.

All user settings, account labels and any future provider credentials live here —
physically outside the repository, created with `0700` directory / `0600` file
permissions — so nothing user-specific can ever be committed by accident.

Override the location with the `GAM_HOME` environment variable (used by tests).
Stdlib only: this module must import under the system `python3` too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any


def app_home() -> Path:
    raw = os.environ.get("GAM_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".google-ai-manager"


def ensure_app_home() -> Path:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return home


def settings_path() -> Path:
    return app_home() / "settings.json"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_private_json(path: Path, data: Any) -> Path:
    """Atomically write `data` as JSON with `0600` permissions."""
    if path.parent == app_home():
        ensure_app_home()  # keep the app home itself 0700
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


_win_home_lock = threading.Lock()
_win_home_cache: list[Path | None] = []


def _wsl_path(win_path: str) -> Path | None:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", win_path.strip())
    if not m:
        return None
    return Path(f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}")


def windows_home() -> Path | None:
    """The Windows user's home folder (where desktop tools such as Antigravity Tools store data).

    Native Windows: `Path.home()`. Inside WSL: `%USERPROFILE%` mapped to /mnt/<drive>/...
    (asked once via cmd.exe, cached). Override with `GAM_WIN_HOME`. None when unknown.
    """
    raw = os.environ.get("GAM_WIN_HOME")
    if raw:
        return Path(raw).expanduser()
    if os.name == "nt":
        return Path.home()
    with _win_home_lock:
        if _win_home_cache:
            return _win_home_cache[0]
        found: Path | None = None
        if Path("/mnt/c").is_dir():
            try:
                out = subprocess.run(
                    ["cmd.exe", "/d", "/c", "echo %USERPROFILE%"],
                    capture_output=True, text=True, timeout=8, cwd="/mnt/c", stdin=subprocess.DEVNULL,
                ).stdout
                lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
                cand = _wsl_path(lines[-1]) if lines else None
                if cand and cand.is_dir():
                    found = cand
            except (OSError, subprocess.SubprocessError):
                found = None
        _win_home_cache.append(found)
        return found
