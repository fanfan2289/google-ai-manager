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
  - Serves `scripts/monitor_ui.html`, REST APIs (`/api/health`, `/api/state`, `/api/probe-now`, `/api/grab-now`, `/api/hold/*` incl. `run-command` / `mount-drive`, `/api/job/{log,stop}`, `/api/stop-all`, `/api/shutdown`), and SSE (`/api/events`) on `127.0.0.1:8765`.
  - Persists probe history in `logs/gpu_monitor.db`, user config in `logs/gpu_monitor_config.json`, and active lock state in `logs/active_hold.json`.
- **Quota Providers (`providers/`)**
  - `base.py`: `QuotaProvider` interface → `ProviderSnapshot` of generic `Metric`s (read-only, never carry tokens). `ProviderHub` (`__init__.py`) caches per (provider, account), serialises refreshes and converts exceptions into `status="error"` snapshots.
  - `colab.py` (CCU / burn rate / live VMs / GPU eligibility), `google_storage.py` (official Drive v3 `about.storageQuota`, reuses the per-account DriveFS refresh token).
  - `netcfg.py`: the single source of truth for proxy settings (`auto` / `manual` / `off`), stored in `~/.google-ai-manager/settings.json` (`0600`). Python code calls `netcfg.apply_proxy_env()`; shell scripts run `eval "$(python3 providers/netcfg.py shell-env)"`. Never hard-code proxy ports elsewhere.
  - `paths.py`: user-private app home `~/.google-ai-manager/` (override with `GAM_HOME`). All user settings / labels / future credentials go here, never into the repo.
  - REST: `GET /api/providers[?account=&refresh=1]`, `GET /api/settings`, `POST /api/settings/proxy`, `POST /api/settings/proxy/test`.
- **Accounts Hub (`providers/accounts_hub.py`)**
  - Reads colab-ext's registry in-process (offline); every write (OAuth `--url-only` / `--code`, DriveFS consent, removal) goes through the public `colab` CLI in a subprocess — the app never handles tokens.
  - "Current account" is the monitor's own `config["account"]` (`None` = follow the CLI's active account). Never call `colab account use` from the app: the CLI's global active account is shared with other tools / older backends.
  - Switching is refused while a hold or cloud job is active and waits for `_probe_lock` (probe cycles bind `(account, State)` once). Removal is refused for the current account, a holding account, or one with live VMs; unverifiable accounts need an explicit second confirmation.
  - Labels / notes / tags / colour: `~/.google-ai-manager/accounts.json` (`0600`).
  - REST: `GET /api/accounts`, `GET /api/accounts/overview[?refresh=1]`, `POST /api/accounts/{add-start,login-start,login-finish,drive-start,drive-finish,use,remove,meta}`. All POSTs reject cross-origin browser requests (`Origin` ≠ `Host`).
  - `release_hold()` kills on `hold["account"]`, never on whatever account is configured now.
- **Multi-account safety & failover (M3)**
  - While a GPU is held, `refresh_account_snapshot()` (watchdog 25 s sync, release, post-lock) follows `hold["account"]`; the "VM disappeared externally" check therefore never compares against another account.
  - `emergency_stop_all()` targets hold account + configured account + last probe account by default; `{"all_accounts": true}` covers every profile; `{"accounts": [...]}` an explicit list. The hold record is cleared only when *its* account verifies 0 remaining VMs.
  - Failover (`failover_on`, ordered `failover_accounts`, `min_ccu_threshold`): `choose_probe_account()` (pure, in `accounts_hub.py`) picks the account per probe cycle / grab under `_probe_lock`. 429 / 412 / 401-403 block only that account (`ACCOUNT_BLOCK_SECONDS`); the global back-off applies only when failover is off or every candidate is blocked.
  - Engine tests with mocked Colab: `tests/test_engine_accounts.py` (run with the google-colab-cli venv python in WSL; skipped elsewhere).
- **M4: more providers, benefits center, self-check, task templates**
  - `antigravity.py` reads the Antigravity Tools cache (`<Windows home>/.antigravity_tools`, found via `paths.windows_home()`); the per-account `token` block is dropped on load and must never be used. Never open another tool's credential files. (The Gemini CLI provider was removed in v2.5: AI Pro logins moved to Antigravity CLI on 2026-06-18.)
  - `benefits.py`: static Pro catalog + `EXTERNAL_URL_PREFIXES`, which must equal the desktop shell's allow-list (`JsBridge.open_external_url`; a test enforces it).
  - `selfcheck.py` + `MonitorEngine.self_check()` → `GET /api/selfcheck`; `GET /api/benefits`.
