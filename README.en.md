# Google AI Manager

> **Disclaimer**: Unofficial open-source community tool. Not affiliated with, endorsed by, or sponsored by Google LLC.
>
> [中文说明](./README.md)

**Google AI Manager** is a native Windows desktop app (Edge WebView2 + system tray) with a WSL2 backend
for people who use **Google AI Pro** across one or more Google accounts. It tracks Colab compute units,
grabs scarce Colab GPUs with strict anti-ghost-billing guards, and puts the quotas of the other Pro
tools in one place.

## Features (v2.3)

- **Colab GPU monitor & auto-hold**: probes `G4 / A100 / H100 / L4 / T4` capacity at zero cost (a
  503 costs nothing), optionally holds a GPU on a hit and shuts the VM down
  automatically when nobody claims it (independent 1-second watchdog, persisted hold state); Google
  Drive can be mounted on demand.
- **Anti-ghost-billing**: every probe allocation that is not intentionally held is released in a
  `finally` block and verified; emergency stop kills all VMs (including orphans) and re-checks for 0.
- **Multi-account hub**: add / re-login / Drive-consent Google accounts from the UI (copy & paste
  OAuth code, no CLI needed), labels and notes, guarded removal, aggregated CCU / burn rate / VMs /
  storage across accounts.
- **Failover**: when the primary account is below a CCU threshold, rate-limited (429), at its
  instance cap (412) or unauthorised, probing and grabbing switch to ordered backup accounts. Holds,
  the watchdog, release and emergency stop always act on the account that owns the hold.
- **Google AI Pro benefits center**: live Google One storage, Colab CCU, Antigravity 5-hour / weekly
  windows and Gemini CLI status, plus official entry points for Flow credits, AI credits, Google Cloud
  credits, Firebase Studio and more.
- **Antigravity quota**: read-only from the Antigravity Tools local cache, matched to accounts by
  e-mail — no tokens are ever read or used.
- **Colab session manager**: every live (billed) Colab VM across all accounts, labelled as held by
  this app / CLI / untracked (opened in the browser or orphaned). Per session: open in Colab web,
  open a terminal (`colab ssh`, or `colab repl` without an SSH key), GPU / memory / disk status,
  browse / upload / download files, execution history, restart kernel, and stop just that VM. New
  sessions require an auto-shutdown time and are guarded by the hold watchdog. Feature ideas from
  [colab-playground](https://github.com/scarletkc/colab-playground) and
  [colab-monitor](https://github.com/hohieuai/colab-monitor) (no code reused).
- **Run a command on the held GPU**: one shell command runs on the held session; when it exits the
  VM is stopped and reconciled, and the watchdog kills it at the time limit. The runner owns the
  session / GPU / account / time-limit flags, so a command cannot disable auto-shutdown.
- **Example workflow**: `cloud_workstation/` (ComfyUI + MiniMax-H3 short drama) is kept as a
  command-line example (`cloud_workstation/run_drama.sh`); the app itself has no drama-specific code.
- **Proxy settings** (auto-detect / manual / direct) shared by the backend and all scripts, and a
  **first-run self-check** with fix hints.

## Quick start (Windows + WSL2)

1. Desktop shell (Windows, Python 3.11+):
   ```powershell
   py -3.12 -m venv .venv-desktop
   .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt
   ```
2. Backend (WSL2 Ubuntu): installs `uv`, `google-colab-cli` and `colab-ext`:
   ```powershell
   wsl.exe -d Ubuntu -e bash scripts/wsl_setup_env.sh
   ```
3. Start `5_显卡余量监控看板_Monitor.bat` (add `--web` for browser mode on `http://127.0.0.1:8765`).
4. Open **Accounts → Add account** to sign in, then check **Self-check** to confirm the setup.

## Privacy & security

- Credentials stay where `google-colab-cli` / `colab-ext` keep them in WSL (`0600`). App settings and
  account notes live in `~/.google-ai-manager/` (`0700` / `0600`), never in the repository.
- `logs/`, `outputs/`, `*.db`, `active_hold.json` and `gpu_monitor_config.json` are git-ignored.
- The local API only listens on `127.0.0.1` and refuses cross-origin browser requests.
- Antigravity and Gemini CLI integrations only read local cache / status files.

## Development

```powershell
# Offline unit tests (no network / Colab / credentials)
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -v
# Engine tests with mocked Colab (inside the google-colab-cli venv)
wsl.exe -d Ubuntu -e bash -lc '$HOME/.local/share/uv/tools/google-colab-cli/bin/python -m unittest discover -s tests'
# Safe live test: read-only backend on a spare port (never probes or allocates GPUs)
wsl.exe -d Ubuntu -e python3 -u scripts/colab_gpu_monitor.py --port 8766 --no-probe
```

See [AGENTS.md](./AGENTS.md) for architecture and safety invariants. Licensed under MIT.
