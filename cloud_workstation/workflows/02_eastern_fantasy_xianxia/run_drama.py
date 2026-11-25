#!/usr/bin/env python3
"""Automated 4-Shot MiniMax-H3 Short Drama Runner (ComfyUI Headless API).

Reads `drama_config.json` + `workflow_h3_fl2va_turbo.json`, generates every shot
on the local ComfyUI G4 server (`http://127.0.0.1:8188`), optionally chains the
last frame of shot N into `first_frame` of shot N+1, stitches the complete film
with ffmpeg, and saves both per-shot clips and `final_drama.mp4` directly to
Google Drive (`/content/drive/MyDrive/AI_Workstation/outputs/...`).
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

COMFY_URL = "http://127.0.0.1:8188"
COMFY_DIR = Path("/content/ComfyUI")
COMFY_OUTPUT_DIR = COMFY_DIR / "output"
COMFY_INPUT_DIR = COMFY_DIR / "input"


def align_h3_frames(duration_sec: float, fps: int = 24) -> int:
    """Snap frame count up to MiniMax-H3's required `17*k + 5` grid."""
    n = max(5, round(duration_sec * fps))
    while n % 17 != 5:
        n += 1
    return n


def http_json(method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    url = f"{COMFY_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI API {method} {path} failed HTTP {e.code}: {body[:2000]}") from e