- **v2.3: generic runner, no drama-specific code in the app**
  - `remote_runner.py` builds the argv for `scripts/wsl_run_remote.sh` → `POST /api/hold/run-command {command, max_minutes, mount_drive}`. The runner owns session / GPU / account / `--fallback-gpu none` / `--max-minutes` / `--reuse-session`; the user command is passed as the single argv element after `-c` (never through a local shell).
  - `wsl_run_remote.sh` requires `-c`; Drive is mounted only with `--mount-drive` (DriveFS credentials are checked only then).
  - Drive auto-mount after a hold defaults to off (`auto_mount_drive`); `migrate_config()` turns it off once for configs saved before `config_version` 3 and renames the legacy `h3work` session default to `gpuwork`.
  - Colab session manager: `colab_sessions.py` (pure validation + payload) → `GET /api/colab/sessions[?refresh=1]`, `POST /api/colab/session/{stop,url,info,ls,log,download,upload,restart-kernel}`. Every CLI call is `colab -a <registered account> <cmd>` with argv only; session names / remote paths are validated (no leading `-`), the session must be live for that account. `stop` unassigns exactly one endpoint (`colab_meter._terminate_endpoint`); stopping the held GPU goes through `release_hold()`. Downloads land in git-ignored `downloads/<account>/<session>/`. New sessions reuse `POST /api/grab-now` with a mandatory `hold_minutes`, so they are always watchdog-protected. Desktop bridge: `open_session_terminal` (new console, regexes must equal `colab_sessions`; a test enforces it), `choose_upload_file` (pywebview dialog), `open_downloads_folder`.
- **v2.4: Google Flow credits (experimental, `flow_credits_on`, default off)**
  - Reader: `desktop_app/flow_credits.py` (Windows). One dedicated Edge user-data-dir per account under `%LOCALAPPDATA%\GoogleAIManager\flow\<account>`; `login` opens a normal window at flow.google.com, `read` starts it headless with a 127.0.0.1 DevTools port, navigates to flow.google.com and runs `PAGE_SCRIPT` in the page: `WIZ_global_data.SNlM0e` (per-document `at`) → POST `/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=nzlxg` → `[0]` is the balance. The page returns only the balance and an `email_match` flag; never return or log the `at` token, cookies or e-mail. (The old labs.google `/fx/api/auth/session` → aisandbox `/v1/credits` path answers 410 since 2026-10-22.)
  - A browser left running on the profile (Edge background mode) blocks headless launch: `close_profile_browser` closes it gracefully first (so cookies flush), then forcibly. It only ever targets processes whose `--user-data-dir` is the dedicated profile.
  - Desktop: `JsBridge.flow_{status,connect,refresh,disconnect}` + `_flow_loop` (every 30 min when enabled) → `POST /api/flow/report` (numbers only; `providers/flow_credits.validate_report` drops everything else) / `POST /api/flow/forget`; `GET /api/flow`. Backend store `~/.google-ai-manager/flow_credits.json` (0600); provider `flow_credits` feeds the benefits center and account cards.
- **v2.5: Google Cloud credits (experimental, `gcp_credits_on`, default off)**
  - Same dedicated profile, same launch (`flow_credits.read_account(..., gcp=True)`): Developer Program benefits page (`me.developers.google.com/benefits`) → program state; Cloud console `/billing` → billing account ids (the console renders inside shadow roots: use the `DEEP_JS` helpers; readyState may never reach `complete`), then `/billing/<id>/credits[/all]` rows with money amounts. Only `state`, counts and amounts are reported (`POST /api/gcp/report`, `providers/gcp_credits.validate_gcp_report`); billing ids never leave the page. The "active" (claimed) path is untested until a billing account exists.
  - Desktop refresh: Flow every 30 min, Cloud credits every 6 h, one browser launch when both are due.
  - Replacing an older backend: the desktop shell waits until port 8765 is free (the old backend finishes its in-flight probe first).
