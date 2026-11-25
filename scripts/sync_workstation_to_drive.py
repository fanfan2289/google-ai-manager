#!/usr/bin/env python3
"""Sync local `cloud_workstation/` scripts & workflows directly to Google Drive via API v3.

Runs locally inside WSL2 (zero Colab VM / zero CCU cost!).
Any edits made on Windows to `cloud_workstation/workflows/.../drama_config.json` are
synced to `/content/drive/MyDrive/AI_Workstation/...` in ~2 seconds.
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _ensure_proxy() -> None:
    """Apply the user-configured proxy (~/.google-ai-manager/settings.json, default: auto-detect)."""
    project_dir = str(Path(__file__).resolve().parent.parent)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    try:
        from providers import netcfg

        netcfg.apply_proxy_env()
    except Exception as exc:
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
        sys.stderr.write(f"[sync_workstation] Note: proxy config not applied ({exc})\n")


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


class DriveClient:
    def __init__(self, access_token: str):
        self.token = access_token
        self._folder_cache: dict[tuple[str, str], str] = {}

    def _req(self, method: str, url: str, data: bytes | None = None, content_type: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        last_err: Exception | None = None
        for attempt in range(1, 5):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as exc:
                last_err = exc
                if attempt < 4:
                    wait = 2 * attempt
                    print(f"[sync] Transient error ({exc!r}), retrying ({attempt}/4) in {wait}s...", flush=True)
                    time.sleep(wait)
        assert last_err is not None
        raise last_err

    def ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        escaped = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name='{escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name)", "pageSize": 5}
        )
        files = self._req("GET", url).get("files", [])
        if files:
            fid = files[0]["id"]
        else:
            meta = json.dumps(
                {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
            ).encode("utf-8")
            fid = self._req(
                "POST",
                "https://www.googleapis.com/drive/v3/files?fields=id",
                data=meta,
                content_type="application/json",
            )["id"]
            print(f"[sync] Created Drive folder: {name} (id={fid})")
        self._folder_cache[key] = fid
        return fid

    def ensure_path(self, parts: list[str]) -> str:
        cur = "root"
        for p in parts:
            cur = self.ensure_folder(cur, p)
        return cur

    def upsert_file(self, parent_id: str, local_path: Path) -> str:
        name = local_path.name
        escaped = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name='{escaped}' and trashed=false"
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name,size)", "pageSize": 5}
        )
        existing = self._req("GET", url).get("files", [])
        content = local_path.read_bytes()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        if existing:
            fid = existing[0]["id"]
            existing_size = int(existing[0].get("size") or -1)
            if local_path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and existing_size == len(content):
                print(f"[sync] Unchanged {local_path.name} ({len(content)} bytes)")
                return fid
            upd_url = f"https://www.googleapis.com/upload/drive/v3/files/{fid}?uploadType=media&fields=id,size"
            res = self._req("PATCH", upd_url, data=content, content_type=mime)
            print(f"[sync] Updated  {local_path.name} ({len(content)} bytes)")
            return res["id"]

        boundary = "===colab_h3_sync_boundary==="
        meta_json = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + meta_json + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

        res = self._req(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,size",
            data=body,
            content_type=f"multipart/related; boundary={boundary}",
        )
        print(f"[sync] Uploaded {local_path.name} ({len(content)} bytes)")
        return res["id"]


def main() -> int:
    project_dir = Path(__file__).resolve().parent.parent
    local_root = project_dir / "cloud_workstation"
    if not local_root.is_dir():
        print(f"[sync] ERROR: {local_root} not found", file=sys.stderr)
        return 1

    acct = accounts.current_account()
    data = dfs_creds.load(account=acct)
    if not data.get("refresh_token"):
        print(f"[sync] ERROR: No DriveFS credentials for account '{acct}'", file=sys.stderr)
        return 1
    tok, _ = dfs_creds.current_access_token(data)
    drive = DriveClient(tok)

    # Ensure standard AI_Workstation subdirectories exist on Drive
    for subdir in [
        ["AI_Workstation", "scripts"],
        ["AI_Workstation", "workflows"],
        ["AI_Workstation", "outputs"],
        ["AI_Workstation", "models", "diffusion_models"],
        ["AI_Workstation", "models", "text_encoders"],
        ["AI_Workstation", "models", "vae"],
        ["AI_Workstation", "models", "loras"],
        ["AI_Workstation", "models", "embeddings"],
    ]:
        drive.ensure_path(subdir)

    count = 0
    for path in sorted(local_root.rglob("*")):
        if path.is_dir() or path.name.startswith(".") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(local_root)
        parent_parts = ["AI_Workstation", *rel.parent.parts]
        parent_id = drive.ensure_path(parent_parts)
        drive.upsert_file(parent_id, path)
        count += 1

    print(f"[sync] ✅ Synced {count} file(s) -> Google Drive /MyDrive/AI_Workstation/ (account={acct})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
