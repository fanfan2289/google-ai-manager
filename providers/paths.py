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
import tempfile
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
