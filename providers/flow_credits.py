"""Google Flow credit balance (experimental, opt-in).

Flow has no public API. The Windows desktop shell reads the balance through a dedicated,
per-account Edge profile (`desktop_app/flow_credits.py`: flow.google.com's own read-only
`nzlxg` RPC, run inside the page) and reports only the number here. This module validates those
reports, keeps the last one per account in `~/.google-ai-manager/flow_credits.json` (0600) and
exposes it as a regular quota provider. No token, cookie or e-mail ever reaches this module.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import paths
from .base import ERROR, NOT_CONFIGURED, OK, Metric, ProviderSnapshot, QuotaProvider

PRO_MONTHLY_CREDITS = 1000
STALE_SECONDS = 3 * 3600
MAX_CREDITS = 10_000_000


class FlowReportError(ValueError):
    pass


def store_path() -> Path:
    return paths.app_home() / "flow_credits.json"


def validate_report(body: dict[str, Any], known_accounts: list[str], *, now: float | None = None) -> dict[str, Any]:
    """Keep only the fields we expect from the desktop reader, with types checked."""
    account = str(body.get("account") or "").strip()
    if account not in set(known_accounts):
        raise FlowReportError(f"未知账号：{account!r}")
    rec: dict[str, Any] = {
        "ok": bool(body.get("ok")),
        "connected": bool(body.get("connected", True)),
        "checked_at": float(now if now is not None else time.time()),
    }
    if rec["ok"]:
        credits = body.get("credits")
        if isinstance(credits, bool) or not isinstance(credits, int) or not 0 <= credits <= MAX_CREDITS:
            raise FlowReportError("credits 必须是非负整数")
        rec["credits"] = credits
        rec["email_verified"] = bool(body.get("email_verified"))
    else:
        rec["error"] = str(body.get("error") or "读取失败")[:200]
    return {"account": account, "record": rec}


class FlowCreditsStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or store_path()

    def load(self) -> dict[str, Any]:
        data = paths.read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def get(self, account: str) -> dict[str, Any] | None:
        rec = self.load().get(account)
        return rec if isinstance(rec, dict) else None

    def put(self, account: str, rec: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            prev = data.get(account) if isinstance(data.get(account), dict) else {}
            if not rec.get("ok") and isinstance(prev.get("credits"), int):
                # Keep the last good balance visible next to the new error.
                rec = {**rec, "last_credits": prev["credits"], "last_ok_at": prev.get("checked_at")}
            data[account] = rec
            paths.write_private_json(self.path, data)
        return rec

    def forget(self, account: str) -> None:
        with self._lock:
            data = self.load()
            if data.pop(account, None) is not None:
                paths.write_private_json(self.path, data)


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class FlowCreditsProvider(QuotaProvider):
    id = "flow_credits"
    label = "Google Flow 积分"
    category = "ai_quota"
    official_api = False
    experimental = True
    min_refresh_seconds = 30
    docs_url = "https://support.google.com/flow/answer/16526234"
    description = "Flow 网页自身的只读余额查询（由桌面端专用浏览器配置读取，实验性，默认关闭）"

    def __init__(self, store: FlowCreditsStore | None = None, enabled: Callable[[], bool] | None = None) -> None:
        self.store = store or FlowCreditsStore()
        self._enabled = enabled or (lambda: True)

    def availability(self) -> tuple[bool, str]:
        if not self._enabled():
            return False, "未开启：设置 → 实验功能 → Flow 积分"
        return True, ""

    def snapshot(self, account: str) -> ProviderSnapshot:
        now = time.time()
        if not self._enabled():
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED,
                                    error="未开启 Flow 积分读取（设置 → 实验功能）", extra={"enabled": False})
        rec = self.store.get(account)
        if not rec:
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED,
                                    error="尚未连接 Flow（在桌面端账号卡片上点「连接 Flow」）",
                                    extra={"enabled": True, "connected": False})
        checked = float(rec.get("checked_at") or 0)
        extra = {"enabled": True, "connected": bool(rec.get("connected", True)), "checked_at_iso": _iso(checked),
                 "stale": now - checked > STALE_SECONDS, "email_verified": bool(rec.get("email_verified"))}
        if not rec.get("ok"):
            if isinstance(rec.get("last_credits"), int):
                extra.update(last_credits=rec["last_credits"], last_ok_at_iso=_iso(rec.get("last_ok_at")))
            status = NOT_CONFIGURED if not extra["connected"] else ERROR
            return ProviderSnapshot(provider=self.id, account=account, status=status,
                                    error=str(rec.get("error") or "读取失败"), extra=extra)
        credits = int(rec["credits"])
        metric = Metric(
            key="flow_credits", label="Flow 积分余额", unit="积分", remaining=float(credits),
            total=float(PRO_MONTHLY_CREDITS) if credits <= PRO_MONTHLY_CREDITS else None,
            detail=f"AI Pro 每月 {PRO_MONTHLY_CREDITS} 积分（按订阅周期发放，不累积）；余额含额外购买的积分",
        )
        if metric.total:
            metric.used = metric.total - credits
        return ProviderSnapshot(provider=self.id, account=account, status=OK, metrics=[metric],
                                updated_at_iso=_iso(checked) or "", extra=extra)
