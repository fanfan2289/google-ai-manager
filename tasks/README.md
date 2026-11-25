# Remote task templates

Each `templates/*.json` describes one job the desktop app can run on a **locked** Colab GPU
(`scripts/wsl_run_h3.sh --reuse-session`). The runner always adds the safety flags itself —
session, GPU, account, `--fallback-gpu none` and `--max-minutes` (watchdog) — so a template can
never disable auto-shutdown.

| field | meaning |
| --- | --- |
| `id` | unique id (`[a-z0-9_]+`, must equal the file name) |
| `name` / `description` | shown in the UI |
| `args` | extra static flags passed to `wsl_run_h3.sh` |
| `params[]` | user inputs: `key`, `label`, `flag`, `type` (`path` / `text` / `number` / `shell`), `default`, `required`, `min` / `max` |
| `ui_extras` | optional buttons: `outputs_folder`, `drama_config` |

Forbidden flags (rejected by the loader): `--keep-alive`, `--dry-run`, `--max-minutes`,
`--reuse-session`, `-s/--session`, `-g/--gpu`, `--fallback-gpu`, `-a/--account`.
Add your own template by dropping a JSON file here; no code change is needed.
