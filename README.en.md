# Google AI Manager

> **Disclaimer**: Unofficial open-source community tool. Not affiliated with, endorsed by, or sponsored by Google LLC.
>
> [中文说明](./README.md)

**Google AI Manager** is a Windows desktop app (Edge WebView2 + system tray, backend in WSL2) for
**Google AI Pro** users. It gathers the monthly allowances of one or more Google accounts into one
panel and adds Colab GPU grabbing with strict anti-ghost-billing guards.

Current version: **v2.9**

## Features

- **Overview of the five monthly Pro allowances**
  - Colab compute units: balance, burn rate, running VMs.
  - Flow credits: balance, 24 h / 7 d usage, trend chart, estimated Veo clips left (experimental).
  - Google Cloud credits: $300 free-trial status, Developer Program $10/month, per-credit expiry (experimental).
  - Google One storage: usage and breakdown (Drive files / Gmail & Photos / trash).
  - Antigravity: Gemini and Claude/GPT 5-hour and weekly windows (read from the Antigravity Tools cache).
  - A to-do list highlights holds, self-check issues, unclaimed or expiring credits and low balances.
- **Accounts**: one-click OAuth sign-in (the browser login completes through a localhost callback; no
  code to paste), Drive consent, labels and notes, guarded removal, totals across accounts, and
  failover to backup accounts when grabbing GPUs.
- **Colab** (one entry, four sub-tabs): grab & hold GPUs with auto-shutdown; a session manager for every
  billed VM (open in Colab web, terminal, GPU status, files, restart kernel, stop one VM); run a command
  on the held GPU; probe statistics.
- **Anti-ghost-billing**: probe allocations released in `finally` and verified; persisted holds with a
  1-second watchdog; dead-man watchdog for commands; one-click emergency stop.
- Light / dark theme, proxy settings, first-run self-check, About page.

## Quick start (Windows + WSL2)

1. Desktop shell (Python 3.11+):
   ```powershell
   py -3.12 -m venv .venv-desktop
   .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt
   ```
2. Backend (WSL2 Ubuntu): `wsl.exe -d Ubuntu -e bash scripts/wsl_setup_env.sh`
3. Start `desktop_app\start.bat` (or make a desktop shortcut to it; `--web` for browser mode on `http://127.0.0.1:8765`).
   Command-line tools: `.\scripts\Invoke-GAM.ps1 stop | status | run | auth | monitor`.
4. **Accounts → Add account → Start OAuth**.
5. Optional: enable Flow / Cloud credit reading in **Settings → Experimental**, then **Connect** on an
   account card and sign in once in the dedicated Edge window.

## Privacy & security

- Credentials stay with `google-colab-cli` / `colab-ext` in WSL (`0600`); the app never handles tokens.
- Flow and Cloud credits are read inside a dedicated per-account browser page; only numbers and states
  leave it. Unofficial and off by default.
- App settings, notes and history live in `~/.google-ai-manager/` (`0700` / `0600`), never in the repo.
- The local API listens on `127.0.0.1` only and refuses cross-origin requests.

## Development

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests
wsl.exe -d Ubuntu -e bash -lc '$HOME/.local/share/uv/tools/google-colab-cli/bin/python -m unittest discover -s tests'
wsl.exe -d Ubuntu -e python3 -u scripts/colab_gpu_monitor.py --port 8766 --no-probe   # safe read-only backend
```

See [AGENTS.md](./AGENTS.md) for architecture and safety invariants. Licensed under MIT.
