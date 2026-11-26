#!/usr/bin/env python3
"""Colab GPU Availability Monitor & Live Dashboard Server (WSL2 + Windows).

Continuously probes Google Colab GPU capacity (default: G4 96GB & A100) with
zero-billing-leak safety guarantees, persists history in SQLite, sends native
Windows + browser alerts when a GPU becomes available, and serves a real-time
web dashboard at http://127.0.0.1:8765.

Safety & Anti-Ghost-Billing Design:
  - 503 Service Unavailable (out of stock) costs 0.00 CCU and creates 0 VMs.
  - If a probe ever receives 200 OK (GPU in stock!), the server IMMEDIATELY
    unassigns that exact probe endpoint inside a retry-protected `finally`
    block and verifies via `list_assignments()` that it is gone (<1s release).
  - Never touches any pre-existing user Colab VM; skips probing automatically
    when an active VM is already running (configurable).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import PurePosixPath
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
LOGS_DIR = PROJECT_DIR / "logs"
DB_PATH = LOGS_DIR / "gpu_monitor.db"
CONFIG_PATH = LOGS_DIR / "gpu_monitor_config.json"
HOLD_STATE_PATH = LOGS_DIR / "active_hold.json"
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
UI_HTML_PATH = SCRIPT_DIR / "monitor_ui.html"
APP_VERSION = "2.3"

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# User-configurable proxy (~/.google-ai-manager/settings.json); stdlib-only, so it runs before
# the re-exec below and the resolved env is inherited by the colab venv interpreter.
from providers import netcfg  # noqa: E402

netcfg.apply_proxy_env()


def _find_colab_python() -> str | None:
    """Locate the Python interpreter inside uv's `google-colab-cli` tool venv."""
    candidates = [
        Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python",
        Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python3",
    ]
    colab_bin = Path.home() / ".local/bin/colab"
    if colab_bin.is_file():
        try:
            first_line = colab_bin.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            if first_line.startswith("#!"):
                shebang_py = first_line[2:].strip().split()[-1]
                candidates.insert(0, Path(shebang_py))
        except OSError:
            pass
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _reexec_in_colab_venv_if_needed() -> None:
    """Re-exec into google-colab-cli's venv Python if `colab_cli` isn't importable here."""
    try:
        import colab_cli  # noqa: F401
        return
    except ImportError:
        pass

    target_py = _find_colab_python()
    if not target_py or os.path.realpath(target_py) == os.path.realpath(sys.executable):
        sys.stderr.write(
            "[gpu_monitor] ERROR: Cannot import `colab_cli` and could not find "
            "`google-colab-cli` uv tool venv.\n"
            "Please run setup first: `uv tool install google-colab-cli`\n"
        )
        sys.exit(2)

    os.execv(target_py, [target_py, *sys.argv])


_reexec_in_colab_venv_if_needed()

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Inside google-colab-cli's Python environment
import requests  # noqa: E402
import colab_cli.cli  # noqa: E402, F401
from colab_cli.client import (  # noqa: E402
    Accelerator,
    Assignment,
    ColabRequestError,
    PostAssignmentResponse,
    Shape,
    Variant,
    resolve_assign_shape,
)
from colab_cli.state import SessionState  # noqa: E402
from colab_cli.utils import get_status_code  # noqa: E402

import colab_meter  # noqa: E402
from providers import PLANNED_PROVIDERS, ProviderHub  # noqa: E402
from providers import colab as colab_provider  # noqa: E402
from providers.google_storage import GoogleStorageProvider  # noqa: E402
from providers import benefits as benefits_catalog  # noqa: E402
from providers import paths as app_paths  # noqa: E402
from providers import selfcheck  # noqa: E402
from providers import remote_runner  # noqa: E402
from providers import colab_sessions  # noqa: E402
from providers.antigravity import AntigravityProvider  # noqa: E402
from providers.gemini_cli import GeminiCliProvider  # noqa: E402
from providers.accounts_hub import (  # noqa: E402
    AccountsError,
    AccountsHub,
    aggregate_account_totals,
    choose_probe_account,
)


GPU_CATALOG: list[dict[str, Any]] = [
    {
        "id": "G4",
        "label": "G4 (RTX PRO 6000)",
        "vram": "96 GB",
        "ccu_hint": "~10–14 CCU/hr",
        "tier": "primary",
        "note": "满血 96GB 显存 · 大模型首选",
        "default_enabled": True,
    },
    {
        "id": "A100",
        "label": "A100",
        "vram": "40 / 80 GB",
        "ccu_hint": "~7–12 CCU/hr",
        "tier": "secondary",
        "note": "40 / 80GB 显存 · 高速算力",
        "default_enabled": True,
    },
    {
        "id": "H100",
        "label": "H100",
        "vram": "80 GB",
        "ccu_hint": "~15+ CCU/hr",
        "tier": "high",
        "note": "旗舰级 GPU · 极低供应",
        "default_enabled": False,
    },
    {
        "id": "L4",
        "label": "L4",
        "vram": "24 GB",
        "ccu_hint": "~5 CCU/hr",
        "tier": "fallback",
        "note": "常年有货（开启探测将秒开秒释）",
        "default_enabled": False,
    },
    {
        "id": "T4",
        "label": "T4",
        "vram": "16 GB",
        "ccu_hint": "~1.2 CCU/hr",
        "tier": "entry",
        "note": "入门卡常年有货（开启探测将秒开秒释）",
        "default_enabled": False,
    },
]
GPU_IDS = [g["id"] for g in GPU_CATALOG]
GPU_META_BY_ID = {g["id"]: g for g in GPU_CATALOG}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled_gpus": ["G4", "A100"],
    "interval_seconds": 60,
    "hit_cooldown_seconds": 600,
    "pause_gpu_on_hit": False,
    "skip_when_active_vm": True,
    "windows_toast": True,
    "browser_sound": True,
    "browser_notify": True,
    "account": None,
    # 命中即锁定（自动占卡 + 超时自动关机；云盘默认不自动挂载，可在占卡卡片上手动挂载）
    "hold_on_hit": True,
    "hold_gpus": ["G4", "A100"],
    "hold_minutes": 15,
    "auto_mount_drive": False,
    "hold_session_name": "gpuwork",
    "manual_claim_max_minutes": 120,
    # 多账号自动切换（M3）：主号 CCU 低于阈值 / 429 限流 / 412 实例上限 / 授权失效时换用备用号
    "failover_on": False,
    "failover_accounts": [],
    "min_ccu_threshold": 5.0,
}