- **v2.6 UI** (top app bar since v2.7.1 — the sidebar is gone; nav items are `.topnav .nav-item[data-view]`, the five-line summary lives in the `#miniPanel` dropdown of the CCU chip): default view `overview` (five allowance cards from `/api/accounts/overview` + a to-do list built from state, self-check and provider snapshots); nav groups 概览 / Colab 算力 / AI 额度 / 系统; `stats` view merges the heatmap and history (`setStatsTab`); Colab-only controls (`topModePill`, `topNextPill`, `btnProbeNow`, `btnTogglePause`) live in the `dashboard` view's `.colab-bar`. Window title / `APP_TITLE` is "Google AI Manager" (the single-instance mutex name is unchanged).
- **v2.7 one-click OAuth**: `AccountsHub.start_oauth(kind=add|login|drive)` → `POST /api/accounts/oauth-{start,status,cancel,code}`. Colab login runs `scripts/gam_oauth_loopback.py` (colab venv): same Cloud SDK client and scopes as the CLI but redirect `http://localhost:<port>/` (the client accepts loopback, as gcloud does), writes colab-ext's normal pending-login file, listens on 127.0.0.1 (WSL localhost forwarding makes it reachable from the Windows browser), checks `state`, then finishes with the public `colab account login <name> --code` — no token ever passes through the app. Drive uses `colab drive consent --flavor dfs --no-browser` (built-in loopback). "add" registers a temporary `gam-new-*` profile, renames it after the Google e-mail (`derive_account_name`), and removes it if the flow fails or is cancelled. Pasting the address-bar URL (`oauth-code`) finishes through the same pending file.
- **v2.8**: one `colab` nav entry → `COLAB_VIEWS` sub-tabs (`#colabTabs`, last sub-view in localStorage). Flow: `FlowHistory` (`~/.google-ai-manager/flow_history.json`, 90 days, one point per 6 h when unchanged), `usage_stats` (decreases only), `GET /api/flow/history`, `flow_low_threshold` toast on crossing. Cloud: reports carry `trial_state` and a sanitised `credits` list (kind / remaining / original / expires YYYY-MM-DD); `expiring()` → once-a-day toast 14 days before expiry. Drive: UI-only breakdown from `storage_drive / storage_trash / storage_other`. Reminders go through `MonitorEngine._notify` (respects `windows_toast`).
- **Settings / About (v2.9)**: settings panes `.set-pane[data-pane]` + `setSettingsTab()`; `GET /static/<name>.png|svg` serves `scripts/static/` (flat names only, `STATIC_RE`). The column layout needs `min-height: 0` on `.main-area` / `.content-scroll` or long pages cannot scroll to the bottom.
- **Themes (v2.8.1)**: `:root[data-theme="light"]` (default) / dark (original palette). Colours go through CSS variables (`--text-strong`, `--text-blue/amber/crimson/emerald`, `--fill-deep`, `--toast-*`, `--log-*`, `--nav-active-*`); do not hard-code hex text colours in new UI. The theme is set before first paint from `localStorage.gamTheme`; `toggleTheme()` switches it.
  - `cloud_workstation/` is an optional example workflow (ComfyUI + MiniMax-H3 drama): `run_drama.sh` wraps the generic runner; `cloud_workstation/local/` holds local-only helpers and is skipped by the Drive sync. Never add drama-specific code to the app, backend or desktop shell.

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
   - Keep personal workflows out of the app: they run through the generic command runner (`scripts/wsl_run_remote.sh`).
   - Long-term: move Colab HTTPS/OAuth monitoring & hold logic to pure cross-platform Python so WSL2 is only optional (needed only for `colab dfs mount`).

## 4. Quick Verification Commands

```powershell
# Desktop Python syntax check
.\.venv-desktop\Scripts\python.exe -m py_compile desktop_app\colab_gpu_tools.py

# WSL backend syntax check
wsl.exe -d Ubuntu -e bash -lc 'bash -n scripts/wsl_run_remote.sh cloud_workstation/run_drama.sh && $HOME/.local/share/uv/tools/google-colab-cli/bin/python -m py_compile scripts/colab_gpu_monitor.py scripts/colab_meter.py'

# Offline unit tests (no network / Colab / credentials)
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -v

# + engine tests (mocked Colab) inside the google-colab-cli venv
wsl.exe -d Ubuntu -e bash -lc '$HOME/.local/share/uv/tools/google-colab-cli/bin/python -m unittest discover -s tests'

# Safe live test: read-only backend on a spare port (never probes or allocates GPUs,
# does not disturb the production backend on 8765). Stop it with POST /api/shutdown.
wsl.exe -d Ubuntu -e python3 -u scripts/colab_gpu_monitor.py --port 8766 --no-probe
```

`APP_VERSION` in `scripts/colab_gpu_monitor.py` and `EXPECTED_VERSION` in `desktop_app/colab_gpu_tools.py` must always match (the desktop app restarts a backend whose version differs).
