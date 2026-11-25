#!/usr/bin/env python3
"""Colab CPU -> Google Drive API v3 Direct Model Downloader for MiniMax-H3 (~65.5 GB).

Runs inside a cheap Colab CPU runtime (0 G4 GPU CCU burned!):
  1. Checks each model file's expected byte size from HuggingFace.
  2. Checks Google Drive `/MyDrive/AI_Workstation/models/<subdir>/<file>` via Drive API v3;
     skips instantly if already uploaded with exact matching byte size.
  3. Downloads to `/tmp/dl_stage/` at full GCP datacenter speed (`aria2c -x 16 -s 16`).
  4. Uploads straight to Google Drive cloud via Resumable Upload API v3 (32 MB chunks,
     auto-refreshes OAuth access token as needed, zero DriveFS FUSE cache risk!).
  5. Verifies exact cloud byte size returned by Google Drive API, then deletes `/tmp/dl_stage/<file>`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Model manifest: (hf_repo, hf_path, drive_subdir)
MODEL_MANIFEST: list[tuple[str, str, str]] = [
    # 1. Core Diffusion Models (official Comfy-Org int8_convrot recommended for Blackwell G4)
    ("Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models"),
    ("Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", "diffusion_models"),
    # 2. Text Encoder (Qwen3-VL 32B NVFP4 AWQ, official template default)
    ("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "text_encoders"),
    # 3. Video + Audio VAEs
    ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_int8_convrot.safetensors", "vae"),
    ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
    ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
    # 4. 8-Step Turbo LoRAs (2.5x speedup -> saves 60% G4 CCU!)
    ("lightx2v/Minimax-h3-Turbo", "minimax_h3_fl2v_turbo_8step_v1.2_768p_comfyui_bf16.safetensors", "loras"),
    ("lightx2v/Minimax-h3-Turbo", "minimax_h3_ref2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors", "loras"),
    # 5. Official VFX Embeddings (tiny <1MB each, awesome for Xianxia / Fantasy!)
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_storm_magic.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_spiral_ascent.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_bullet_time.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_dark_magic.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_fire_breath.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_blooming_flowers.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_four_seasons.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_art_is_explosion.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_kiss_camera.safetensors", "embeddings"),
    ("Comfy-Org/MiniMax-H3", "embeddings/minimaxh3_truman_show.safetensors", "embeddings"),
]

STAGE_DIR = Path("/tmp/dl_stage")
CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB (multiple of 256 KB required by Google Drive API)


class OAuthTokenProvider:
    def __init__(self, creds_json: str):
        self.cfg = json.loads(creds_json)
        self.access_token: str = ""
        self.expires_at: float = 0.0

    def get_token(self) -> str:
        if self.access_token and time.time() < self.expires_at - 300:
            return self.access_token
        body = urllib.parse.urlencode(
            {
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg.get("client_secret", ""),
                "refresh_token": self.cfg["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.get("token_uri", "https://oauth2.googleapis.com/token"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.access_token = data["access_token"]
        self.expires_at = time.time() + float(data.get("expires_in", 3600))
        return self.access_token


class DriveUploader:
    def __init__(self, tok_provider: OAuthTokenProvider):
        self.tok = tok_provider
        self._folder_cache: dict[tuple[str, str], str] = {}

    def _json_req(self, method: str, url: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.tok.get_token()}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        escaped = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name='{escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name)", "pageSize": 5}
        )
        files = self._json_req("GET", url).get("files", [])
        if files:
            fid = files[0]["id"]
        else:
            fid = self._json_req(
                "POST",
                "https://www.googleapis.com/drive/v3/files?fields=id",
                {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            )["id"]
        self._folder_cache[key] = fid
        return fid

    def ensure_path(self, parts: list[str]) -> str:
        cur = "root"
        for p in parts:
            cur = self.ensure_folder(cur, p)
        return cur

    def find_file(self, parent_id: str, name: str) -> dict | None:
        escaped = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name='{escaped}' and trashed=false"
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name,size)", "pageSize": 5}
        )
        files = self._json_req("GET", url).get("files", [])
        return files[0] if files else None

    def delete_file(self, file_id: str) -> None:
        req = urllib.request.Request(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers={"Authorization": f"Bearer {self.tok.get_token()}"},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass

    def resumable_upload(self, parent_id: str, local_path: Path) -> dict:
        total_size = local_path.stat().st_size
        name = local_path.name

        # Remove any incomplete previous copy with the same name
        existing = self.find_file(parent_id, name)
        if existing:
            if int(existing.get("size", -1)) == total_size:
                return existing
            print(f"  [drive] Removing partial file {name} (old size={existing.get('size')})...", flush=True)
            self.delete_file(existing["id"])

        # 1. Initiate resumable session
        init_meta = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        init_req = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name,size",
            data=init_meta,
            headers={
                "Authorization": f"Bearer {self.tok.get_token()}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(total_size),
            },
            method="POST",
        )
        with urllib.request.urlopen(init_req, timeout=60) as resp:
            upload_url = resp.headers["Location"]

        # 2. Stream 32 MB chunks with retry & live throughput reporting
        t0 = time.time()
        last_report = t0
        offset = 0
        with open(local_path, "rb") as f:
            while offset < total_size:
                f.seek(offset)
                chunk = f.read(min(CHUNK_SIZE, total_size - offset))
                end = offset + len(chunk) - 1
                content_range = f"bytes {offset}-{end}/{total_size}"

                result_json = None
                for attempt in range(1, 6):
                    put_req = urllib.request.Request(
                        upload_url,
                        data=chunk,
                        headers={
                            "Authorization": f"Bearer {self.tok.get_token()}",
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": content_range,
                        },
                        method="PUT",
                    )
                    try:
                        with urllib.request.urlopen(put_req, timeout=180) as resp:
                            if resp.status in (200, 201):
                                result_json = json.loads(resp.read().decode("utf-8"))
                                break
                    except urllib.error.HTTPError as e:
                        if e.code == 308:
                            # Resume incomplete (normal for non-final chunks!)
                            break
                        if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                            wait = 2.0 * attempt
                            print(f"  [drive] HTTP {e.code} on chunk {offset}, retrying in {wait}s...", flush=True)
                            time.sleep(wait)
                            continue
                        err_body = e.read().decode("utf-8", errors="replace")[:500]
                        raise RuntimeError(f"Drive upload failed HTTP {e.code}: {err_body}") from e
                    except Exception as e:
                        if attempt < 5:
                            wait = 2.0 * attempt
                            print(f"  [drive] Transient error ({e}), retrying in {wait}s...", flush=True)
                            time.sleep(wait)
                            continue
                        raise

                offset += len(chunk)
                now = time.time()
                if total_size > 100 * 1024 * 1024 and (now - last_report >= 10.0 or offset == total_size):
                    mb_done = offset / (1024**2)
                    mb_tot = total_size / (1024**2)
                    spd = mb_done / max(0.1, now - t0)
                    pct = (offset / total_size) * 100.0
                    print(
                        f"  [upload] {name}: {pct:5.1f}% ({mb_done:,.0f}/{mb_tot:,.0f} MB, {spd:.1f} MB/s)",
                        flush=True,
                    )
                    last_report = now

                if result_json is not None:
                    return result_json

        raise RuntimeError(f"Upload loop ended without HTTP 200 for {name}")


def get_hf_file_sizes(repos: set[str]) -> dict[tuple[str, str], int]:
    sizes: dict[tuple[str, str], int] = {}
    for repo in repos:
        url = f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true"
        req = urllib.request.Request(url, headers={"User-Agent": "colab-h3-downloader"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for item in json.loads(r.read().decode("utf-8")):
                if item.get("type") == "file":
                    sizes[(repo, item["path"])] = int(item.get("size", 0))
    return sizes


def ensure_aria2() -> bool:
    if shutil.which("aria2c"):
        return True
    print("[setup] Installing aria2 for 16-connection HuggingFace downloads...", flush=True)
    r = subprocess.run(
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq aria2",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0 and shutil.which("aria2c") is not None


def download_from_hf(repo: str, hf_path: str, dst: Path, expected_size: int, use_aria2: bool) -> None:
    url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(hf_path)}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file() and expected_size > 0 and dst.stat().st_size == expected_size:
        print(f"  [dl] Already staged locally: {dst.name}", flush=True)
        return
    dst.unlink(missing_ok=True)

    t0 = time.time()
    if use_aria2 and expected_size > 20 * 1024 * 1024:
        cmd = [
            "aria2c",
            "-x", "16", "-s", "16", "-k", "4M",
            "--file-allocation=none",
            "--summary-interval=10",
            "--console-log-level=warn",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "-d", str(dst.parent),
            "-o", dst.name,
            url,
        ]
        subprocess.run(cmd, check=True)
    else:
        subprocess.run(["curl", "-fSL", "--retry", "5", "-o", str(dst), url], check=True)

    actual = dst.stat().st_size
    if expected_size > 0 and actual != expected_size:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded size mismatch for {dst.name}: got {actual}, expected {expected_size}")
    elapsed = max(0.1, time.time() - t0)
    print(
        f"  [dl] ✅ Downloaded {dst.name} ({actual / (1024**3):.2f} GB in {elapsed:.1f}s, "
        f"{(actual / (1024**2)) / elapsed:.1f} MB/s)",
        flush=True,
    )


def main() -> int:
    creds_json = os.environ.get("DRIVE_CREDS_JSON", "")
    if not creds_json:
        print("[ERROR] Missing DRIVE_CREDS_JSON environment variable!", file=sys.stderr)
        return 1

    t_all = time.time()
    use_aria2 = ensure_aria2()
    print(f"[setup] aria2c available: {use_aria2}", flush=True)

    tok_provider = OAuthTokenProvider(creds_json)
    drive = DriveUploader(tok_provider)

    repos = {r for r, _, _ in MODEL_MANIFEST}
    hf_sizes = get_hf_file_sizes(repos)
    total_bytes = sum(hf_sizes.get((r, p), 0) for r, p, _ in MODEL_MANIFEST)
    print(
        f"=== MiniMax-H3 Direct-to-Drive Downloader: {len(MODEL_MANIFEST)} files "
        f"(total {total_bytes / (1024**3):.2f} GB) ===",
        flush=True,
    )

    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    done_bytes = 0

    for idx, (repo, hf_path, subdir) in enumerate(MODEL_MANIFEST, 1):
        fname = os.path.basename(hf_path)
        expected = hf_sizes.get((repo, hf_path), 0)
        parent_id = drive.ensure_path(["AI_Workstation", "models", subdir])

        existing = drive.find_file(parent_id, fname)
        if existing and expected > 0 and int(existing.get("size", -1)) == expected:
            done_bytes += expected
            print(
                f"[{idx}/{len(MODEL_MANIFEST)}] ⏭️ SKIP (already in Drive, verified {expected / (1024**3):.2f} GB): "
                f"{subdir}/{fname}",
                flush=True,
            )
            continue

        print(
            f"\n[{idx}/{len(MODEL_MANIFEST)}] ⬇️ {subdir}/{fname} ({expected / (1024**3):.2f} GB) from {repo} ...",
            flush=True,
        )
        local_file = STAGE_DIR / fname
        try:
            download_from_hf(repo, hf_path, local_file, expected, use_aria2)
            uploaded = drive.resumable_upload(parent_id, local_file)
            cloud_size = int(uploaded.get("size", 0))
            if expected > 0 and cloud_size != expected:
                raise RuntimeError(f"Cloud size mismatch for {fname}: cloud={cloud_size}, expected={expected}")
            done_bytes += cloud_size
            print(
                f"  [drive] ✅ Cloud verified: {subdir}/{fname} ({cloud_size / (1024**3):.2f} GB, "
                f"total progress {done_bytes / (1024**3):.2f}/{total_bytes / (1024**3):.2f} GB)",
                flush=True,
            )
        finally:
            local_file.unlink(missing_ok=True)

    print(
        f"\n🎉 ALL {len(MODEL_MANIFEST)} MODEL FILES ({done_bytes / (1024**3):.2f} GB) "
        f"SYNCED & CLOUD-VERIFIED IN GOOGLE DRIVE in {(time.time() - t_all) / 60:.1f} min!",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
