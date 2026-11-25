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
UI_HTML_PATH = SCRIPT_DIR / "monitor_ui.html"


def _ensure_wsl_proxy_env() -> None:
    """Ensure NO_PROXY covers localhost and auto-detect local Windows Clash proxy if unset."""
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")
    if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
        for port in (7897, 7890, 10809):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    proxy_url = f"http://127.0.0.1:{port}"
                    os.environ["HTTP_PROXY"] = proxy_url
                    os.environ["HTTPS_PROXY"] = proxy_url
                    os.environ["http_proxy"] = proxy_url
                    os.environ["https_proxy"] = proxy_url
                    break
            except OSError:
                continue


_ensure_wsl_proxy_env()


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


GPU_CATALOG: list[dict[str, Any]] = [
    {
        "id": "G4",
        "label": "G4 (RTX PRO 6000)",
        "vram": "96 GB",
        "ccu_hint": "~10–14 CCU/hr",
        "tier": "primary",
        "note": "MiniMax-H3 首选 · 满血 96GB 显存",
        "default_enabled": True,
    },
    {
        "id": "A100",
        "label": "A100",
        "vram": "40 / 80 GB",
        "ccu_hint": "~7–12 CCU/hr",
        "tier": "secondary",
        "note": "MiniMax-H3 备选 · 高速算力",
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
    # 命中即锁定（自动占卡 + 自动挂盘 + 超时自动关机）
    "hold_on_hit": True,
    "hold_gpus": ["G4", "A100"],
    "hold_minutes": 15,
    "auto_mount_drive": True,
    "hold_session_name": "h3work",
    "manual_claim_max_minutes": 120,
}


def _now_ts() -> float:
    return time.time()


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

    def __init__(self, store: ProbeStore, broker: SSEBroker, account: str | None = None) -> None:
        self.store = store
        self.broker = broker
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
        self.session_start_ccu: float | None = None
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
        if CONFIG_PATH.is_file():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
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
        cfg["hold_session_name"] = str(cfg.get("hold_session_name") or "h3work").strip() or "h3work"
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
                val = str(patch["hold_session_name"] or "h3work").strip()
                if val:
                    self.config["hold_session_name"] = val
            for bool_key in (
                "pause_gpu_on_hit",
                "skip_when_active_vm",
                "windows_toast",
                "browser_sound",
                "browser_notify",
                "hold_on_hit",
                "auto_mount_drive",
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

    def trigger_manual_probe(self, gpu: str | None = None) -> None:
        if gpu and gpu not in GPU_META_BY_ID:
            raise ValueError(f"Unsupported GPU: {gpu}")
        self._manual_queue.append([gpu] if gpu else None)
        self._wake_event.set()

    def _get_account_state(self):
        states = colab_meter._iter_states(self.config.get("account"))
        return states[0]

    def refresh_account_snapshot(self) -> dict[str, Any]:
        acct_name, st = self._get_account_state()
        snap = colab_meter._query_account(acct_name, st)
        eligible_gpus: list[str] = []
        ineligible_gpus: list[str] = []
        eligible_tpus: list[str] = []
        try:
            with colab_meter._activate_ext_context(acct_name):
                raw_ccu = st.client._issue_request(
                    f"{st.client.colab_domain}/tun/m/ccu-info",
                    schema=dict,
                )
                if isinstance(raw_ccu, dict):
                    eligible_gpus = [str(x).upper() for x in (raw_ccu.get("eligibleGpus") or [])]
                    ineligible_gpus = [str(x).upper() for x in (raw_ccu.get("ineligibleGpus") or [])]
                    eligible_tpus = [str(x).upper() for x in (raw_ccu.get("eligibleTpus") or [])]
        except Exception:
            pass

        sanitized = {
            "account": snap["account"],
            "balance": snap["balance"],
            "rate_hourly": snap["rate_hourly"],
            "assignments": [
                {
                    "name": item["name"],
                    "endpoint": item["endpoint"],
                    "hardware": item["hardware"],
                    "shape": item["shape"],
                    "variant": item["variant"],
                }
                for item in snap["assignments"]
            ],
            "eligible_gpus": eligible_gpus,
            "ineligible_gpus": ineligible_gpus,
            "eligible_tpus": eligible_tpus,
            "usage_error": snap["usage_error"],
            "updated_at_iso": _now_iso(),
        }
        with self._state_lock:
            self.account_snapshot = sanitized
            if self.session_start_ccu is None and isinstance(sanitized["balance"], (int, float)):
                self.session_start_ccu = float(sanitized["balance"])
        return sanitized

    def emergency_stop_all(self) -> dict[str, Any]:
        # Stop any running drama subprocess first so it doesn't race
        self._terminate_job_proc_only()
        with self._probe_lock:
            acct_name, st = self._get_account_state()
            killed, remaining = colab_meter._terminate_account_sessions(
                acct_name=acct_name,
                st=st,
                target_session=None,
                kill_orphans=True,
            )
            snap = self.refresh_account_snapshot()
            with self._state_lock:
                if not remaining:
                    self.critical_alert = None
                    self.active_hold = None
                    self._save_hold_state_locked()
            self.broadcast_state()
            return {
                "account": acct_name,
                "killed": killed,
                "remaining_count": len(remaining),
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
            detail = "Google Drive 已挂载至 /content/drive（模型与剧本已就绪）" if ok else f"挂盘失败 (rc={proc.returncode}): {combined[-200:]}"
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
                raise ValueError("短剧任务运行中，已由任务看门狗自动托管")
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
                raise ValueError("短剧任务正在运行中")
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
            acct_name, st = self._get_account_state()
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

    def grab_gpu_now(self, gpu_id: str, hold_minutes: int | None = None) -> dict[str, Any]:
        """Immediately attempt a single-shot allocation of `gpu_id` and lock + auto-mount if available."""
        gpu_id = str(gpu_id or "").strip().upper()
        if gpu_id not in GPU_META_BY_ID:
            raise ValueError(f"Unsupported GPU: {gpu_id}")
        with self._state_lock:
            if self.active_hold is not None:
                raise ValueError(f"当前已锁定 {self.active_hold['gpu']} ({self.active_hold['session_name']})，请先释放或使用当前实例")

        with self._probe_lock:
            with self._state_lock:
                self.probing = True
            self.broadcast_state()
            try:
                acct_name, st = self._get_account_state()
                snap = self.refresh_account_snapshot()
                rec = self.probe_single_gpu(
                    gpu_id,
                    snap,
                    acct_name=acct_name,
                    st=st,
                    manual=True,
                    force_hold=True,
                    hold_minutes_override=hold_minutes,
                )
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

    def start_drama_job(
        self,
        *,
        workflow: str | None = None,
        config_file: str | None = None,
        max_minutes: int = 180,
    ) -> dict[str, Any]:
        """Claim the active locked GPU (or start fresh) and launch `wsl_run_h3.sh --reuse-session`."""
        with self._state_lock:
            hold = dict(self.active_hold) if self.active_hold else None
            if self._job_proc is not None and self._job_proc.poll() is None:
                raise ValueError("已有短剧推理任务正在运行中")
            if not hold:
                raise ValueError("当前没有已锁定的 GPU 实例，请先抢到/锁定 G4 或 A100 后再一键接管")

        run_script = SCRIPT_DIR / "wsl_run_h3.sh"
        if not run_script.is_file():
            raise ValueError(f"未找到推理脚本: {run_script}")

        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = LOGS_DIR / f"desktop_drama_{ts}.log"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        cmd = [
            "bash",
            str(run_script),
            "--reuse-session",
            "-s",
            hold["session_name"],
            "-g",
            hold["gpu"],
            "--fallback-gpu",
            "none",
            "--max-minutes",
            str(max(30, min(360, int(max_minutes)))),
        ]
        if hold.get("account"):
            cmd.extend(["-a", str(hold["account"])])
        if workflow:
            cmd.extend(["--workflow", str(workflow)])
        if config_file:
            cmd.extend(["--config", str(config_file)])

        log_fh = open(job_log, "w", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=self._colab_env(),
        )
        self._job_proc = proc

        with self._state_lock:
            self.last_job_log_path = str(job_log)
            self.last_job_status = "RUNNING"
            if self.active_hold:
                self.active_hold["state"] = "CLAIMED_JOB"
                self.active_hold["job_pid"] = proc.pid
                self.active_hold["job_log_path"] = str(job_log)
                self.active_hold["job_status"] = "RUNNING"
                self.active_hold["expires_at_ts"] = _now_ts() + max_minutes * 60.0
                self.active_hold["expires_at_iso"] = _now_iso(self.active_hold["expires_at_ts"])
                self._save_hold_state_locked()
            hold_copy = dict(self.active_hold) if self.active_hold else None

        print(f"[{_now_iso()}] [gpu_monitor] 已接管 {hold['gpu']} 启动短剧任务 (PID={proc.pid}, log={job_log})", flush=True)
        self.broadcast_state()
        return {"ok": True, "pid": proc.pid, "log_path": str(job_log), "hold": hold_copy}

    def stop_drama_job(self) -> dict[str, Any]:
        """Stop running drama job and force-release all Colab GPU VMs."""
        return self.release_hold(reason="用户手动中止短剧任务并回收 GPU")

    def get_job_log(self, tail_bytes: int = 32768) -> dict[str, Any]:
        with self._state_lock:
            path_str = (self.active_hold or {}).get("job_log_path") or self.last_job_log_path
            status = (self.active_hold or {}).get("job_status") or self.last_job_status or "IDLE"
        if not path_str:
            # Fallback to newest run_h3work_*.log if present
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
        """1s background watchdog: enforces hold countdown auto-shutdown & monitors claimed drama jobs."""
        last_sync_ts = 0.0
        while not self._stop_event.is_set():
            try:
                now = _now_ts()
                with self._state_lock:
                    hold = dict(self.active_hold) if self.active_hold else None

                if hold:
                    state = hold.get("state")
                    # 1) Check claimed drama job completion
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
                                reason="短剧推理任务已完成并自动回收 GPU" if ok_job else f"短剧推理任务异常结束 (rc={rc})，已安全回收 GPU"
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
                        cfg_auto_mount = bool(self.config.get("auto_mount_drive", True))
                        cfg_session = str(self.config.get("hold_session_name") or "h3work")
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
        with self._probe_lock:
            with self._state_lock:
                self.probing = True
            self.broadcast_state()

            results: list[dict[str, Any]] = []
            try:
                acct_name, st = self._get_account_state()
                snap = self.refresh_account_snapshot()
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
                    if rec["status"] == "RATE_LIMITED":
                        break

                if any_hit:
                    # Post-hit refresh to update live balance & active VM list
                    self.refresh_account_snapshot()
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
            start_ccu = self.session_start_ccu
            started_ts = self.started_ts
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
            "paused": paused,
            "probing": probing,
            "current_probe_gpu": cur_gpu,
            "next_probe_ts": next_ts,
            "next_probe_in_s": max(0, int(next_ts - now)) if not paused else None,
            "rate_limit_remaining_s": max(0, int(rl_until - now)),
            "critical_alert": crit,
            "account": snap,
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
                        "version": "2.0-hold",
                        "server_time_iso": _now_iso(),
                        "has_active_hold": engine.active_hold is not None,
                    }
                )
                return

            if path == "/api/state":
                self._send_json(engine.build_state_payload())
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

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json({"error": f"Invalid JSON: {exc}"}, status=400)
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
                    rec = engine.grab_gpu_now(gpu, hold_minutes=hold_mins)
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

            if path == "/api/hold/run-drama":
                try:
                    res = engine.start_drama_job(
                        workflow=body.get("workflow"),
                        config_file=body.get("config"),
                        max_minutes=int(body.get("max_minutes", 180)),
                    )
                    self._send_json(res)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return

            if path == "/api/job/stop":
                try:
                    res = engine.stop_drama_job()
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
                    res = engine.emergency_stop_all()
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
    return p


def main() -> int:
    args = build_parser().parse_args()
    store = ProbeStore(DB_PATH)
    broker = SSEBroker()
    engine = MonitorEngine(store=store, broker=broker, account=args.account)

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
