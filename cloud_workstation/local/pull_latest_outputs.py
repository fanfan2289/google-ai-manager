#!/usr/bin/env python3
"""Pull the latest rendered `final_drama.mp4` and shot clips from Google Drive to the project `outputs/` folder."""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _ensure_proxy() -> None:
    """Apply the user-configured proxy (~/.google-ai-manager/settings.json, default: auto-detect)."""
    project_dir = str(Path(__file__).resolve().parents[2])
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    try:
        from providers import netcfg

        netcfg.apply_proxy_env()
    except Exception as exc:
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
        sys.stderr.write(f"[pull_outputs] Note: proxy config not applied ({exc})\n")


def _reexec_colab_venv() -> None:
    try:
        import colab_cli  # noqa: F401
        return
    except ImportError:
        py = Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python"
        if py.is_file() and os.path.realpath(py) != os.path.realpath(sys.executable):
            os.execv(str(py), [str(py), *sys.argv])
        raise


_ensure_proxy()
_reexec_colab_venv()

import colab_cli.cli  # noqa: E402, F401
from colab_ext import accounts, dfs_creds  # noqa: E402


def main() -> int:
    acct = accounts.current_account()
    data = dfs_creds.load(account=acct)
    tok, _ = dfs_creds.current_access_token(data)
    hdr = {"Authorization": f"Bearer {tok}"}

    # Find the latest final_drama.mp4 and its parent folder
    params = urllib.parse.urlencode({
        "q": "name='final_drama.mp4' and trashed=false",
        "orderBy": "createdTime desc",
        "pageSize": 1,
        "fields": "files(id,name,size,parents,createdTime)",
    })
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?{params}", headers=hdr)
    files = json.loads(urllib.request.urlopen(req, timeout=20).read().decode()).get("files", [])
    if not files:
        print("[pull] 未在 Google Drive 找到 final_drama.mp4")
        return 0

    parent_id = files[0]["parents"][0]
    folder_meta = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(f"https://www.googleapis.com/drive/v3/files/{parent_id}?fields=name", headers=hdr),
            timeout=20,
        ).read().decode()
    )
    folder_name = folder_meta.get("name", "latest")
    out_dir = Path(__file__).resolve().parents[2] / "outputs" / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    list_params = urllib.parse.urlencode({
        "q": f"'{parent_id}' in parents and trashed=false",
        "orderBy": "name",
        "fields": "files(id,name,size)",
    })
    items = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?{list_params}", headers=hdr),
            timeout=20,
        ).read().decode()
    ).get("files", [])

    for item in items:
        if not item["name"].endswith((".mp4", ".json")):
            continue
        dst = out_dir / item["name"]
        expected = int(item.get("size", 0))
        if dst.is_file() and expected > 0 and dst.stat().st_size == expected:
            print(f"[pull] Already exists: {dst.name}")
            continue
        dl_req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files/{item['id']}?alt=media", headers=hdr)
        with urllib.request.urlopen(dl_req, timeout=180) as r:
            dst.write_bytes(r.read())
        print(f"[pull] ✅ Downloaded -> {dst} ({dst.stat().st_size / (1024**2):.2f} MB)")

    print(f"[pull] 🎬 成品目录已拉回本地: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