def extract_last_frame(video_path: Path, out_png: Path) -> Path:
    """Extract the exact last frame of `video_path` into `out_png` for seamless continuation."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-sseof", "-0.15",
        "-i", str(video_path),
        "-update", "1", "-q:v", "2",
        str(out_png),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out_png.is_file() or out_png.stat().st_size == 0:
        raise RuntimeError(f"Failed to extract last frame from {video_path}")
    return out_png


def build_prompt_graph(
    base_workflow: dict,
    defaults: dict,
    shot: dict,
    prefix: str,
    first_frame_filename: str | None = None,
) -> dict:
    wf = copy.deepcopy(base_workflow)
    width = int(shot.get("width", defaults.get("width", 1344)))
    height = int(shot.get("height", defaults.get("height", 768)))
    fps = int(shot.get("fps", defaults.get("fps", 24)))
    dur = float(shot.get("duration_sec", defaults.get("duration_sec", 6)))
    length = int(shot.get("length", align_h3_frames(dur, fps)))
    steps = int(shot.get("steps", defaults.get("steps", 8)))
    seed = int(shot.get("seed", int(time.time())))
    use_turbo = bool(shot.get("use_turbo_lora", defaults.get("use_turbo_lora", True)))

    wf["1"]["inputs"]["unet_name"] = shot.get("unet_name", defaults["unet_name"])
    wf["3"]["inputs"]["clip_name"] = shot.get("clip_name", defaults["clip_name"])
    wf["4"]["inputs"]["vae_name"] = shot.get("video_vae", defaults["video_vae"])
    wf["5"]["inputs"]["vae_name"] = shot.get("audio_vae", defaults["audio_vae"])

    if use_turbo:
        wf["2"]["inputs"]["lora_name"] = shot.get("turbo_lora", defaults["turbo_lora"])
        wf["2"]["inputs"]["strength_model"] = float(shot.get("lora_strength", 1.0))
        model_ref = ["2", 0]
    else:
        wf.pop("2", None)
        model_ref = ["1", 0]

    wf["9"]["inputs"]["model"] = model_ref
    wf["9"]["inputs"]["steps"] = steps
    wf["10"]["inputs"]["model"] = model_ref

    wf["6"]["inputs"]["prompt"] = shot["prompt"]
    wf["6"]["inputs"]["width"] = width
    wf["6"]["inputs"]["height"] = height
    wf["6"]["inputs"]["length"] = length

    if first_frame_filename:
        wf["20"] = {
            "class_type": "LoadImage",
            "inputs": {"image": first_frame_filename},
        }
        wf["6"]["inputs"]["first_frame"] = ["20", 0]

    wf["7"]["inputs"]["noise_seed"] = seed
    wf["14"]["inputs"]["fps"] = float(fps)
    wf["15"]["inputs"]["filename_prefix"] = prefix
    return wf


def wait_for_prompt(prompt_id: str, shot_id: str) -> Path:
    t0 = time.time()
    last_log = 0.0
    while True:
        hist = http_json("GET", f"/history/{prompt_id}")
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                comfy_log = Path("/tmp/comfyui_g4.log")
                tail = ""
                if comfy_log.is_file():
                    tail = "\n--- /tmp/comfyui_g4.log (tail) ---\n" + comfy_log.read_text(encoding="utf-8", errors="replace")[-3500:]
                raise RuntimeError(f"ComfyUI execution error on {shot_id}: {json.dumps(status, ensure_ascii=False)}{tail}")
            outputs = entry.get("outputs", {})
            for node_out in outputs.values():
                for key in ("videos", "gifs", "images"):
                    items = node_out.get(key) or []
                    for item in items:
                        fn = item.get("filename")
                        sub = item.get("subfolder", "")
                        if fn and fn.lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                            p = COMFY_OUTPUT_DIR / sub / fn if sub else COMFY_OUTPUT_DIR / fn
                            if p.is_file():
                                print(
                                    f"[drama] ✅ {shot_id} finished in {time.time() - t0:.1f}s -> {p.name}",
                                    flush=True,
                                )
                                return p
            raise RuntimeError(f"ComfyUI finished {shot_id} but no video output found: {outputs}")

        now = time.time()
        if now - last_log >= 15.0:
            q = http_json("GET", "/queue")
            running = len(q.get("queue_running", []))
            pending = len(q.get("queue_pending", []))
            print(
                f"[drama] ⏳ Generating {shot_id}... elapsed={now - t0:.0f}s (running={running}, pending={pending})",
                flush=True,
            )
            last_log = now
        time.sleep(3.0)


def stitch_videos(clips: list[Path], out_mp4: Path) -> Path:
    if len(clips) == 1:
        shutil.copy2(clips[0], out_mp4)
        return out_mp4

    # Re-encode cleanly with normalized audio/video stream parameters to avoid concat DTS glitches
    concat_list = out_mp4.parent / "concat_list.txt"
    norm_clips = []
    for i, clip in enumerate(clips):
        norm = Path(f"/tmp/norm_shot_{i:02d}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(clip),
                "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-r", "24",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                str(norm),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        norm_clips.append(norm)

    concat_list.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in norm_clips) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    concat_list.unlink(missing_ok=True)
    return out_mp4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="drama_config.json")
    ap.add_argument("--workflow", default="workflow_h3_fl2va_turbo.json")
    ap.add_argument("--max-shots", type=int, default=0, help="Limit number of shots to render (0 = all)")
    ap.add_argument(
        "--max-runtime-min",
        type=float,
        default=0.0,
        help="Graceful time budget in minutes: stop queuing new shots if exceeded and stitch completed clips (0 = unlimited)",
    )
    args = ap.parse_args()

    t_start = time.time()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base_wf = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {})
    shots = cfg.get("shots", [])
    if args.max_shots > 0:
        shots = shots[: args.max_shots]
    if not shots:
        raise ValueError("No shots defined in drama_config.json")

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    drive_out_dir = Path(cfg["output_drive_dir"]) / f"{cfg.get('project_name', 'drama')}_{run_id}"
    drive_out_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== Starting Drama: {cfg.get('title', cfg.get('project_name'))} ({len(shots)} shots) ===", flush=True)
    print(f"[drama] Output Google Drive dir: {drive_out_dir}", flush=True)
    if args.max_runtime_min > 0:
        print(f"[drama] Graceful time budget: {args.max_runtime_min:.0f} min", flush=True)

    client_id = str(uuid.uuid4())
    local_clips: list[Path] = []
    prev_clip: Path | None = None

    for idx, shot in enumerate(shots, 1):
        elapsed_min = (time.time() - t_start) / 60.0
        if args.max_runtime_min > 0 and local_clips and elapsed_min >= args.max_runtime_min:
            print(
                f"\n[drama] ⏰ Reached graceful time budget ({elapsed_min:.1f}/{args.max_runtime_min:.0f} min) "
                f"after {len(local_clips)}/{len(shots)} shots. Stitching completed shots cleanly before shutdown!",
                flush=True,
            )
            break
        shot_id = shot.get("id", f"shot_{idx:02d}")
        print(f"\n[drama] --- [{idx}/{len(shots)}] {shot.get('title', shot_id)} ---", flush=True)

        first_frame_name = None
        if shot.get("continue_from_prev") and prev_clip is not None:
            ref_name = f"prev_last_{run_id}_{idx:02d}.png"
            extract_last_frame(prev_clip, COMFY_INPUT_DIR / ref_name)
            first_frame_name = ref_name
            print(f"[drama] Anchored previous shot last frame -> {ref_name}", flush=True)

        graph = build_prompt_graph(
            base_workflow=base_wf,
            defaults=defaults,
            shot=shot,
            prefix=f"h3_{run_id}/{shot_id}",
            first_frame_filename=first_frame_name,
        )
        res = http_json("POST", "/prompt", {"prompt": graph, "client_id": client_id})
        prompt_id = res["prompt_id"]
        print(f"[drama] Queued prompt_id={prompt_id}", flush=True)

        clip_path = wait_for_prompt(prompt_id, shot_id)
        local_clips.append(clip_path)
        prev_clip = clip_path

        # Copy each shot immediately to Google Drive so partial progress is never lost
        dst_clip = drive_out_dir / f"{idx:02d}_{shot_id}.mp4"
        shutil.copy2(clip_path, dst_clip)
        print(f"[drama] Saved shot clip -> {dst_clip} ({dst_clip.stat().st_size / (1024**2):.2f} MB)", flush=True)

    # Stitch full drama
    local_final = Path(f"/tmp/final_drama_{run_id}.mp4")
    stitch_videos(local_clips, local_final)
    drive_final = drive_out_dir / "final_drama.mp4"
    shutil.copy2(local_final, drive_final)
    shutil.copy2(Path(args.config), drive_out_dir / "drama_config.json")

    # Flush OS buffers and wait briefly for DriveFS to finish pushing final mp4
    os.sync()
    time.sleep(8.0)
    print(f"\n[drama] 🎉 ALL {len(shots)} SHOTS COMPLETE!", flush=True)
    print(f"[drama] Final film saved to Google Drive: {drive_final} ({drive_final.stat().st_size / (1024**2):.2f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
