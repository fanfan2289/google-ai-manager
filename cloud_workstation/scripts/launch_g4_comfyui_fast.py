#!/usr/bin/env python3
"""Colab G4 (96GB Blackwell) Fast ComfyUI + MiniMax-H3 Launcher.

Runs inside the Colab G4 VM after Google Drive is mounted at `/content/drive`:
  1. Ensures ComfyUI (latest master) is installed on fast local NVMe SSD (`/content/ComfyUI`).
  2. Installs only missing Python dependencies with `uv` (or `pip`) in seconds.
  3. Symlinks all MiniMax-H3 weights from `/content/drive/MyDrive/AI_Workstation/models/*`
     into `/content/ComfyUI/models/*` (and copies small VAE/LoRA/embedding files to SSD).
  4. Starts ComfyUI API server on `http://127.0.0.1:8188` with `--highvram` and waits
     until ready.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DRIVE_ROOT = Path("/content/drive/MyDrive/AI_Workstation")
COMFY_DIR = Path("/content/ComfyUI")
COMFY_URL = "http://127.0.0.1:8188"
COMFY_LOG = Path("/tmp/comfyui_g4.log")

MODEL_SUBDIRS = [
    "diffusion_models",
    "text_encoders",
    "vae",
    "loras",
    "embeddings",
    "model_patches",
]


def run(cmd: list[str] | str, check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    print(f"[launch] $ {cmd if isinstance(cmd, str) else ' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, cwd=cwd)


def is_comfy_ready(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{COMFY_URL}/system_stats", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_comfyui() -> None:
    if not (COMFY_DIR / "main.py").is_file():
        print("[launch] Cloning latest ComfyUI master to fast local SSD...", flush=True)
        run(["git", "clone", "--depth", "1", "https://github.com/Comfy-Org/ComfyUI.git", str(COMFY_DIR)])
    else:
        print("[launch] Local /content/ComfyUI already present.", flush=True)

    # Verify critical MiniMax-H3 native node file exists
    h3_node = COMFY_DIR / "comfy_extras" / "nodes_minimax_h3.py"
    if not h3_node.is_file():
        raise RuntimeError(f"Expected native MiniMax H3 node at {h3_node}, but not found!")

    # Install ComfyUI requirements quickly (prefer uv if available, skip reinstalling torch)
    req_file = COMFY_DIR / "requirements.txt"
    filtered_req = Path("/tmp/comfy_req_notorch.txt")
    lines = [
        ln for ln in req_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
        and not any(ln.strip().lower().startswith(p) for p in ("torch==", "torch>", "torch<", "torchvision", "torchaudio", "torch "))
        and ln.strip().lower() != "torch"
    ]
    # Ensure `av` (PyAV for SaveVideo) and `comfy-kitchen` (int8_convrot / nvfp4 kernels) are installed
    extras = ["av", "comfy-kitchen", "websocket-client", "imageio-ffmpeg"]
    filtered_req.write_text("\n".join(lines + extras) + "\n", encoding="utf-8")

    uv_bin = shutil.which("uv")
    if uv_bin:
        run([uv_bin, "pip", "install", "--system", "--quiet", "-r", str(filtered_req)])
    else:
        run([sys.executable, "-m", "pip", "install", "-q", "-r", str(filtered_req)])


def link_models(copy_large_to_ssd: bool = False) -> None:
    src_models = DRIVE_ROOT / "models"
    if not src_models.is_dir():
        raise FileNotFoundError(
            f"Model directory not found at {src_models}. "
            "Did Google Drive mount properly and did model download complete?"
        )

    dst_models = COMFY_DIR / "models"
    linked = 0
    for sub in MODEL_SUBDIRS:
        sdir = src_models / sub
        ddir = dst_models / sub
        ddir.mkdir(parents=True, exist_ok=True)
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            target = ddir / f.name
            if target.exists() or target.is_symlink():
                target.unlink()
            size_gb = f.stat().st_size / (1024**3)
            # Copy small files (<3 GB) or optionally all files to fast SSD
            if copy_large_to_ssd or size_gb < 3.0:
                print(f"[launch] Staging to SSD: {sub}/{f.name} ({size_gb:.2f} GB)", flush=True)
                shutil.copy2(f, target)
            else:
                os.symlink(f, target)
                print(f"[launch] Symlinked Drive: {sub}/{f.name} ({size_gb:.2f} GB)", flush=True)
            linked += 1

    if linked == 0:
        raise RuntimeError(f"No model files found under {src_models}!")
    print(f"[launch] Total {linked} model asset(s) wired into ComfyUI.", flush=True)


def detect_vram_gb() -> float:
    try:
        out = subprocess.check_output(
            [sys.executable, "-c", "import torch; print(torch.cuda.get_device_properties(0).total_memory / (1024**3) if torch.cuda.is_available() else 0)"],
            text=True,
        ).strip()
        return float(out.splitlines()[-1])
    except Exception:
        return 0.0


def start_comfyui(profile: str) -> None:
    if is_comfy_ready():
        print("[launch] ComfyUI is already running on :8188.", flush=True)
        return

    vram_gb = detect_vram_gb()
    if profile == "lowvram" or (0 < vram_gb < 30):
        vram_flag: str | None = "--lowvram"
    elif 0 < vram_gb < 70:
        vram_flag = None  # ComfyUI latest default = dynamic/normal VRAM (40GB A100)
    else:
        vram_flag = "--highvram"
    print(f"[launch] Detected GPU VRAM: {vram_gb:.1f} GB -> using {vram_flag or '(default normal/dynamic VRAM)'}", flush=True)

    cmd = [
        sys.executable,
        "-u",
        str(COMFY_DIR / "main.py"),
        "--listen", "127.0.0.1",
        "--port", "8188",
        "--disable-auto-launch",
    ]
    if vram_flag:
        cmd.append(vram_flag)
    cmd.append("--fast")

    print(f"[launch] Starting ComfyUI server (profile={profile}) -> log: {COMFY_LOG}", flush=True)
    log_f = open(COMFY_LOG, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(COMFY_DIR),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    deadline = time.time() + 180
    while time.time() < deadline:
        if is_comfy_ready():
            print(f"[launch] ✅ ComfyUI READY at {COMFY_URL} (PID={proc.pid})", flush=True)
            return
        if proc.poll() is not None:
            log_f.flush()
            tail = COMFY_LOG.read_text(encoding="utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"ComfyUI exited early (code={proc.returncode}):\n{tail}")
        time.sleep(2.0)

    tail = COMFY_LOG.read_text(encoding="utf-8", errors="replace")[-3000:]
    raise TimeoutError(f"ComfyUI did not become ready within 180s. Last logs:\n{tail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="scheme_a", help="Hardware profile (scheme_a=96GB G4 highvram)")
    ap.add_argument("--copy-to-ssd", action="store_true", help="Copy large weights to local SSD before launch")
    args = ap.parse_args()

    t0 = time.time()
    print("=== MiniMax-H3 G4 Fast Launcher ===", flush=True)
    run("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv", check=False)
    run([sys.executable, "-c", "import torch; print('PyTorch:', torch.__version__, 'CUDA:', torch.version.cuda, 'Avail:', torch.cuda.is_available())"], check=False)

    ensure_comfyui()
    link_models(copy_large_to_ssd=args.copy_to_ssd)
    start_comfyui(args.profile)
    print(f"[launch] Startup finished in {time.time() - t0:.1f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
