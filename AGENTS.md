# AGENTS.md — Google AI Manager Collaboration Guide

This repository (`google-ai-manager`) is collaboratively developed by multiple AI coding agents (e.g., Claude Code and Antigravity) and the repository owner. Follow these conventions to keep changes consistent, safe, and free of credential leaks.

## 1. Architecture Overview

- **Windows Desktop Shell (`desktop_app/colab_gpu_tools.py`)**
  - Native Edge WebView2 window (`pywebview`) + System Tray (`pystray`) + single-instance Win32 mutex.
  - Spawns and monitors the WSL2 backend daemon (`scripts/colab_gpu_monitor.py`) via `wsl.exe` with `CREATE_NO_WINDOW`.
  - **Windows GUI rule**: `webview.start(func=...)` runs `func` on a background thread; never mutate WinForms controls directly off the UI thread—use thread-safe Win32 messages (e.g., `SendMessageW` for `WM_SETICON`) or `pywebview` APIs.
  - Keep `js_api` bound to a minimal `JsBridge` wrapper (never expose objects containing `PIL.Image`, `pystray.Icon`, or `threading.Event`).
- **Backend Daemon & Anti-Ghost-Billing Core (`scripts/colab_gpu_monitor.py`, `scripts/colab_meter.py`)**
  - Runs inside WSL2 Ubuntu using `google-colab-cli`'s `uv` tool venv (`~/.local/share/uv/tools/google-colab-cli/bin/python`).
  - Serves `scripts/monitor_ui.html`, REST APIs (`/api/health`, `/api/state`, `/api/probe-now`, `/api/grab-now`, `/api/hold/*`, `/api/job/*`, `/api/stop-all`, `/api/shutdown`), and SSE (`/api/events`) on `127.0.0.1:8765`.
  - Persists probe history in `logs/gpu_monitor.db`, user config in `logs/gpu_monitor_config.json`, and active lock state in `logs/active_hold.json`.
- **Cloud Workflow Runner (`scripts/wsl_run_h3.sh`, `cloud_workstation/`)**
  - Supports `--reuse-session` to take over an already-locked Colab session without stopping/re-allocating it, skipping `colab dfs mount` when `/content/drive/MyDrive` is already mounted.

## 2. Non-Negotiable Safety Invariants

1. **Zero Secret / PII Commits**: Never commit emails, OAuth tokens, refresh tokens, API keys, `logs/`, `outputs/`, `*.db`, `active_hold.json`, `gpu_monitor_config.json`, or `.venv-desktop/`. Always check `git status` and `git diff --cached` before committing.
2. **Anti-Ghost-Billing**:
   - Any probe allocation NOT intentionally held MUST be unassigned immediately in a `finally` block and verified via `list_assignments()`.
   - Any held VM (`HOLDING` or `CLAIMED_MANUAL`) MUST be persisted to `logs/active_hold.json` and enforced by the independent 1-second `_hold_watchdog_loop()` auto-shutdown timer.
   - Never terminate or disturb pre-existing user Colab VMs during automated testing unless explicitly asked.

## 3. Target Roadmap (Next Milestones)

1. **Multi-Account Management (inspired by Antigravity Tools)**:
   - Visual Accounts Hub in the desktop UI (add/switch/remove/tag multiple Google accounts, OAuth flow, Drive consent status).
   - Aggregated multi-account CCU balance & active VM overview; account selector & failover for GPU sniping.
2. **Modular Google Quota Providers (`providers/`)**:
   - Extract Colab logic into a provider interface so future providers can plug in cleanly:
     - `colab` (CCU, GPU capacity, auto-hold, anti-ghost-billing)
     - `drive_quota` (Google One / Drive storage usage per account)
     - `gemini_api` (AI Studio / Gemini API keys, RPM/RPD quotas, health checks)
     - `gcp_vertex` / additional Google AI credit sources
3. **Cross-Platform & Decoupling**:
   - Make proxy configuration and remote task templates user-configurable in the UI (decouple personal workflows into optional templates).
   - Long-term: move Colab HTTPS/OAuth monitoring & hold logic to pure cross-platform Python so WSL2 is only optional (needed only for `colab dfs mount`).

## 4. Quick Verification Commands

```powershell
# Desktop Python syntax check
.\.venv-desktop\Scripts\python.exe -m py_compile desktop_app\colab_gpu_tools.py

# WSL backend syntax check
wsl.exe -d Ubuntu -e bash -lc 'bash -n scripts/wsl_run_h3.sh && $HOME/.local/share/uv/tools/google-colab-cli/bin/python -m py_compile scripts/colab_gpu_monitor.py scripts/colab_meter.py'
```