# Saved-config schema version. v3 (app 2.3): Drive auto-mount defaults to off and the default
# hold session name changed from the old drama-specific "h3work" to "gpuwork".
CONFIG_VERSION = 3
LEGACY_SESSION_NAME = "h3work"


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Upgrade a saved config dict in place; returns (config, changed)."""
    if int(raw.get("config_version") or 0) >= CONFIG_VERSION:
        return raw, False
    raw["auto_mount_drive"] = False
    if str(raw.get("hold_session_name") or "").strip() in ("", LEGACY_SESSION_NAME):
        raw["hold_session_name"] = DEFAULT_CONFIG["hold_session_name"]
    raw["config_version"] = CONFIG_VERSION
    return raw, True


# Per-account "blocked" windows used by failover (seconds), keyed by probe status.
ACCOUNT_BLOCK_SECONDS = {"RATE_LIMITED": 180.0, "LIMIT_REACHED": 300.0, "AUTH_ERROR": 600.0}


def _now_ts() -> float:
    return time.time()


def _clamp_float(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _clean_account_list(raw: Any, allowed: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = str(item or "").strip()
        if name and name not in out and (allowed is None or name in allowed):
            out.append(name)
    return out[:10]


def _now_iso(ts: float | None = None) -> str:
    d = dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=dt.timezone.utc).astimezone()
    return d.isoformat(timespec="seconds")


def _local_hour(ts: float | None = None) -> int:
    d = dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=dt.timezone.utc).astimezone()
    return d.hour


def _send_windows_toast(title: str, message: str) -> None:
    """Fire a non-blocking native Windows balloon notification + exclamation sound via WSL interop."""
    ps_bin = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not os.path.exists(ps_bin):
        return

    safe_title = title.replace("'", "''").replace("\n", " ")[:120]
    safe_msg = message.replace("'", "''").replace("\n", " ")[:240]
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "[System.Media.SystemSounds]::Exclamation.Play(); "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
        f"$n.BalloonTipTitle = '{safe_title}'; "
        f"$n.BalloonTipText = '{safe_msg}'; "
        "$n.Visible = $true; "
        "$n.ShowBalloonTip(10000); "
        "Start-Sleep -Seconds 6; "
        "$n.Dispose()"
    )

    def _runner() -> None:
        try:
            subprocess.run(
                [ps_bin, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception:
            pass

    threading.Thread(target=_runner, daemon=True).start()


def _open_windows_browser(url: str) -> None:
    """Open URL in the host Windows default browser."""
    for cmd in (
        ["/usr/local/bin/wsl-open-browser", url],
        ["/mnt/c/Windows/system32/cmd.exe", "/c", "start", "", url],
    ):
        if os.path.exists(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


class ProbeStore:
    """SQLite persistence for probe history and hourly availability analytics."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    ts_iso TEXT NOT NULL,
                    hour_local INTEGER NOT NULL,
                    account TEXT NOT NULL,
                    gpu TEXT NOT NULL,
                    status TEXT NOT NULL,
                    http_code INTEGER,
                    latency_ms INTEGER,
                    released INTEGER DEFAULT 0,
                    release_ms INTEGER,
                    ccu_balance REAL,
                    burn_rate REAL,
                    active_vms INTEGER,
                    detail TEXT
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_ts ON probe_logs(ts DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_gpu_ts ON probe_logs(gpu, ts DESC);")
            conn.commit()

    def insert(self, rec: dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO probe_logs (
                    ts, ts_iso, hour_local, account, gpu, status, http_code,
                    latency_ms, released, release_ms, ccu_balance, burn_rate,
                    active_vms, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["ts"],
                    rec["ts_iso"],
                    rec["hour_local"],
                    rec["account"],
                    rec["gpu"],
                    rec["status"],
                    rec.get("http_code"),
                    rec.get("latency_ms"),
                    rec.get("released", 0),
                    rec.get("release_ms"),
                    rec.get("ccu_balance"),
                    rec.get("burn_rate"),
                    rec.get("active_vms"),
                    rec.get("detail", ""),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_history(
        self,
        *,
        limit: int = 120,
        gpu: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        clauses = []
        params: list[Any] = []
        if gpu and gpu in GPU_META_BY_ID:
            clauses.append("gpu = ?")
            params.append(gpu)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM probe_logs {where} ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_gpu_summaries(self, since_ts: float) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._lock, self._connect() as conn:
            for gpu_id in GPU_IDS:
                latest_row = conn.execute(
                    "SELECT * FROM probe_logs WHERE gpu = ? ORDER BY id DESC LIMIT 1",
                    (gpu_id,),
                ).fetchone()
                last_hit_row = conn.execute(
                    "SELECT ts, ts_iso FROM probe_logs WHERE gpu = ? AND status = 'AVAILABLE' ORDER BY id DESC LIMIT 1",
                    (gpu_id,),
                ).fetchone()
                agg = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'AVAILABLE' THEN 1 ELSE 0 END) AS hits,
                        SUM(CASE WHEN status = 'SOLD_OUT' THEN 1 ELSE 0 END) AS sold_out,
                        AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) AS avg_latency_ms
                    FROM probe_logs
                    WHERE gpu = ? AND ts >= ?
                    """,
                    (gpu_id, since_ts),
                ).fetchone()
                recent_ticks = conn.execute(
                    """
                    SELECT ts, ts_iso, status, http_code, latency_ms
                    FROM probe_logs
                    WHERE gpu = ?
                    ORDER BY id DESC
                    LIMIT 36
                    """,
                    (gpu_id,),
                ).fetchall()

                # Count consecutive misses since last AVAILABLE
                consec_misses = 0
                recent_statuses = conn.execute(
                    "SELECT status FROM probe_logs WHERE gpu = ? ORDER BY id DESC LIMIT 200",
                    (gpu_id,),
                ).fetchall()
                for r in recent_statuses:
                    if r["status"] == "AVAILABLE":
                        break
                    if r["status"] in ("SOLD_OUT", "BUSY", "RATE_LIMITED"):
                        consec_misses += 1

                out[gpu_id] = {
                    "latest": dict(latest_row) if latest_row else None,
                    "last_available_ts": last_hit_row["ts"] if last_hit_row else None,
                    "last_available_iso": last_hit_row["ts_iso"] if last_hit_row else None,
                    "window_total": int(agg["total"] or 0),
                    "window_hits": int(agg["hits"] or 0),
                    "window_sold_out": int(agg["sold_out"] or 0),
                    "avg_latency_ms": round(float(agg["avg_latency_ms"]), 0) if agg["avg_latency_ms"] is not None else None,
                    "consecutive_misses": consec_misses,
                    "recent_ticks": [dict(r) for r in reversed(recent_ticks)],
                }
        return out

    def get_analytics(self, hours: int = 24) -> dict[str, Any]:
        hours = max(1, min(hours, 168))
        since_ts = time.time() - hours * 3600.0
        with self._lock, self._connect() as conn:
            heatmap: dict[str, list[dict[str, Any]]] = {}
            for gpu_id in GPU_IDS:
                rows = conn.execute(
                    """
                    SELECT
                        hour_local,
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'AVAILABLE' THEN 1 ELSE 0 END) AS hits,
                        SUM(CASE WHEN status = 'SOLD_OUT' THEN 1 ELSE 0 END) AS sold_out,
                        AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) AS avg_ms
                    FROM probe_logs
                    WHERE gpu = ? AND ts >= ?
                    GROUP BY hour_local
                    """,
                    (gpu_id, since_ts),
                ).fetchall()
                by_hour = {int(r["hour_local"]): dict(r) for r in rows}
                slots = []
                for h in range(24):
                    item = by_hour.get(h)
                    total = int(item["total"]) if item else 0
                    hits = int(item["hits"]) if item else 0
                    slots.append(
                        {
                            "hour": h,
                            "total": total,
                            "hits": hits,
                            "sold_out": int(item["sold_out"]) if item else 0,
                            "hit_rate": round((hits / total) * 100.0, 1) if total > 0 else None,
                            "avg_ms": round(float(item["avg_ms"]), 0) if item and item["avg_ms"] is not None else None,
                        }
                    )
                heatmap[gpu_id] = slots

            series_rows = conn.execute(
                """
                SELECT ts, ts_iso, gpu, status, latency_ms, ccu_balance
                FROM probe_logs
                WHERE ts >= ?
                ORDER BY id DESC
                LIMIT 120
                """,
                (since_ts,),
            ).fetchall()

            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'AVAILABLE' THEN 1 ELSE 0 END) AS hits,
                    AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) AS avg_ms
                FROM probe_logs
                WHERE ts >= ?
                """,
                (since_ts,),
            ).fetchone()

            last_any_hit = conn.execute(
                "SELECT gpu, ts, ts_iso FROM probe_logs WHERE status = 'AVAILABLE' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        return {
            "hours": hours,
            "heatmap": heatmap,
            "series": [dict(r) for r in reversed(series_rows)],
            "totals": {
                "total_probes": int(totals["total"] or 0),
                "total_hits": int(totals["hits"] or 0),
                "avg_latency_ms": round(float(totals["avg_ms"]), 0) if totals["avg_ms"] is not None else None,
                "last_any_hit": dict(last_any_hit) if last_any_hit else None,
            },
        }

    def clear_all(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM probe_logs;")
            conn.commit()


class SSEBroker:
    """Thread-safe Server-Sent Events pub/sub fan-out for live browser dashboards."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[queue.Queue[str]] = set()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._clients.discard(q)

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        msg = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        with self._lock:
            dead: list[queue.Queue[str]] = []
            for q in self._clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)


class MonitorEngine:
    """Core Colab GPU probing scheduler + zero-leak safety guard."""

    def __init__(
        self,
        store: ProbeStore,
        broker: SSEBroker,
        account: str | None = None,
        *,
        probe_disabled: bool = False,
    ) -> None:
        self.store = store
        self.broker = broker
        # Read-only mode (`--no-probe`): never allocates GPUs; used for safe UI / provider testing.
        self.probe_disabled = probe_disabled
        self.providers = ProviderHub(ttl_seconds=120)
        self.providers.register(colab_provider.ColabProvider())
        self.providers.register(GoogleStorageProvider())
        self.accounts = AccountsHub(colab_bin=self._colab_bin(), env=self._colab_env)
        self.providers.register(AntigravityProvider(email_of=self.accounts.email_of))
        self.providers.register(GeminiCliProvider(email_of=self.accounts.email_of))
        self._selfcheck_cache: tuple[float, dict[str, Any]] | None = None
        # Colab session manager: cached live listing + when each VM endpoint was first seen.
        self._sessions_cache: tuple[float, dict[str, Any]] | None = None
        self._sessions_lock = threading.Lock()
        self._session_first_seen: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._manual_queue: collections.deque[list[str] | None] = collections.deque()

        self.config = self._load_config()
        if account:
            self.config["account"] = account

        self.paused = False
        self.probing = False
        self.current_probe_gpu: str | None = None
        self.next_probe_ts: float = 0.0
        self.rate_limit_until_ts: float = 0.0
        self.cooldown_until: dict[str, float] = {}
        self.critical_alert: str | None = None

        self.started_ts = _now_ts()
        # First balance seen per account this run (sidebar "本次 -x" delta).
        self.session_start_ccu: dict[str, float] = {}
        # Failover bookkeeping: account -> (blocked_until_ts, reason); last chosen probe account.
        self.account_blocks: dict[str, tuple[float, str]] = {}
        self.probe_account: str | None = None
        self.failover_note: str = ""
        self.account_snapshot: dict[str, Any] = {
            "account": self.config.get("account") or "loading...",
            "balance": None,
            "rate_hourly": None,
            "assignments": [],
            "eligible_gpus": [],
            "ineligible_gpus": [],
            "eligible_tpus": [],
            "usage_error": None,
            "updated_at_iso": None,
        }
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._hold_lock = threading.Lock()
        self._job_proc: subprocess.Popen[str] | None = None
        self.last_job_log_path: str | None = None
        self.last_job_status: str | None = None
        self.active_hold: dict[str, Any] | None = self._load_hold_state()
        self._server_shutdown_cb: Any = None

    def _load_hold_state(self) -> dict[str, Any] | None:
        if not HOLD_STATE_PATH.is_file():
            return None
        try:
            raw = json.loads(HOLD_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("endpoint") and raw.get("state") in (
                "HOLDING",
                "CLAIMED_MANUAL",
                "CLAIMED_JOB",
            ):
                return raw
        except Exception:
            pass
        return None

    def _save_hold_state_locked(self) -> None:
        try:
            HOLD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            if self.active_hold is None:
                HOLD_STATE_PATH.unlink(missing_ok=True)
            else:
                HOLD_STATE_PATH.write_text(
                    json.dumps(self.active_hold, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        cfg["config_version"] = CONFIG_VERSION
        migrated = False
        if CONFIG_PATH.is_file():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw, migrated = migrate_config(raw)
                    cfg.update(raw)
            except Exception:
                pass
        # Validate enabled_gpus
        gpus = [g for g in cfg.get("enabled_gpus", []) if g in GPU_META_BY_ID]
        cfg["enabled_gpus"] = gpus or ["G4", "A100"]
        hold_gpus = [g for g in cfg.get("hold_gpus", ["G4", "A100"]) if g in GPU_META_BY_ID]
        cfg["hold_gpus"] = hold_gpus
        cfg["interval_seconds"] = max(20, min(3600, int(cfg.get("interval_seconds", 60))))
        cfg["hit_cooldown_seconds"] = max(0, min(7200, int(cfg.get("hit_cooldown_seconds", 600))))
        cfg["hold_minutes"] = max(5, min(180, int(cfg.get("hold_minutes", 15))))
        cfg["manual_claim_max_minutes"] = max(15, min(360, int(cfg.get("manual_claim_max_minutes", 120))))
        cfg["hold_session_name"] = str(cfg.get("hold_session_name") or "gpuwork").strip() or "gpuwork"
        cfg["failover_on"] = bool(cfg.get("failover_on", False))
        cfg["failover_accounts"] = _clean_account_list(cfg.get("failover_accounts"))
        cfg["min_ccu_threshold"] = _clamp_float(cfg.get("min_ccu_threshold", 5.0), 0.0, 10000.0, 5.0)
        if migrated:
            try:
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return cfg

    def _save_config(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            if "enabled_gpus" in patch and isinstance(patch["enabled_gpus"], list):
                cleaned = [g for g in patch["enabled_gpus"] if g in GPU_META_BY_ID]
                self.config["enabled_gpus"] = cleaned
            if "hold_gpus" in patch and isinstance(patch["hold_gpus"], list):
                cleaned = [g for g in patch["hold_gpus"] if g in GPU_META_BY_ID]
                self.config["hold_gpus"] = cleaned
            if "interval_seconds" in patch:
                self.config["interval_seconds"] = max(20, min(3600, int(patch["interval_seconds"])))
                # Reschedule sooner if new interval is shorter
                now = _now_ts()
                if self.next_probe_ts > now + self.config["interval_seconds"]:
                    self.next_probe_ts = now + self.config["interval_seconds"]
            if "hit_cooldown_seconds" in patch:
                self.config["hit_cooldown_seconds"] = max(0, min(7200, int(patch["hit_cooldown_seconds"])))
            if "hold_minutes" in patch:
                self.config["hold_minutes"] = max(5, min(180, int(patch["hold_minutes"])))
            if "manual_claim_max_minutes" in patch:
                self.config["manual_claim_max_minutes"] = max(
                    15, min(360, int(patch["manual_claim_max_minutes"]))
                )
            if "hold_session_name" in patch:
                val = str(patch["hold_session_name"] or "").strip()
                if val and remote_runner.SESSION_RE.match(val):
                    self.config["hold_session_name"] = val
            if "min_ccu_threshold" in patch:
                self.config["min_ccu_threshold"] = _clamp_float(patch["min_ccu_threshold"], 0.0, 10000.0, 5.0)
            if "failover_accounts" in patch:
                try:
                    known = self.accounts.names()
                except Exception:
                    known = None
                self.config["failover_accounts"] = _clean_account_list(patch["failover_accounts"], known)
            for bool_key in (
                "pause_gpu_on_hit",
                "skip_when_active_vm",
                "windows_toast",
                "browser_sound",
                "browser_notify",
                "hold_on_hit",
                "auto_mount_drive",
                "failover_on",
            ):
                if bool_key in patch:
                    self.config[bool_key] = bool(patch[bool_key])
            self._save_config()
            cfg_copy = dict(self.config)
        self._wake_event.set()
        self.broadcast_state()
        return cfg_copy

    def set_paused(self, paused: bool) -> bool:
        with self._state_lock:
            self.paused = paused
            if not paused:
                self.next_probe_ts = min(self.next_probe_ts, _now_ts() + 2.0)
        self._wake_event.set()
        self.broadcast_state()
        return self.paused

    def clear_cooldown(self, gpu: str | None = None) -> None:
        with self._state_lock:
            if gpu:
                self.cooldown_until.pop(gpu, None)
            else:
                self.cooldown_until.clear()
        self.broadcast_state()

    def _ensure_probe_allowed(self) -> None:
        if self.probe_disabled:
            raise ValueError("当前后端以只读模式运行（--no-probe），已禁用探测与抢卡")

    def trigger_manual_probe(self, gpu: str | None = None) -> None:
        self._ensure_probe_allowed()
        if gpu and gpu not in GPU_META_BY_ID:
            raise ValueError(f"Unsupported GPU: {gpu}")
        self._manual_queue.append([gpu] if gpu else None)
        self._wake_event.set()

    def primary_account_name(self) -> str:
        """The account chosen in the Accounts Hub (or the CLI active one), independent of failover."""
        return str(self._get_account_state()[0])

    def _primary_name_safe(self) -> str | None:
        try:
            return self.primary_account_name()
        except Exception:
            return self.config.get("account")

    def current_account_name(self) -> str:
        with self._state_lock:
            name = self.account_snapshot.get("account")
        if name and name != "loading...":
            return str(name)
        return str(colab_provider.resolve_account_state(self.config.get("account"))[0])

    def providers_payload(self, account: str | None = None, *, force: bool = False) -> dict[str, Any]:
        acct = (account or "").strip() or self.current_account_name()
        return {
            "account": acct,
            "providers": self.providers.describe_all(),
            "snapshots": self.providers.snapshot_many(acct, force=force),
            "planned": PLANNED_PROVIDERS,
        }

    def _get_account_state(self, account: str | None = None):
        """(name, State) for `account`, defaulting to the configured / CLI-active account."""
        states = colab_meter._iter_states(account or self.config.get("account"))
        return states[0]

    # ------------------------------------------------------------------ Accounts Hub (M2)
    def _job_running(self) -> bool:
        proc = self._job_proc
        return proc is not None and proc.poll() is None

    def accounts_payload(self) -> dict[str, Any]:
        listing = self.accounts.list_accounts()
        with self._state_lock:
            hold = dict(self.active_hold) if self.active_hold else None
        return {
            **listing,
            "current": self.primary_account_name(),
            "probe_account": self.probe_account,
            "pinned": self.config.get("account"),
            "hold_account": hold.get("account") if hold else None,
            "job_running": self._job_running(),
        }

    def use_account(self, name: str | None) -> dict[str, Any]:
        """Point monitoring / grabbing at `name` (None = follow the CLI's active account).

        Only this app's config changes; the colab CLI's global active account is left alone
        because other tools (and older backends) follow it.
        """
        name = (str(name).strip() or None) if name is not None else None
        if name is not None:
            name = self.accounts.require_existing(name)
        with self._hold_lock:
            with self._state_lock:
                if self.active_hold is not None:
                    raise AccountsError("当前有锁定中的 GPU 实例，请先释放后再切换账号")
            if self._job_running():
                raise AccountsError("云端任务运行中，请结束后再切换账号")
            # Wait for an in-flight probe cycle: it binds (account, State) once per cycle.
            if not self._probe_lock.acquire(timeout=45):
                raise AccountsError("探测进行中，请稍后再试")
            try:
                if name is None:
                    self.config.pop("account", None)
                else:
                    self.config["account"] = name
                self._save_config()
                with self._state_lock:
                    self.cooldown_until.clear()
                    self.account_blocks.clear()
                    self.failover_note = ""
                    self.account_snapshot = {**self.account_snapshot, "account": name or "loading..."}
            finally:
                self._probe_lock.release()
        try:
            self.refresh_account_snapshot()
        except Exception as exc:
            print(f"[{_now_iso()}] [gpu_monitor] 切换账号后刷新失败: {exc}", file=sys.stderr, flush=True)
        self.broadcast_state()
        return {"ok": True, "current": self.primary_account_name(), "pinned": self.config.get("account")}

    def remove_account(self, name: str, *, confirm: str, allow_unverified: bool = False) -> dict[str, Any]:
        name = self.accounts.require_existing(name)
        if str(confirm or "").strip() != name:
            raise AccountsError("确认名称不匹配")
        if name == self.primary_account_name():
            raise AccountsError("不能删除当前正在使用的账号，请先切换到其他账号")
        with self._state_lock:
            if self.active_hold and self.active_hold.get("account") == name:
                raise AccountsError("该账号有锁定中的 GPU 实例，请先释放")
        snap = self.providers.snapshot("colab", name, force=True).to_dict()
        if snap.get("status") == "ok":
            vms = next((m.get("used") for m in snap.get("metrics", []) if m.get("key") == "active_vms"), 0) or 0
            if vms > 0:
                raise AccountsError(f"该账号仍有 {int(vms)} 个运行中的 Colab VM，请先强杀后再删除")
        elif not allow_unverified:
            return {
                "ok": False,
                "needs_confirm": True,
                "detail": "无法确认该账号是否还有运行中的 VM（" + str(snap.get("error") or snap.get("status")) + "）。"
                "删除后本工具将无法再管理它的实例。",
            }
        res = self.accounts.remove(name)
        self.providers.invalidate(name)
        with self._state_lock:
            if name in (self.config.get("failover_accounts") or []):
                self.config["failover_accounts"] = [n for n in self.config["failover_accounts"] if n != name]
                self._save_config()
            self.account_blocks.pop(name, None)
        return res

    # ------------------------------------------------------------------ Pro benefits center (M4)
    def benefits_payload(self, account: str | None = None, *, force: bool = False) -> dict[str, Any]:
        acct = (account or "").strip() or self.primary_account_name()
        ids = [pid for pid in benefits_catalog.tracked_provider_ids() if pid in self.providers.ids()]
        snaps = self.providers.snapshot_many(acct, ids, force=force)
        items = []
        for b in benefits_catalog.catalog():
            b["snapshot"] = snaps.get(b.get("provider") or "")
            items.append(b)
        ag = self.providers.get("antigravity")
        known = {(self.accounts.email_of(n) or "").lower() for n in self.accounts.names()}
        ag_rows = []
        for snap in ag.all_accounts():
            snap["in_hub"] = str((snap.get("extra") or {}).get("email") or "").lower() in known
            ag_rows.append(snap)
        return {"account": acct, "plans_url": benefits_catalog.PLANS_URL, "items": items, "antigravity_accounts": ag_rows}

    # ------------------------------------------------------------------ first-run self check (M4)
    def self_check(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._selfcheck_cache
        if cached and not force and time.time() - cached[0] < 60:
            return cached[1]
        import importlib.metadata as md

        def colab_cli():
            try:
                return selfcheck.OK, f"google-colab-cli {md.version('google-colab-cli')}"
            except md.PackageNotFoundError:
                return selfcheck.WARN, "已可导入 colab_cli，但未找到包版本信息"

        def colab_ext():
            from colab_ext import accounts as _acc  # noqa: F401

            return selfcheck.OK, "colab-ext 多账号扩展已安装"

        def accounts():
            rows = self.accounts.list_accounts()["accounts"]
            ready = [r for r in rows if r["auth"]["ready"] is True]
            if not ready:
                return selfcheck.FAIL, f"{len(rows)} 个账号，均未完成 Colab 授权", "打开「账号管理」→ 添加账号 / 登录 Colab"
            return selfcheck.OK, f"{len(ready)}/{len(rows)} 个账号已授权 Colab"

        def drive():
            rows = self.accounts.list_accounts()["accounts"]
            ready = [r["name"] for r in rows if r["drive"]["ready"]]
            bad = [n for n in ready if self.providers.snapshot("google_storage", n).status == "unauthorized"]
            # Without auto-mount, Drive consent is only needed for manual mounts / storage quota.
            level = selfcheck.WARN if self.config.get("auto_mount_drive") else selfcheck.INFO
            if not ready:
                return level, "没有账号授权 Google Drive（无法挂载云盘、无法显示存储额度）", "「账号管理」→ 授权云盘"
            if bad:
                return level, f"{len(bad)} 个账号的云盘授权缺少 Drive 权限（挂载云盘与存储额度需要）", "在账号卡片点「重新授权云盘」并勾选 Drive 权限"
            return selfcheck.OK, f"{len(ready)}/{len(rows)} 个账号已授权云盘"

        def network():
            r = netcfg.resolve_proxy()
            res = netcfg.test_proxy(r["url"])
            via = r["detail"] or ("直连" if not r["url"] else r["url"])
            if res.get("ok"):
                return selfcheck.OK, f"可访问 Google（{via}，{res.get('latency_ms', '-')} ms）"
            return selfcheck.FAIL, f"无法访问 Google（{via}）：{res.get('error') or '未知错误'}", "「设置」→ 网络代理：切换模式或填写手动代理后点测试"

        def colab_api():
            with self._state_lock:
                snap = dict(self.account_snapshot)
            if snap.get("usage_error"):
                return selfcheck.WARN, f"Colab 额度查询失败：{snap['usage_error']}", "检查代理或重新登录该账号"
            if isinstance(snap.get("balance"), (int, float)):
                return selfcheck.OK, f"账号 {snap.get('account')} 余额 {float(snap['balance']):.2f} CCU"
            return selfcheck.INFO, "Colab 额度尚未加载（后端刚启动时属正常）"

        def app_home():
            home = app_paths.ensure_app_home()
            if os.name != "posix":
                return selfcheck.OK, str(home)
            mode = os.stat(home).st_mode & 0o777
            if mode & 0o077:
                return selfcheck.WARN, f"{home} 权限为 {oct(mode)}，其他用户可读", f"chmod 700 {home}"
            return selfcheck.OK, f"{home}（0700）"

        def logs_dir():
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            probe = LOGS_DIR / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return selfcheck.OK, "logs/ 可写（探测记录、配置与占卡状态）"

        def win_home():
            h = app_paths.windows_home()
            if h:
                return selfcheck.OK, f"Windows 用户目录：{h}"
            return selfcheck.INFO, "未检测到 Windows 用户目录（非 WSL 环境时可忽略）", "可设置环境变量 GAM_WIN_HOME"

        def antigravity():
            ok, reason = self.providers.get("antigravity").availability()
            if ok:
                n = len(self.providers.get("antigravity").list_accounts())
                return selfcheck.OK, f"Antigravity Tools 本地缓存可读（{n} 个账号）"
            return selfcheck.INFO, reason, "可选：安装并登录 Antigravity Tools 以显示 Antigravity 额度"

        def gemini():
            p = self.providers.get("gemini_cli")
            if p.installed():
                return selfcheck.OK, "已安装 Gemini CLI" + ("，已登录" if p.login_home() else "，未登录")
            return selfcheck.INFO, "未安装 Gemini CLI" + ("（检测到旧登录记录）" if p.login_home() else ""), "可选：npm install -g @google/gemini-cli"

        def hold_state():
            with self._state_lock:
                hold = dict(self.active_hold) if self.active_hold else None
            if hold:
                return selfcheck.INFO, f"有占卡中的实例：{hold.get('gpu')}（账号 {hold.get('account') or '-'}），看门狗守护中"
            return selfcheck.OK, "当前无占卡实例"

        result = selfcheck.run_checks([
            ("colab_cli", "google-colab-cli", colab_cli),
            ("colab_ext", "colab-ext 多账号扩展", colab_ext),
            ("accounts", "Colab 账号授权", accounts),
            ("drive", "Google Drive 云盘授权", drive),
            ("network", "网络与代理", network),
            ("colab_api", "Colab 额度接口", colab_api),
            ("app_home", "用户私有目录", app_home),
            ("logs", "本地数据目录", logs_dir),
            ("win_home", "Windows 用户目录", win_home),
            ("antigravity", "Antigravity Tools（可选）", antigravity),
            ("gemini_cli", "Gemini CLI（可选）", gemini),
            ("hold", "占卡与防扣费守护", hold_state),
        ])
        result["version"] = APP_VERSION
        result["probe_disabled"] = self.probe_disabled
        self._selfcheck_cache = (time.time(), result)
        return result

    def accounts_overview(self, *, force: bool = False) -> dict[str, Any]:
        """Per-account Colab + storage snapshots and de-duplicated totals."""
        names = self.accounts.names()
        per = {name: self.providers.snapshot_many(name, ["colab", "google_storage", "antigravity"], force=force)
               for name in names}
        return {"accounts": per, "totals": aggregate_account_totals(per)}

    # ------------------------------------------------------------------ Colab session manager (v2.3)
    def colab_sessions_payload(self, *, force: bool = False) -> dict[str, Any]:
        """Every live Colab VM across all registered accounts (no proxy tokens / URLs)."""
        with self._sessions_lock:
            cached = self._sessions_cache
            if cached and not force and time.time() - cached[0] < 10:
                return cached[1]
            raw = []
            for acct_name, st in colab_meter._iter_states(None):
                try:
                    q = colab_meter._query_account(acct_name, st)
                    raw.append({
                        "account": acct_name,
                        "balance": q.get("balance"),
                        "rate_hourly": q.get("rate_hourly"),
                        "usage_error": q.get("usage_error"),
                        "assignments": [
                            {k: a.get(k) for k in ("name", "endpoint", "hardware", "shape", "variant")}
                            for a in q.get("assignments") or []
                        ],
                    })
                except Exception as exc:
                    raw.append({"account": acct_name, "assignments": [], "error": str(exc)[:300]})
            now = _now_ts()
            live = {a["endpoint"] for r in raw for a in r["assignments"]}
            for ep in live:
                self._session_first_seen.setdefault(ep, now)
            for ep in list(self._session_first_seen):
                if ep not in live:
                    self._session_first_seen.pop(ep, None)
            with self._state_lock:
                hold = dict(self.active_hold) if self.active_hold else None
                job_running = self._job_proc is not None and self._job_proc.poll() is None
            payload = colab_sessions.build_sessions_payload(
                raw, hold=hold, first_seen=dict(self._session_first_seen), now=now)
            payload.update({
                "updated_at_iso": _now_iso(now),
                "job_running": job_running,
                "probe_disabled": self.probe_disabled,
                "hold_gpus": [g["id"] for g in GPU_CATALOG],
            })
            self._sessions_cache = (time.time(), payload)
            return payload

    def _live_session(self, account: Any, name: Any = None, endpoint: Any = None) -> dict[str, Any]:
        """Validate (account, session name or endpoint) against the live listing."""
        acct = colab_sessions.check_account_name(account, self.accounts.names())
        if name is not None:
            name = colab_sessions.check_session_name(name)
        for force in (False, True):
            listing = self.colab_sessions_payload(force=force)
            for a in listing["accounts"]:
                if a["account"] != acct:
                    continue
                for row in a["sessions"]:
                    if (name is not None and row["name"] == name) or (endpoint is not None and row["endpoint"] == endpoint):
                        return {**row, "account": acct}
        raise colab_sessions.SessionError("该会话已不在运行（或不属于这个账号），请刷新列表")

    def _colab_cli(self, account: str, args: list[str], *, stdin: str | None = None,
                   timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._colab_bin(), "-a", account, *args],
            input=stdin, text=True, capture_output=True, timeout=timeout, env=self._colab_env(),
        )

    def _job_on(self, row: dict[str, Any]) -> bool:
        with self._state_lock:
            hold = self.active_hold or {}
        return row["source"] == "hold" and hold.get("state") == "CLAIMED_JOB"

    def session_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """Run one per-session action through the colab CLI. Read-only unless stated."""
        if action == "stop":
            return self.stop_session(body.get("account"), str(body.get("endpoint") or ""))
        row = self._live_session(body.get("account"), body.get("name"))
        if not row["tracked"]:
            raise colab_sessions.SessionError("该会话未被 colab 命令行记录，只能停止")
        acct, name = row["account"], row["name"]
        if action == "url":
            proc = self._colab_cli(acct, ["url", "-s", name], timeout=40)
            if proc.returncode != 0:
                raise colab_sessions.SessionError(colab_sessions.clean_cli_output(proc.stderr or proc.stdout, 400) or "colab url 失败")
            return {"ok": True, "url": colab_sessions.parse_session_url(proc.stdout)}
        if action == "info":
            proc = self._colab_cli(acct, ["exec", "-s", name, "--timeout", "40"],
                                   stdin=colab_sessions.GPU_INFO_SCRIPT, timeout=60)
            return {"ok": proc.returncode == 0, "text": colab_sessions.clean_cli_output(proc.stdout + "\n" + proc.stderr)}
        if action == "ls":
            path = colab_sessions.check_remote_path(body.get("path"))
            proc = self._colab_cli(acct, ["ls", "-s", name, path], timeout=60)
            return {"ok": proc.returncode == 0, "path": path,
                    "text": colab_sessions.clean_cli_output(proc.stdout + "\n" + proc.stderr)}
        if action == "log":
            proc = self._colab_cli(acct, ["log", "-s", name, "-n", "80"], timeout=40)
            return {"ok": proc.returncode == 0, "text": colab_sessions.clean_cli_output(proc.stdout + "\n" + proc.stderr)}
        if action == "download":
            remote = colab_sessions.check_remote_path(body.get("path"), default="")
            target = colab_sessions.download_target(DOWNLOADS_DIR, acct, name, remote)
            target.parent.mkdir(parents=True, exist_ok=True)
            proc = self._colab_cli(acct, ["download", "-s", name, remote, str(target)], timeout=1800)
            if proc.returncode != 0 or not target.is_file():
                raise colab_sessions.SessionError(colab_sessions.clean_cli_output(proc.stderr or proc.stdout, 600) or "下载失败")
            return {"ok": True, "local_path": str(target), "size": target.stat().st_size,
                    "relative": str(target.relative_to(PROJECT_DIR))}
        if action == "upload":
            local = app_paths._wsl_path(str(body.get("local_path") or "")) or Path(str(body.get("local_path") or ""))
            if not local.is_file():
                raise colab_sessions.SessionError("本地文件不存在")
            remote_dir = colab_sessions.check_remote_path(body.get("path"))
            remote = str(PurePosixPath(remote_dir) / local.name)
            proc = self._colab_cli(acct, ["upload", "-s", name, str(local), remote], timeout=1800)
            if proc.returncode != 0:
                raise colab_sessions.SessionError(colab_sessions.clean_cli_output(proc.stderr or proc.stdout, 600) or "上传失败")
            return {"ok": True, "remote_path": remote}
        if action == "restart-kernel":
            if self._job_on(row):
                raise colab_sessions.SessionError("该会话正在运行远端命令，不能重启内核")
            proc = self._colab_cli(acct, ["restart-kernel", "-s", name], timeout=60)
            if proc.returncode != 0:
                raise colab_sessions.SessionError(colab_sessions.clean_cli_output(proc.stderr or proc.stdout, 400) or "重启内核失败")
            return {"ok": True}
        raise colab_sessions.SessionError(f"未知操作：{action}")

    def stop_session(self, account: Any, endpoint: str) -> dict[str, Any]:
        """Stop one VM (billing ends). The held GPU goes through release_hold()."""
        row = self._live_session(account, endpoint=endpoint)
        acct = row["account"]
        if row["source"] == "hold":
            res = self.release_hold(reason="在 Colab 会话管理中手动停止")
            self._sessions_cache = None
            return {**res, "account": acct, "endpoint": endpoint}
        _name, st = self._get_account_state(acct)
        ok, still = colab_meter._terminate_endpoint(acct, st, endpoint)
        self._sessions_cache = None
        self.providers.invalidate(acct)
        label = row["name"] or "未跟踪会话"
        print(f"[{_now_iso()}] [gpu_monitor] 会话管理：停止 {acct}/{label} ({endpoint}) ok={ok} still={still}", flush=True)
        if still:
            with self._state_lock:
                self.critical_alert = f"⚠️ 会话 {label} 停止后仍在运行，请再次停止或使用紧急强杀！"
        try:
            if acct == self._primary_name_safe() or acct == self._hold_account():
                self.refresh_account_snapshot()
        except Exception:
            pass
        self.broadcast_state()
        return {"ok": ok and not still, "account": acct, "endpoint": endpoint, "still_live": still}

    def _hold_account(self) -> str | None:
        with self._state_lock:
            return (self.active_hold or {}).get("account") or None

    def refresh_account_snapshot(self, account: str | None = None) -> dict[str, Any]:
        """Refresh the displayed snapshot: `account`, else the holding account, else the configured one."""
        acct_name, st = self._get_account_state(account or self._hold_account())
        return self._refresh_snapshot_for(acct_name, st)

    def _refresh_snapshot_for(self, acct_name: str, st, *, display: bool = True) -> dict[str, Any]:
        sanitized = colab_provider.fetch_account_snapshot(acct_name, st)
        # Keep the provider cache warm so /api/providers reuses this live data.
        try:
            self.providers.put(colab_provider.snapshot_from_account_data(sanitized))
        except Exception:
            pass
        with self._state_lock:
            if display:
                self.account_snapshot = sanitized
            if isinstance(sanitized.get("balance"), (int, float)):
                self.session_start_ccu.setdefault(acct_name, float(sanitized["balance"]))
        return sanitized

    def _block_account(self, acct_name: str, status: str) -> None:
        secs = ACCOUNT_BLOCK_SECONDS.get(status)
        if secs:
            with self._state_lock:
                self.account_blocks[acct_name] = (_now_ts() + secs, status)

    def _select_probe_account(self, explicit: str | None = None):
        """(name, State, snapshot) for the next probe / grab, applying failover when enabled.

        Returns None when every candidate is temporarily blocked (failover on). Must be called
        with `_probe_lock` held: the chosen (name, State) is used for the whole cycle.
        """
        if explicit:
            name, st = self._get_account_state(self.accounts.require_existing(explicit))
            snap = self._refresh_snapshot_for(name, st)
            with self._state_lock:
                self.probe_account, self.failover_note = name, ""
            return name, st, snap

        primary, primary_st = self._get_account_state()
        states: dict[str, Any] = {primary: primary_st}
        with self._state_lock:
            failover_on = bool(self.config.get("failover_on"))
            backups = list(self.config.get("failover_accounts") or [])
            threshold = float(self.config.get("min_ccu_threshold") or 0.0)
            now = _now_ts()
            blocked = {k: v[0] for k, v in self.account_blocks.items() if v[0] > now}

        def snapshot_of(name: str):
            if name not in states:
                states[name] = self._get_account_state(name)[1]
            return self._refresh_snapshot_for(name, states[name], display=False)

        name, snap, note = choose_probe_account(
            primary, backups, failover_on=failover_on, threshold=threshold,
            blocked_until=blocked, now=now, snapshot_of=snapshot_of,
        )
        with self._state_lock:
            self.failover_note = note
            if name is None:
                # Every candidate is blocked: wait until the first one frees up.
                self.rate_limit_until_ts = max(self.rate_limit_until_ts, min(blocked.values(), default=now + 60.0))
                return None
            self.probe_account = name
            if snap is not None:
                self.account_snapshot = snap
        if note:
            print(f"[{_now_iso()}] [gpu_monitor] 多账号切换：{note}", flush=True)
        if snap is None:
            raise RuntimeError(f"无法读取账号 {name} 的 CCU 额度")
        return name, states[name], snap

    def emergency_stop_all(self, *, all_accounts: bool = False, accounts: list[str] | None = None) -> dict[str, Any]:
        """Kill every VM (incl. orphans) on the target accounts and verify 0 remain.

        Default targets: the account holding a GPU (if any) + the configured account + the account
        the last probe used. `all_accounts=True` covers every registered profile; `accounts`
        names an explicit list (Accounts Hub per-account stop).
        """
        # Stop any running remote command subprocess first so it doesn't race
        self._terminate_job_proc_only()
        with self._probe_lock:
            hold_acct = self._hold_account()
            with self._state_lock:
                legacy_hold = self.active_hold is not None and not hold_acct
            if legacy_hold:  # hold record without an account field: it belongs to the configured account
                hold_acct = self._get_account_state()[0]
            if accounts:
                targets = [(n, self._get_account_state(self.accounts.require_existing(n))[1]) for n in accounts]
            elif all_accounts:
                targets = colab_meter._iter_states(None)
            else:
                targets = []
                for n in (hold_acct, None, self.probe_account):
                    try:
                        name, st = self._get_account_state(n)
                    except Exception:
                        continue
                    if name not in [t[0] for t in targets]:
                        targets.append((name, st))

            per: list[dict[str, Any]] = []
            killed_total = remaining_total = 0
            hold_cleared = False
            for acct_name, st in targets:
                try:
                    killed, remaining = colab_meter._terminate_account_sessions(
                        acct_name=acct_name,
                        st=st,
                        target_session=None,
                        kill_orphans=True,
                    )
                    per.append({"account": acct_name, "killed": killed, "remaining_count": len(remaining)})
                    killed_total += killed
                    remaining_total += len(remaining)
                    if acct_name == hold_acct and not remaining:
                        hold_cleared = True
                except Exception as exc:
                    per.append({"account": acct_name, "error": str(exc)[:300]})
                self.providers.invalidate(acct_name)

            with self._state_lock:
                if hold_cleared:
                    self.active_hold = None
                    self._save_hold_state_locked()
                still_holding = self.active_hold is not None
                failed = [p["account"] for p in per if "error" in p]
                if remaining_total:
                    self.critical_alert = f"⚠️ 强杀后仍有 {remaining_total} 个残留 VM，请再次点击紧急强杀！"
                elif not still_holding and not failed:
                    self.critical_alert = None
            try:
                snap = self.refresh_account_snapshot()
            except Exception as exc:
                snap = {"usage_error": str(exc)}
            self.broadcast_state()
            return {
                "account": ", ".join(p["account"] for p in per),
                "accounts": per,
                "killed": killed_total,
                "remaining_count": remaining_total,
                "failed_accounts": failed,
                "snapshot": snap,
            }

    def _colab_bin(self) -> str:
        c = Path.home() / ".local/bin/colab"
        if c.is_file():
            return str(c)
        return "colab"

    def _colab_env(self) -> dict[str, str]:
        env = dict(os.environ)
        local_bin = str(Path.home() / ".local/bin")
        env["PATH"] = f"{local_bin}:/usr/local/bin:{env.get('PATH', '')}"
        return env

    def _register_locked_session(
        self,
        *,
        acct_name: str,
        st,
        session_name: str,
        assign_resp: Any,
        accel: Accelerator,
        shape: Shape,
        snap: dict[str, Any],
        hold_minutes: int,
        auto_mount: bool,
    ) -> dict[str, Any]:
        """Register an allocated Colab endpoint as a usable `colab` CLI session and start hold countdown."""
        if isinstance(assign_resp, PostAssignmentResponse):
            token = assign_resp.runtime_proxy_info.token
            url = assign_resp.runtime_proxy_info.url
            endpoint = assign_resp.endpoint
            expires_at = assign_resp.runtime_proxy_info.expires_at()
        else:
            rpi = getattr(assign_resp, "runtime_proxy_info", None)
            token = rpi.token if rpi else getattr(assign_resp, "runtime_proxy_token", "")
            url = rpi.url if rpi else ""
            endpoint = assign_resp.endpoint
            expires_at = rpi.expires_at() if rpi else None

        s = SessionState(
            name=session_name,
            token=token,
            url=url,
            endpoint=endpoint,
            token_expires_at=expires_at,
            variant=Variant.GPU.value,
            accelerator=accel.value,
            machine_shape=(
                Shape.HIGH_RAM.name if shape == Shape.HIGH_RAM else Shape.STANDARD.name
            ),
        )
        with colab_meter._activate_ext_context(acct_name):
            try:
                st.store.remove(session_name)
            except Exception:
                pass
            st.store.add(s)
            try:
                st.history.log_event(
                    session_name,
                    "session_created",
                    {
                        "endpoint": endpoint,
                        "variant": Variant.GPU.value,
                        "accelerator": accel.value,
                        "machine_shape": s.machine_shape,
                        "source": "colab_gpu_monitor_hold",
                    },
                )
            except Exception:
                pass

        # Save CCU baseline for colab_meter reconciliation
        try:
            colab_meter.STATE_DIR.mkdir(parents=True, exist_ok=True)
            baseline = {
                "recorded_at_epoch": _now_ts(),
                "recorded_at_iso": _now_iso(),
                "account": acct_name,
                "session": session_name,
                "balance_start": snap.get("balance"),
                "rate_start": snap.get("rate_hourly"),
                "preexisting_endpoints": [],
            }
            colab_meter.BASELINE_FILE.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        except Exception:
            pass

        now = _now_ts()
        exp = now + hold_minutes * 60.0
        hold_obj: dict[str, Any] = {
            "state": "HOLDING",
            "gpu": accel.value,
            "gpu_model": None,
            "endpoint": endpoint,
            "session_name": session_name,
            "account": acct_name,
            "locked_at_ts": now,
            "locked_at_iso": _now_iso(now),
            "expires_at_ts": exp,
            "expires_at_iso": _now_iso(exp),
            "hold_minutes": hold_minutes,
            "drive_status": "MOUNTING" if auto_mount else "SKIPPED",
            "drive_detail": "正在静默挂载 Google Drive (/content/drive)..." if auto_mount else "尚未挂载云盘（可随时点击一键挂载）",
            "ccu_at_lock": snap.get("balance"),
            "job_pid": None,
            "job_log_path": None,
            "job_status": "IDLE",
        }
        with self._state_lock:
            self.active_hold = hold_obj
            self._save_hold_state_locked()

        threading.Thread(
            target=self._post_lock_setup,
            args=(acct_name, session_name, endpoint, auto_mount),
            name="post-lock-setup",
            daemon=True,
        ).start()
        return hold_obj

    def _post_lock_setup(self, acct_name: str, session_name: str, endpoint: str, auto_mount: bool) -> None:
        """Async post-lock tasks: auto-mount Google Drive & query exact GPU model/VRAM."""
        colab_bin = self._colab_bin()
        env = self._colab_env()
        acct_args = ["-a", acct_name] if acct_name else []

        # 1) Detect exact GPU model + VRAM
        try:
            proc = subprocess.run(
                [colab_bin, *acct_args, "exec", "-s", session_name, "--timeout", "35"],
                input="import subprocess; subprocess.run('nvidia-smi --query-gpu=name,memory.total --format=csv,noheader', shell=True)\n",
                text=True,
                capture_output=True,
                timeout=45,
                env=env,
            )
            lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip() and not ln.strip().startswith("[colab]")]
            if lines:
                with self._state_lock:
                    if self.active_hold and self.active_hold.get("endpoint") == endpoint:
                        self.active_hold["gpu_model"] = lines[-1]
                        self._save_hold_state_locked()
                self.broadcast_state()
        except Exception:
            pass

        # 2) Auto-mount Google Drive if enabled
        if auto_mount:
            self._run_drive_mount_for_hold(acct_name, session_name, endpoint)

        try:
            self.refresh_account_snapshot()
        except Exception:
            pass
        self.broadcast_state()

    def _run_drive_mount_for_hold(self, acct_name: str, session_name: str, endpoint: str) -> tuple[bool, str]:
        with self._state_lock:
            if not self.active_hold or self.active_hold.get("endpoint") != endpoint:
                return False, "当前无锁定实例"
            self.active_hold["drive_status"] = "MOUNTING"
            self.active_hold["drive_detail"] = "正在静默挂载 Google Drive -> /content/drive..."
            self._save_hold_state_locked()
        self.broadcast_state()

        colab_bin = self._colab_bin()
        env = self._colab_env()
        acct_args = ["-a", acct_name] if acct_name else []
        try:
            proc = subprocess.run(
                [colab_bin, *acct_args, "dfs", "mount", "-s", session_name],
                text=True,
                capture_output=True,
                timeout=90,
                env=env,
            )
            combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            ok = proc.returncode == 0
            detail = "Google Drive 已挂载至 /content/drive" if ok else f"挂盘失败 (rc={proc.returncode}): {combined[-200:]}"
        except Exception as exc:
            ok = False
            detail = f"挂盘异常: {exc}"

        with self._state_lock:
            if self.active_hold and self.active_hold.get("endpoint") == endpoint:
                self.active_hold["drive_status"] = "MOUNTED" if ok else "FAILED"
                self.active_hold["drive_detail"] = detail
                self._save_hold_state_locked()
        self.broadcast_state()
        print(f"[{_now_iso()}] [gpu_monitor] Drive mount ({session_name}): {detail}", flush=True)
        return ok, detail

    def mount_drive_now(self) -> dict[str, Any]:
        with self._state_lock:
            hold = dict(self.active_hold) if self.active_hold else None
        if not hold:
            raise ValueError("当前没有正在锁定的 GPU 实例")
        threading.Thread(
            target=self._run_drive_mount_for_hold,
            args=(hold["account"], hold["session_name"], hold["endpoint"]),
            daemon=True,
        ).start()
        return {"ok": True, "session_name": hold["session_name"]}

    def extend_hold(self, extra_minutes: int = 10) -> dict[str, Any]:
        extra_minutes = max(1, min(180, int(extra_minutes)))
        with self._state_lock:
            if not self.active_hold:
                raise ValueError("当前没有正在锁定的 GPU 实例")
            if self.active_hold["state"] == "CLAIMED_JOB":
                raise ValueError("远端命令运行中，已由任务看门狗自动托管")
            base = max(_now_ts(), float(self.active_hold["expires_at_ts"]))
            new_exp = base + extra_minutes * 60.0
            self.active_hold["expires_at_ts"] = new_exp
            self.active_hold["expires_at_iso"] = _now_iso(new_exp)
            self.active_hold["hold_minutes"] = int(self.active_hold.get("hold_minutes", 15)) + extra_minutes
            self._save_hold_state_locked()
            hold_copy = dict(self.active_hold)
        print(
            f"[{_now_iso()}] [gpu_monitor] 占卡续期 +{extra_minutes}m -> 新到期时间 {hold_copy['expires_at_iso']}",
            flush=True,
        )
        self.broadcast_state()
        return hold_copy

    def claim_hold_manual(self, max_minutes: int | None = None) -> dict[str, Any]:
        with self._state_lock:
            if not self.active_hold:
                raise ValueError("当前没有正在锁定的 GPU 实例")
            if self.active_hold["state"] == "CLAIMED_JOB":
                raise ValueError("远端命令正在运行中")
            ttl = int(max_minutes or self.config.get("manual_claim_max_minutes", 120))
            ttl = max(15, min(360, ttl))
            new_exp = _now_ts() + ttl * 60.0
            self.active_hold["state"] = "CLAIMED_MANUAL"
            self.active_hold["expires_at_ts"] = new_exp
            self.active_hold["expires_at_iso"] = _now_iso(new_exp)
            self.active_hold["hold_minutes"] = ttl
            self._save_hold_state_locked()
            hold_copy = dict(self.active_hold)
        print(
            f"[{_now_iso()}] [gpu_monitor] 用户已认领手动使用 ({hold_copy['session_name']})，最大兜底保护 {ttl} 分钟",
            flush=True,
        )
        self.broadcast_state()
        return hold_copy

    def release_hold(self, *, reason: str = "用户手动释放锁定实例") -> dict[str, Any]:
        with self._hold_lock:
            with self._state_lock:
                hold = dict(self.active_hold) if self.active_hold else None
            if not hold:
                return {"ok": True, "released": False, "detail": "当前无锁定实例"}

            self._terminate_job_proc_only()
            acct_name, st = self._get_account_state(hold.get("account"))
            with self._probe_lock:
                killed, remaining = colab_meter._terminate_account_sessions(
                    acct_name=acct_name,
                    st=st,
                    target_session=None,
                    kill_orphans=True,
                )
                snap = self.refresh_account_snapshot()

            ccu_spent = None
            start_bal = hold.get("ccu_at_lock")
            end_bal = snap.get("balance")
            if isinstance(start_bal, (int, float)) and isinstance(end_bal, (int, float)):
                ccu_spent = round(max(0.0, float(start_bal) - float(end_bal)), 4)

            with self._state_lock:
                if not remaining:
                    self.active_hold = None
                    self._save_hold_state_locked()
                    self.critical_alert = None
                else:
                    self.critical_alert = f"⚠️ 释放锁定实例后仍有 {len(remaining)} 个残留 VM，请点击紧急强杀！"
                toast_on = bool(self.config.get("windows_toast", True))

            elapsed_min = round(max(0.0, (_now_ts() - float(hold["locked_at_ts"])) / 60.0), 1)
            ccu_txt = f"，消耗约 {ccu_spent:.2f} CCU" if ccu_spent is not None else ""
            summary = f"{hold['gpu']} ({hold['session_name']}) 已关闭：{reason}（占用 {elapsed_min} 分钟{ccu_txt}，残留 VM={len(remaining)}）"
            print(f"[{_now_iso()}] [gpu_monitor] {summary}", flush=True)

            self.broker.publish(
                "alert",
                {
                    "kind": "released",
                    "gpu": hold["gpu"],
                    "ts_iso": _now_iso(),
                    "detail": summary,
                    "ccu_spent": ccu_spent,
                    "remaining_count": len(remaining),
                },
            )
            if toast_on:
                _send_windows_toast(f"Colab {hold['gpu']} 已自动关机释放", summary)

            self.broadcast_state()
            return {
                "ok": len(remaining) == 0,
                "released": True,
                "killed": killed,
                "remaining_count": len(remaining),
                "ccu_spent": ccu_spent,
                "detail": summary,
            }

    def grab_gpu_now(self, gpu_id: str, hold_minutes: int | None = None, account: str | None = None) -> dict[str, Any]:
        """Immediately attempt a single-shot allocation of `gpu_id` and lock + auto-mount if available."""
        gpu_id = str(gpu_id or "").strip().upper()
        if gpu_id not in GPU_META_BY_ID:
            raise ValueError(f"Unsupported GPU: {gpu_id}")
        self._ensure_probe_allowed()
        with self._state_lock:
            if self.active_hold is not None:
                raise ValueError(f"当前已锁定 {self.active_hold['gpu']} ({self.active_hold['session_name']})，请先释放或使用当前实例")

        with self._probe_lock:
            with self._state_lock:
                self.probing = True
            self.broadcast_state()
            try:
                picked = self._select_probe_account(account)
                if picked is None:
                    raise ValueError("所有可用账号都在冷却中（429 / 实例上限 / 授权失效），请稍后再试")
                acct_name, st, snap = picked
                rec = self.probe_single_gpu(
                    gpu_id,
                    snap,
                    acct_name=acct_name,
                    st=st,
                    manual=True,
                    force_hold=True,
                    hold_minutes_override=hold_minutes,
                )
                if rec.get("status") in ACCOUNT_BLOCK_SECONDS:
                    self._block_account(acct_name, rec["status"])
                return rec
            finally:
                with self._state_lock:
                    self.probing = False
                    self.current_probe_gpu = None
                self.broadcast_state()

    def _terminate_job_proc_only(self) -> None:
        proc = self._job_proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._job_proc = None

    def run_command(self, command: str, *, max_minutes: int = 180, mount_drive: bool = False) -> dict[str, Any]:
        """Claim the held GPU and run one shell command on it via `wsl_run_remote.sh --reuse-session`.

        The runner sets session / GPU / account / `--fallback-gpu none` / `--max-minutes` itself
        (see providers/remote_runner.py), so the command can never disable the auto-shutdown
        watchdog. When the command exits the GPU is released and reconciled.
        """
        with self._state_lock:
            hold = dict(self.active_hold) if self.active_hold else None
            if self._job_proc is not None and self._job_proc.poll() is None:
                raise ValueError("已有远端命令正在运行中")
            if not hold:
                raise ValueError("当前没有已锁定的 GPU 实例，请先抢到 / 锁定 GPU 后再运行命令")
        argv = remote_runner.build_runner_argv(
            session=hold["session_name"],
            gpu=hold["gpu"],
            account=hold.get("account"),
            max_minutes=max_minutes,
            command=command,
            mount_drive=bool(mount_drive),
        )
        if not remote_runner.RUN_SCRIPT.is_file():
            raise ValueError(f"未找到运行脚本: {remote_runner.RUN_SCRIPT}")
        minutes = remote_runner.clamp_minutes(max_minutes)

        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = LOGS_DIR / f"remote_command_{ts}.log"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(job_log, "w", encoding="utf-8", buffering=1) as log_fh:  # the child keeps its own copy
            proc = subprocess.Popen(
                argv,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=self._colab_env(),
            )
        self._job_proc = proc
        preview = argv[-1].splitlines()[0][:80]

        with self._state_lock:
            self.last_job_log_path = str(job_log)
            self.last_job_status = "RUNNING"
            if self.active_hold:
                self.active_hold["state"] = "CLAIMED_JOB"
                self.active_hold["job_pid"] = proc.pid
                self.active_hold["job_log_path"] = str(job_log)
                self.active_hold["job_status"] = "RUNNING"
                self.active_hold["task_name"] = "远端命令"
                self.active_hold["command_preview"] = preview
                self.active_hold["expires_at_ts"] = _now_ts() + minutes * 60.0
                self.active_hold["expires_at_iso"] = _now_iso(self.active_hold["expires_at_ts"])
                self._save_hold_state_locked()
            hold_copy = dict(self.active_hold) if self.active_hold else None

        print(f"[{_now_iso()}] [gpu_monitor] 已接管 {hold['gpu']} 运行远端命令 (PID={proc.pid}, log={job_log})", flush=True)
        self.broadcast_state()
        return {"ok": True, "pid": proc.pid, "log_path": str(job_log), "hold": hold_copy}

    def stop_job(self) -> dict[str, Any]:
        """Stop the running remote command and force-release the held GPU."""
        return self.release_hold(reason="用户手动中止远端命令并回收 GPU")

    def get_job_log(self, tail_bytes: int = 32768) -> dict[str, Any]:
        with self._state_lock:
            path_str = (self.active_hold or {}).get("job_log_path") or self.last_job_log_path
            status = (self.active_hold or {}).get("job_status") or self.last_job_status or "IDLE"
        if not path_str:
            # Fallback to the newest runner log (run_<session>_*.log) if present
            candidates = sorted(LOGS_DIR.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                path_str = str(candidates[0])
        if not path_str or not Path(path_str).is_file():
            return {"status": status, "path": path_str, "content": ""}
        p = Path(path_str)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read()
        return {
            "status": status,
            "path": str(p),
            "size": size,
            "content": data.decode("utf-8", errors="replace"),
        }

    def _hold_watchdog_loop(self) -> None:
        """1s background watchdog: enforces hold countdown auto-shutdown & monitors claimed remote commands."""
        last_sync_ts = 0.0
        while not self._stop_event.is_set():
            try:
                now = _now_ts()
                with self._state_lock:
                    hold = dict(self.active_hold) if self.active_hold else None

                if hold:
                    state = hold.get("state")
                    # 1) Check claimed remote command completion
                    if state == "CLAIMED_JOB":
                        proc = self._job_proc
                        job_done = False
                        rc = None
                        if proc is not None:
                            rc = proc.poll()
                            if rc is not None:
                                job_done = True
                                self._job_proc = None
                        elif hold.get("job_pid"):
                            try:
                                os.kill(int(hold["job_pid"]), 0)
                            except OSError:
                                job_done = True

                        if job_done:
                            ok_job = rc == 0
                            with self._state_lock:
                                self.last_job_status = "DONE" if ok_job else f"FAILED (rc={rc})"
                            # Verify server has 0 lingering VMs
                            self.release_hold(
                                reason="远端命令已完成并自动回收 GPU" if ok_job else f"远端命令异常结束 (rc={rc})，已安全回收 GPU"
                            )
                            continue

                    # 2) Enforce hold / manual-claim expiration timer!
                    if state in ("HOLDING", "CLAIMED_MANUAL"):
                        exp_ts = float(hold.get("expires_at_ts") or 0.0)
                        if now >= exp_ts:
                            reason = (
                                f"占卡 {hold.get('hold_minutes', 15)} 分钟到期未使用，自动关机防扣费"
                                if state == "HOLDING"
                                else "手动使用达到最大保护时长，自动关机防扣费"
                            )
                            self.release_hold(reason=reason)
                            continue

                    # 3) Every 25s, sync with Colab backend to update live CCU & detect external stops
                    if now - last_sync_ts >= 25.0:
                        last_sync_ts = now
                        try:
                            snap = self.refresh_account_snapshot()
                            live_eps = {a.get("endpoint") for a in (snap.get("assignments") or [])}
                            # Grace period 20s after lock before assuming externally killed
                            if (now - float(hold.get("locked_at_ts") or 0.0)) > 20.0 and hold.get("endpoint") not in live_eps:
                                print(
                                    f"[{_now_iso()}] [gpu_monitor] 检测到锁定实例 {hold.get('endpoint')} 已在外部关闭，自动清理占卡状态。",
                                    flush=True,
                                )
                                with self._state_lock:
                                    if self.active_hold and self.active_hold.get("endpoint") == hold.get("endpoint"):
                                        self.active_hold = None
                                        self._save_hold_state_locked()
                            self.broadcast_state()
                        except Exception:
                            pass
            except Exception as exc:
                print(f"[{_now_iso()}] [gpu_monitor] Watchdog error: {exc}", file=sys.stderr, flush=True)

            self._stop_event.wait(timeout=1.0)

    def _force_release_probe_endpoint(self, st, endpoint: str) -> tuple[bool, int, str]:
        """Immediately unassign a single probe-created endpoint and verify removal."""
        t0 = time.monotonic()
        last_err = ""
        for attempt in range(1, 6):
            try:
                st.client.unassign(endpoint)
                last_err = ""
                break
            except Exception as exc:
                last_err = str(exc)
                time.sleep(0.4 * attempt)

        # Verify endpoint no longer appears in server-side assignments
        verified = False
        for _ in range(4):
            try:
                live = list(st.client.list_assignments() or [])
                if all(a.endpoint != endpoint for a in live):
                    verified = True
                    break
            except Exception as exc:
                last_err = str(exc)
            time.sleep(0.8)
            try:
                st.client.unassign(endpoint)
            except Exception:
                pass

        release_ms = int((time.monotonic() - t0) * 1000)
        return verified, release_ms, last_err

    def probe_single_gpu(
        self,
        gpu_id: str,
        snap: dict[str, Any],
        *,
        acct_name: str,
        st,
        manual: bool = False,
        force_hold: bool = False,
        hold_minutes_override: int | None = None,
    ) -> dict[str, Any]:
        """Execute one zero-retry Colab GPU capacity probe; either hold+mount or auto-release <1s."""
        with self._state_lock:
            self.current_probe_gpu = gpu_id
        self.broadcast_state()

        # Short-circuit if Colab ccu-info explicitly lists this GPU in ineligibleGpus
        ineligible = set(snap.get("ineligible_gpus") or [])
        if gpu_id in ineligible:
            now = _now_ts()
            rec = {
                "ts": now,
                "ts_iso": _now_iso(now),
                "hour_local": _local_hour(now),
                "account": acct_name,
                "gpu": gpu_id,
                "status": "INELIGIBLE",
                "http_code": 400,
                "latency_ms": 0,
                "released": 0,
                "release_ms": None,
                "ccu_balance": snap.get("balance"),
                "burn_rate": snap.get("rate_hourly"),
                "active_vms": len(snap.get("assignments") or []),
                "detail": "当前账号无该卡权限 (Colab ccu-info: ineligibleGpus，需更高订阅等级/资格)",
            }
            rec["id"] = self.store.insert(rec)
            with self._state_lock:
                self.current_probe_gpu = None
            self.broker.publish("probe", rec)
            return rec

        t0 = time.monotonic()
        nbh = uuid.uuid4()
        accel = Accelerator(gpu_id)
        shape = resolve_assign_shape(accel, high_mem=False)

        status = "NETWORK_ERROR"
        http_code: int | None = None
        detail = ""
        released = 0
        release_ms: int | None = None
        allocated_endpoint: str | None = None
        allocated_resp: Any = None
        locked_hold: dict[str, Any] | None = None

        with colab_meter._activate_ext_context(acct_name):
            try:
                # Step 1: GET /tun/m/assign -> XSRF token
                get_resp = st.client._get_assignment(nbh, Variant.GPU, accel, shape)
                if isinstance(get_resp, Assignment):
                    allocated_resp = get_resp
                    allocated_endpoint = get_resp.endpoint
                    status = "AVAILABLE"
                    http_code = 200
                    detail = f"有卡可抢！分配成功 ({allocated_endpoint})"
                else:
                    # Step 2: Single-shot POST /tun/m/assign (NO retries!)
                    post_resp = st.client._post_assignment(
                        nbh, get_resp.token, Variant.GPU, accel, shape
                    )
                    if isinstance(post_resp, PostAssignmentResponse):
                        allocated_resp = post_resp
                        allocated_endpoint = post_resp.endpoint
                        status = "AVAILABLE"
                        http_code = 200
                        detail = f"有卡可抢！分配成功 ({allocated_endpoint})"
                    else:
                        status = "AVAILABLE"
                        http_code = 200
                        detail = "有卡可抢！"
            except ColabRequestError as exc:
                http_code = get_status_code(exc)
                reason = getattr(getattr(exc, "response", None), "reason", "") or str(exc)
                body_snip = (exc.response_body or "").strip().replace("\n", " ")[:180]
                if http_code == 503:
                    status = "SOLD_OUT"
                    detail = "503 Service Unavailable (满载缺货 · 0 扣费)"
                elif http_code in (502, 504):
                    status = "BUSY"
                    detail = f"{http_code} {reason} (后端繁忙 · 0 扣费)"
                elif http_code == 429:
                    status = "RATE_LIMITED"
                    detail = "429 Too Many Requests (触发限流，已自动拉长冷却)"
                    with self._state_lock:
                        # With failover on, only this account cools down; the next cycle can use a backup.
                        if not self.config.get("failover_on"):
                            self.rate_limit_until_ts = _now_ts() + 180.0
                elif http_code == 412:
                    status = "LIMIT_REACHED"
                    detail = "412 实例数量已达账号上限"
                elif http_code == 400:
                    status = "INELIGIBLE"
                    detail = "400 Bad Request (当前账号无该卡权限或参数不受该显卡支持)"
                elif http_code in (401, 403):
                    status = "AUTH_ERROR"
                    detail = f"{http_code} 授权失效或无权访问，请重跑 1_首次授权_Auth.bat"
                else:
                    status = "NETWORK_ERROR"
                    detail = f"HTTP {http_code or '?'} {reason} {body_snip}".strip()
            except requests.exceptions.Timeout:
                status = "NETWORK_ERROR"
                detail = "请求超时（代理或网络波动）"
            except Exception as exc:
                status = "NETWORK_ERROR"
                detail = f"{type(exc).__name__}: {str(exc)[:180]}"
            finally:
                # Decide whether to LOCK (hold + auto-mount + countdown) or auto-release <1s!
                if allocated_endpoint:
                    with self._state_lock:
                        cfg_hold_on_hit = bool(self.config.get("hold_on_hit", True))
                        cfg_hold_gpus = set(self.config.get("hold_gpus") or ["G4", "A100"])
                        cfg_hold_mins = int(hold_minutes_override or self.config.get("hold_minutes", 15))
                        cfg_auto_mount = bool(self.config.get("auto_mount_drive", False))
                        cfg_session = str(self.config.get("hold_session_name") or "gpuwork")
                        already_holding = self.active_hold is not None

                    should_hold = (
                        allocated_resp is not None
                        and not already_holding
                        and (force_hold or (cfg_hold_on_hit and gpu_id in cfg_hold_gpus))
                    )
                    if should_hold:
                        try:
                            locked_hold = self._register_locked_session(
                                acct_name=acct_name,
                                st=st,
                                session_name=cfg_session,
                                assign_resp=allocated_resp,
                                accel=accel,
                                shape=shape,
                                snap=snap,
                                hold_minutes=max(5, min(180, cfg_hold_mins)),
                                auto_mount=cfg_auto_mount,
                            )
                            released = 2  # 2 = HELD / LOCKED intentionally
                            mount_note = "，正在自动挂载 Google Drive" if cfg_auto_mount else ""
                            detail = (
                                f"已自动抢占并锁定 {gpu_id} ({cfg_session} / {allocated_endpoint})！"
                                f"倒计时 {cfg_hold_mins} 分钟未使用将自动关机{mount_note}"
                            )
                        except Exception as reg_exc:
                            # If registration failed for any reason, fail-safe release immediately!
                            verified, rel_ms, rel_err = self._force_release_probe_endpoint(st, allocated_endpoint)
                            release_ms = rel_ms
                            released = 1 if verified else -1
                            detail = f"占卡注册异常 ({reg_exc})，已安全秒释 ({rel_ms}ms): {rel_err or 'OK'}"
                    else:
                        verified, rel_ms, rel_err = self._force_release_probe_endpoint(st, allocated_endpoint)
                        release_ms = rel_ms
                        if verified:
                            released = 1
                            detail = f"有卡！已于 {rel_ms}ms 内自动秒释 ({allocated_endpoint})，防幽灵计费已校验"
                        else:
                            released = -1
                            detail = (
                                f"⚠️ 有卡但自动释放未确认 ({allocated_endpoint}): {rel_err}！"
                                "请立即点击「一键强杀残留 VM」"
                            )
                            with self._state_lock:
                                self.critical_alert = detail

        latency_ms = int((time.monotonic() - t0) * 1000)
        now = _now_ts()
        rec = {
            "ts": now,
            "ts_iso": _now_iso(now),
            "hour_local": _local_hour(now),
            "account": acct_name,
            "gpu": gpu_id,
            "status": status,
            "http_code": http_code,
            "latency_ms": latency_ms,
            "released": released,
            "release_ms": release_ms,
            "ccu_balance": snap.get("balance"),
            "burn_rate": snap.get("rate_hourly"),
            "active_vms": len(snap.get("assignments") or []) + (1 if locked_hold else 0),
            "detail": detail,
        }
        rec["id"] = self.store.insert(rec)

        if status == "AVAILABLE":
            with self._state_lock:
                cd = int(self.config.get("hit_cooldown_seconds", 600))
                if cd > 0 and not locked_hold:
                    self.cooldown_until[gpu_id] = now + cd
                if self.config.get("pause_gpu_on_hit") and not locked_hold and gpu_id in self.config["enabled_gpus"]:
                    self.config["enabled_gpus"] = [g for g in self.config["enabled_gpus"] if g != gpu_id]
                    self._save_config()
                toast_on = bool(self.config.get("windows_toast", True))
            meta = GPU_META_BY_ID.get(gpu_id, {})
            if locked_hold:
                alert_payload = {
                    "kind": "locked",
                    "gpu": gpu_id,
                    "label": meta.get("label", gpu_id),
                    "vram": meta.get("vram", ""),
                    "ts_iso": rec["ts_iso"],
                    "detail": detail,
                    "released": 2,
                    "hold": locked_hold,
                }
                self.broker.publish("alert", alert_payload)
                if toast_on:
                    _send_windows_toast(
                        f"🔒 已抢到并锁定 Colab {gpu_id} ({meta.get('vram', '')})！",
                        f"已为您占住 {locked_hold['hold_minutes']} 分钟并正在挂载 Google Drive，超时未使用将自动关机！",
                    )
            else:
                alert_payload = {
                    "kind": "hit",
                    "gpu": gpu_id,
                    "label": meta.get("label", gpu_id),
                    "vram": meta.get("vram", ""),
                    "ts_iso": rec["ts_iso"],
                    "detail": detail,
                    "released": released,
                    "release_ms": release_ms,
                }
                self.broker.publish("alert", alert_payload)
                if toast_on:
                    _send_windows_toast(
                        f"Colab {gpu_id} ({meta.get('vram', '')}) 有卡可抢！",
                        f"刚刚探测到 {gpu_id} 有余量（已秒释防扣费），请在面板点击「直接抢占」！",
                    )

        with self._state_lock:
            self.current_probe_gpu = None
        self.broker.publish("probe", rec)
        return rec

    def run_probe_cycle(self, target_gpus: list[str] | None = None, *, manual: bool = False) -> list[dict[str, Any]]:
        """Run one full probe cycle across target GPUs (thread-safe, serialized)."""
        if self.probe_disabled:
            # Read-only mode: only refresh the account snapshot, never call /tun/m/assign.
            try:
                self.refresh_account_snapshot()
            except Exception as exc:
                print(f"[{_now_iso()}] [gpu_monitor] Snapshot refresh error: {exc}", file=sys.stderr, flush=True)
            self.broadcast_state()
            return []
        with self._probe_lock:
            with self._state_lock:
                self.probing = True
            self.broadcast_state()

            results: list[dict[str, Any]] = []
            try:
                picked = self._select_probe_account()
                if picked is None:
                    print(f"[{_now_iso()}] [gpu_monitor] {self.failover_note}", flush=True)
                    return results
                acct_name, st, snap = picked
                active_vms = snap.get("assignments") or []

                with self._state_lock:
                    skip_busy = bool(self.config.get("skip_when_active_vm", True))
                    gpus = list(target_gpus) if target_gpus is not None else list(self.config.get("enabled_gpus", []))
                    holding_now = self.active_hold is not None

                if (holding_now or (skip_busy and active_vms)) and not manual:
                    # Do not disturb active/locked Colab VM
                    print(
                        f"[{_now_iso()}] [gpu_monitor] 检测到 {'锁定中的实例' if holding_now else f'{len(active_vms)} 个活跃 VM'}，已自动跳过本轮探测以防冲突。",
                        flush=True,
                    )
                    return results

                any_hit = False
                now = _now_ts()
                for idx, gpu_id in enumerate(gpus):
                    if self._stop_event.is_set():
                        break
                    with self._state_lock:
                        if self.active_hold is not None:
                            break
                    if not manual:
                        with self._state_lock:
                            cd_until = self.cooldown_until.get(gpu_id, 0.0)
                        if cd_until > now:
                            continue
                    if idx > 0:
                        time.sleep(1.2)
                    rec = self.probe_single_gpu(gpu_id, snap, acct_name=acct_name, st=st, manual=manual)
                    results.append(rec)
                    icon = "🔒 已锁卡!" if rec.get("released") == 2 else ("✅ 有卡!" if rec["status"] == "AVAILABLE" else ("❌ 503" if rec["status"] == "SOLD_OUT" else f"⚠️ {rec['status']}"))
                    print(
                        f"[{rec['ts_iso']}] [{gpu_id:4s}] {icon} | {rec['latency_ms']}ms | {rec['detail']}",
                        flush=True,
                    )
                    if rec["status"] == "AVAILABLE":
                        any_hit = True
                    if rec.get("released") == 2:
                        # Locked a GPU! Stop probing remaining GPUs immediately.
                        break
                    if rec["status"] in ACCOUNT_BLOCK_SECONDS:
                        self._block_account(acct_name, rec["status"])
                        break

                if any_hit:
                    # Post-hit refresh (holding account if we just locked, else the probed one)
                    if self._hold_account():
                        self.refresh_account_snapshot()
                    else:
                        self._refresh_snapshot_for(acct_name, st)
            except Exception as exc:
                print(f"[{_now_iso()}] [gpu_monitor] Probe cycle error: {exc}", file=sys.stderr, flush=True)
            finally:
                with self._state_lock:
                    self.probing = False
                    self.current_probe_gpu = None
                self.broadcast_state()

            return results

    def _loop(self) -> None:
        # Initial quick probe right after startup
        self.next_probe_ts = _now_ts() + 1.0
        while not self._stop_event.is_set():
            # Process any manual probes immediately
            if self._manual_queue:
                manual_gpus = self._manual_queue.popleft()
                self.run_probe_cycle(target_gpus=manual_gpus, manual=True)
                continue

            now = _now_ts()
            with self._state_lock:
                paused = self.paused
                due = now >= self.next_probe_ts and now >= self.rate_limit_until_ts
                interval = int(self.config.get("interval_seconds", 60))

            if not paused and due:
                self.run_probe_cycle(manual=False)
                with self._state_lock:
                    interval = int(self.config.get("interval_seconds", 60))
                    base_next = _now_ts() + interval
                    self.next_probe_ts = max(base_next, self.rate_limit_until_ts)
                self.broadcast_state()

            self._wake_event.wait(timeout=0.5)
            self._wake_event.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="gpu-monitor-loop", daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._hold_watchdog_loop, name="hold-watchdog", daemon=True)
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=3.0)

    def build_state_payload(self) -> dict[str, Any]:
        now = _now_ts()
        since_24h = now - 86400.0
        summaries = self.store.get_gpu_summaries(since_24h)
        with self._state_lock:
            cfg = dict(self.config)
            snap = dict(self.account_snapshot)
            paused = self.paused
            probing = self.probing
            cur_gpu = self.current_probe_gpu
            next_ts = self.next_probe_ts
            rl_until = self.rate_limit_until_ts
            cooldowns = dict(self.cooldown_until)
            start_ccu = self.session_start_ccu.get(str(snap.get("account") or ""))
            started_ts = self.started_ts
            probe_account = self.probe_account
            failover_note = self.failover_note
            blocks = {
                k: {"remaining_s": int(v[0] - now), "reason": v[1]}
                for k, v in self.account_blocks.items()
                if v[0] > now
            }
            crit = self.critical_alert
            hold_copy = dict(self.active_hold) if self.active_hold else None
            last_job_status = self.last_job_status
            last_job_log = self.last_job_log_path

        if hold_copy:
            rem_s = max(0, int(float(hold_copy.get("expires_at_ts") or 0.0) - now))
            elapsed_s = max(0, int(now - float(hold_copy.get("locked_at_ts") or now)))
            hold_copy["remaining_s"] = rem_s
            hold_copy["elapsed_s"] = elapsed_s

        cur_bal = snap.get("balance")
        ccu_delta = None
        if isinstance(start_ccu, (int, float)) and isinstance(cur_bal, (int, float)):
            ccu_delta = round(max(0.0, start_ccu - cur_bal), 4)

        eligible_set = set(snap.get("eligible_gpus") or [])
        ineligible_set = set(snap.get("ineligible_gpus") or [])
        hold_gpu_set = set(cfg.get("hold_gpus") or [])
        gpu_cards = []
        for meta in GPU_CATALOG:
            gid = meta["id"]
            s = summaries.get(gid, {})
            cd_rem = max(0, int(cooldowns.get(gid, 0.0) - now))
            eligible_flag: bool | None = None
            if gid in ineligible_set:
                eligible_flag = False
            elif gid in eligible_set:
                eligible_flag = True
            gpu_cards.append(
                {
                    **meta,
                    "eligible": eligible_flag,
                    "enabled": gid in cfg.get("enabled_gpus", []),
                    "hold_enabled": gid in hold_gpu_set,
                    "probing_now": probing and cur_gpu == gid,
                    "cooldown_remaining_s": cd_rem,
                    "latest": s.get("latest"),
                    "last_available_ts": s.get("last_available_ts"),
                    "last_available_iso": s.get("last_available_iso"),
                    "window_total": s.get("window_total", 0),
                    "window_hits": s.get("window_hits", 0),
                    "window_sold_out": s.get("window_sold_out", 0),
                    "avg_latency_ms": s.get("avg_latency_ms"),
                    "consecutive_misses": s.get("consecutive_misses", 0),
                    "recent_ticks": s.get("recent_ticks", []),
                }
            )

        return {
            "server_time_ts": now,
            "server_time_iso": _now_iso(now),
            "started_ts": started_ts,
            "started_iso": _now_iso(started_ts),
            "version": APP_VERSION,
            "probe_disabled": self.probe_disabled,
            "paused": paused,
            "probing": probing,
            "current_probe_gpu": cur_gpu,
            "next_probe_ts": next_ts,
            "next_probe_in_s": max(0, int(next_ts - now)) if not paused else None,
            "rate_limit_remaining_s": max(0, int(rl_until - now)),
            "critical_alert": crit,
            "account": snap,
            "primary_account": self._primary_name_safe(),
            "probe_account": probe_account,
            "failover_note": failover_note,
            "account_blocks": blocks,
            "active_hold": hold_copy,
            "last_job_status": last_job_status,
            "last_job_log_path": last_job_log,
            "session_start_ccu": start_ccu,
            "session_ccu_delta": ccu_delta,
            "config": cfg,
            "gpus": gpu_cards,
        }

    def broadcast_state(self) -> None:
        try:
            self.broker.publish("state", self.build_state_payload())
        except Exception:
            pass


def make_handler(engine: MonitorEngine):
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress noisy per-request access logs in terminal
            return

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 65536))
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                if not UI_HTML_PATH.is_file():
                    self._send_json({"error": f"UI file missing: {UI_HTML_PATH}"}, status=500)
                    return
                body = UI_HTML_PATH.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "version": APP_VERSION,
                        "probe_disabled": engine.probe_disabled,
                        "server_time_iso": _now_iso(),
                        "has_active_hold": engine.active_hold is not None,
                    }
                )
                return

            if path == "/api/state":
                self._send_json(engine.build_state_payload())
                return

            if path == "/api/providers":
                account = (qs.get("account") or [None])[0]
                force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                try:
                    self._send_json(engine.providers_payload(account, force=force))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/settings":
                self._send_json({"proxy": netcfg.public_view(), "version": APP_VERSION})
                return

            if path == "/api/accounts":
                try:
                    self._send_json(engine.accounts_payload())
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path in ("/api/benefits", "/api/selfcheck"):
                force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                try:
                    if path == "/api/benefits":
                        self._send_json(engine.benefits_payload((qs.get("account") or [None])[0], force=force))
                    else:
                        self._send_json(engine.self_check(force=force))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/accounts/overview":
                force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                try:
                    self._send_json(engine.accounts_overview(force=force))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/colab/sessions":
                force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                try:
                    self._send_json(engine.colab_sessions_payload(force=force))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/job/log":
                tail = int((qs.get("tail") or ["32768"])[0])
                self._send_json(engine.get_job_log(tail_bytes=max(2048, min(262144, tail))))
                return

            if path == "/api/history":
                limit = int((qs.get("limit") or ["120"])[0])
                gpu = (qs.get("gpu") or [None])[0]
                status = (qs.get("status") or [None])[0]
                self._send_json({"items": engine.store.get_history(limit=limit, gpu=gpu, status=status)})
                return

            if path == "/api/analytics":
                hours = int((qs.get("hours") or ["24"])[0])
                self._send_json(engine.store.get_analytics(hours=hours))
                return

            if path == "/api/events":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                sub_q = engine.broker.subscribe()
                try:
                    # Push immediate initial snapshot
                    init_msg = f"event: state\ndata: {json.dumps(engine.build_state_payload(), ensure_ascii=False)}\n\n"
                    self.wfile.write(init_msg.encode("utf-8"))
                    self.wfile.flush()

                    while not engine._stop_event.is_set():
                        try:
                            msg = sub_q.get(timeout=15.0)
                            self.wfile.write(msg.encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    engine.broker.unsubscribe(sub_q)
                return

            self._send_json({"error": "Not found"}, status=404)

        def _cross_origin(self) -> bool:
            """Browsers attach Origin to cross-site POSTs; only accept our own page (or none)."""
            origin = self.headers.get("Origin")
            if not origin:  # desktop shell / CLI clients (urllib) send no Origin
                return False
            host = (self.headers.get("Host") or "").strip().lower()
            netloc = urllib.parse.urlparse(origin).netloc.lower()
            return not host or netloc != host

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if self._cross_origin():
                # The body is left unread, so the keep-alive connection must not be reused.
                self.close_connection = True
                self._send_json({"error": "cross-origin request refused"}, status=403)
                return
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json({"error": f"Invalid JSON: {exc}"}, status=400)
                return

            if path.startswith("/api/accounts/"):
                action = path[len("/api/accounts/"):]
                hub = engine.accounts
                name = str(body.get("name") or "").strip()
                try:
                    if action == "add-start":
                        res = hub.start_add(name, body.get("email"))
                    elif action == "login-start":
                        res = hub.start_login(name)
                    elif action == "login-finish":
                        res = hub.finish_login(name, str(body.get("code") or ""))
                        engine.providers.invalidate(name)
                    elif action == "drive-start":
                        res = hub.start_drive(name)
                    elif action == "drive-finish":
                        res = hub.finish_drive(name, str(body.get("code") or ""))
                        engine.providers.invalidate(name)
                    elif action == "use":
                        res = engine.use_account(body.get("name") or None)
                    elif action == "remove":
                        res = engine.remove_account(
                            name,
                            confirm=str(body.get("confirm") or ""),
                            allow_unverified=bool(body.get("allow_unverified")),
                        )
                    elif action == "meta":
                        res = {"ok": True, "meta": hub.set_meta(name, body.get("meta") or {})}
                    else:
                        self._send_json({"error": "Not found"}, status=404)
                        return
                    self._send_json(res)
                except AccountsError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path.startswith("/api/colab/session/"):
                action = path[len("/api/colab/session/"):]
                try:
                    self._send_json(engine.session_action(action, body))
                except (colab_sessions.SessionError, AccountsError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except subprocess.TimeoutExpired:
                    self._send_json({"error": "colab 命令超时"}, status=504)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/probe-now":
                gpu = body.get("gpu")
                try:
                    engine.trigger_manual_probe(gpu=gpu)
                    self._send_json({"ok": True, "gpu": gpu or "ENABLED"})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return

            if path == "/api/grab-now":
                gpu = body.get("gpu")
                hold_mins = body.get("hold_minutes")
                try:
                    rec = engine.grab_gpu_now(gpu, hold_minutes=hold_mins, account=body.get("account") or None)
                    self._send_json({"ok": True, "record": rec, "locked": rec.get("released") == 2})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/hold/extend":
                mins = int(body.get("minutes", 10))
                try:
                    hold = engine.extend_hold(extra_minutes=mins)
                    self._send_json({"ok": True, "hold": hold})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return

            if path == "/api/hold/claim":
                max_mins = body.get("max_minutes")
                try:
                    hold = engine.claim_hold_manual(max_minutes=max_mins)
                    self._send_json({"ok": True, "hold": hold})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return

            if path == "/api/hold/mount-drive":
                try:
                    res = engine.mount_drive_now()
                    self._send_json(res)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return

            if path == "/api/hold/release":
                try:
                    res = engine.release_hold(reason=str(body.get("reason") or "用户手动释放锁定实例"))
                    self._send_json(res)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/hold/run-command":
                try:
                    res = engine.run_command(
                        str(body.get("command") or ""),
                        max_minutes=body.get("max_minutes", 180),
                        mount_drive=bool(body.get("mount_drive")),
                    )
                    self._send_json(res)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/job/stop":
                try:
                    res = engine.stop_job()
                    self._send_json(res)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/shutdown":
                release_first = bool(body.get("release_hold", False))
                if release_first and engine.active_hold is not None:
                    try:
                        engine.release_hold(reason="客户端退出前自动释放锁定实例")
                    except Exception:
                        pass
                self._send_json({"ok": True, "shutting_down": True})
                if callable(engine._server_shutdown_cb):
                    threading.Timer(0.15, engine._server_shutdown_cb).start()
                return

            if path == "/api/config":
                try:
                    updated = engine.update_config(body)
                    self._send_json({"ok": True, "config": updated})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return

            if path == "/api/settings/proxy":
                try:
                    cfg = netcfg.save_proxy_settings(body)
                    resolved = netcfg.apply_proxy_env(cfg)
                    print(f"[{_now_iso()}] [gpu_monitor] 代理设置已更新: {resolved['detail']}", flush=True)
                    self._send_json({"ok": True, "proxy": netcfg.public_view()})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/settings/proxy/test":
                url = body.get("url")
                if url is None:
                    url = netcfg.resolve_proxy()["url"]
                self._send_json(netcfg.test_proxy(str(url or "")))
                return

            if path == "/api/toggle-pause":
                target = body.get("paused")
                if target is None:
                    target = not engine.paused
                paused = engine.set_paused(bool(target))
                self._send_json({"ok": True, "paused": paused})
                return

            if path == "/api/clear-cooldown":
                engine.clear_cooldown(gpu=body.get("gpu"))
                self._send_json({"ok": True})
                return

            if path == "/api/stop-all":
                try:
                    names = body.get("accounts")
                    res = engine.emergency_stop_all(
                        all_accounts=bool(body.get("all_accounts")),
                        accounts=[str(n) for n in names] if isinstance(names, list) and names else None,
                    )
                    self._send_json({"ok": True, **res})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/test-alert":
                payload = {
                    "kind": "test",
                    "gpu": "G4",
                    "label": "G4 (RTX PRO 6000)",
                    "vram": "96 GB",
                    "ts_iso": _now_iso(),
                    "detail": "这是一条测试提醒：当 G4 / A100 放卡时你将收到同样的声音与弹窗通知！",
                    "released": 1,
                    "release_ms": 280,
                }
                engine.broker.publish("alert", payload)
                if engine.config.get("windows_toast", True):
                    _send_windows_toast(
                        "Colab GPU 监控 · 测试提醒",
                        "监控通道正常！一旦检测到 G4 / A100 有卡，将立即声音+弹窗通知你。",
                    )
                self._send_json({"ok": True})
                return

            if path == "/api/clear-history":
                engine.store.clear_all()
                engine.broadcast_state()
                self._send_json({"ok": True})
                return

            self._send_json({"error": "Not found"}, status=404)

    return DashboardHandler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Colab GPU 余量持续监控与实时看板服务")
    p.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址 (默认: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="HTTP 看板端口 (默认: 8765)")
    p.add_argument("--interval", type=int, default=None, help="覆盖轮询间隔/秒 (例如 60)")
    p.add_argument("--gpus", default=None, help="覆盖监控 GPU 列表，逗号分隔 (例如 G4,A100)")
    p.add_argument("-a", "--account", default=None, help="指定 colab-ext 账号名")
    p.add_argument("--open", dest="open_browser", action="store_true", help="启动后自动打开 Windows 浏览器看板")
    p.add_argument("--once", action="store_true", help="仅执行一次命令行探测后退出（不启动 Web 看板）")
    p.add_argument(
        "--no-probe",
        dest="no_probe",
        action="store_true",
        help="只读模式：只刷新账号与额度快照，禁用探测/抢卡（用于安全测试 UI 与 providers）",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    store = ProbeStore(DB_PATH)
    broker = SSEBroker()
    engine = MonitorEngine(store=store, broker=broker, account=args.account, probe_disabled=args.no_probe)

    patch: dict[str, Any] = {}
    if args.interval is not None:
        patch["interval_seconds"] = args.interval
    if args.gpus:
        patch["enabled_gpus"] = [g.strip().upper() for g in args.gpus.split(",") if g.strip()]
    if patch:
        engine.update_config(patch)

    if args.once:
        snap = engine.refresh_account_snapshot()
        bal = snap.get("balance")
        bal_str = f"{bal:.2f} CCU" if isinstance(bal, (int, float)) else "N/A"
        print(
            f"=== Colab GPU 单次余量探测 [{snap['account']}] "
            f"(余额: {bal_str} | 活跃实例: {len(snap['assignments'])}) ===",
            flush=True,
        )
        engine.run_probe_cycle(manual=True)
        return 0

    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine))
    server.daemon_threads = True

    def _shutdown_now():
        engine.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    engine._server_shutdown_cb = _shutdown_now

    def _handle_sig(signum, _frame):
        print(f"\n[{_now_iso()}] [gpu_monitor] 收到退出信号 ({signum})，正在安全停止监控...", flush=True)
        _shutdown_now()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    url = f"http://{args.host}:{args.port}"
    print("==================================================================", flush=True)
    print(f"  Colab GPU 余量监控看板已启动: {url}", flush=True)
    print(f"  监控显卡 : {', '.join(engine.config['enabled_gpus'])}", flush=True)
    print(f"  轮询间隔 : {engine.config['interval_seconds']}s  |  历史库: {DB_PATH}", flush=True)
    print("  防扣费保护: 503 缺货 0 扣费；命中即 <1s 自动秒释并复核 0 残留", flush=True)
    print(f"  网络代理 : {netcfg.resolve_proxy()['detail']}", flush=True)
    if engine.probe_disabled:
        print("  ⚠️ 只读模式 (--no-probe)：已禁用探测与抢卡，仅刷新账号与额度快照", flush=True)
    print("  按 Ctrl+C 可随时安全退出。", flush=True)
    print("==================================================================", flush=True)

    engine.start()
    if args.open_browser:
        threading.Timer(0.8, _open_windows_browser, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        engine.stop()
        server.server_close()
        print(f"[{_now_iso()}] [gpu_monitor] 监控服务已安全停止。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
