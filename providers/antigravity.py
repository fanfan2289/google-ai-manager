"""Google Antigravity quota windows, read from the Antigravity Tools local cache.

Antigravity Tools (desktop app) keeps, per signed-in Google account, the latest
`retrieveUserQuotaSummary` result in `~/.antigravity_tools/accounts/<id>.json` (Windows home).
This provider only reads that cache:

  * no network calls, no tokens — the `token` block of each file is dropped right after
    parsing and never leaves this module;
  * data is as fresh as Antigravity Tools last refreshed it (shown as `last_updated`).

Quota windows: per model group (e.g. "Gemini Models", "Claude and GPT models") a 5-hour and a
weekly bucket with `remaining_fraction` + `reset_time`. A missing fraction means 0 left.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from . import paths
from .base import ERROR, NOT_CONFIGURED, OK, Metric, ProviderSnapshot, QuotaProvider

WINDOW_LABELS = {"5h": "5 小时", "weekly": "每周", "daily": "每日"}
WINDOW_ORDER = {"5h": 0, "daily": 1, "weekly": 2}
STALE_AFTER_S = 6 * 3600


def _iso_from_unix(ts: Any) -> str | None:
    try:
        return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def parse_quota(quota: dict[str, Any]) -> list[Metric]:
    """Antigravity `quota` block -> one percentage Metric per bucket (5h first, then weekly)."""
    metrics: list[Metric] = []
    for group in quota.get("quota_groups") or []:
        gname = str(group.get("display_name") or "额度")
        buckets = sorted(group.get("buckets") or [], key=lambda b: WINDOW_ORDER.get(str(b.get("window")), 9))
        for b in buckets:
            win = str(b.get("window") or "")
            frac = b.get("remaining_fraction")
            frac = 0.0 if b.get("disabled") or not isinstance(frac, (int, float)) else max(0.0, min(1.0, float(frac)))
            pct = round(frac * 100.0, 1)
            metrics.append(
                Metric(
                    key=f"ag_{b.get('bucket_id') or gname + '_' + win}",
                    label=f"{gname} · {WINDOW_LABELS.get(win, win or '窗口')}",
                    unit="%",
                    total=100.0,
                    used=round(100.0 - pct, 1),
                    remaining_fraction=frac,
                    reset_at_iso=b.get("reset_time") or None,
                    detail="已用尽" if b.get("disabled") else "",
                )
            )
    return metrics


class AntigravityProvider(QuotaProvider):
    id = "antigravity"
    label = "Google Antigravity"
    category = "ai_quota"
    official_api = False
    min_refresh_seconds = 30
    docs_url = "https://antigravity.google/"
    description = "5 小时 / 每周窗口剩余额度（读取 Antigravity Tools 本地缓存，不使用任何令牌）"

    def __init__(self, email_of: Callable[[str], str | None] | None = None, root: Path | None = None) -> None:
        self._email_of = email_of
        self._root = root

    def root(self) -> Path | None:
        if self._root is not None:
            return self._root
        home = paths.windows_home()
        return home / ".antigravity_tools" if home else None

    def availability(self) -> tuple[bool, str]:
        root = self.root()
        if root and (root / "accounts.json").is_file():
            return True, ""
        return False, "未检测到 Antigravity Tools 本地数据（~/.antigravity_tools），请先在 Antigravity Tools 登录账号"

    # ------------------------------------------------------------------ cache reading
    def list_accounts(self) -> list[dict[str, Any]]:
        root = self.root()
        if not root:
            return []
        data = paths.read_json(root / "accounts.json", {}) or {}
        current = data.get("current_account_id")
        out = []
        for a in data.get("accounts") or []:
            if isinstance(a, dict) and a.get("id"):
                out.append({
                    "id": str(a["id"]),
                    "email": a.get("email"),
                    "name": a.get("name"),
                    "disabled": bool(a.get("disabled")),
                    "current": a.get("id") == current,
                })
        return out

    def _load_account(self, acct_id: str) -> dict[str, Any]:
        root = self.root()
        safe = "".join(ch for ch in acct_id if ch.isalnum() or ch in "-_")
        raw = paths.read_json(root / "accounts" / f"{safe}.json", {}) if root and safe else {}
        if not isinstance(raw, dict):
            return {}
        # Keep only non-secret fields; the token block is discarded here.
        return {k: raw.get(k) for k in ("quota", "disabled", "banned", "validation_blocked", "email")}

    def snapshot_for_cached_account(self, acct: dict[str, Any], account_label: str) -> ProviderSnapshot:
        data = self._load_account(acct["id"])
        quota = data.get("quota") or {}
        last = quota.get("last_updated")
        extra = {
            "antigravity_account": acct.get("name") or acct.get("email"),
            "email": acct.get("email"),
            "tier": quota.get("subscription_tier"),
            "last_updated_iso": _iso_from_unix(last),
            "stale": isinstance(last, (int, float)) and (dt.datetime.now().timestamp() - float(last)) > STALE_AFTER_S,
            "current_in_tools": acct.get("current", False),
        }
        if data.get("banned") or quota.get("is_forbidden"):
            return ProviderSnapshot(provider=self.id, account=account_label, status=ERROR, extra=extra,
                                    error=f"Antigravity 拒绝访问：{quota.get('forbidden_reason') or '账号被封禁或受限'}")
        metrics = parse_quota(quota)
        if not metrics:
            return ProviderSnapshot(provider=self.id, account=account_label, status=NOT_CONFIGURED, extra=extra,
                                    error="Antigravity Tools 尚未缓存该账号的额度，请在 Antigravity Tools 中刷新一次")
        return ProviderSnapshot(provider=self.id, account=account_label, status=OK, metrics=metrics, extra=extra)

    def snapshot(self, account: str) -> ProviderSnapshot:
        ok, reason = self.availability()
        if not ok:
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED, error=reason)
        email = (self._email_of(account) if self._email_of else None) or (account if "@" in account else None)
        cached = self.list_accounts()
        match = next((a for a in cached if email and str(a.get("email") or "").lower() == email.lower()), None)
        if match is None:
            return ProviderSnapshot(
                provider=self.id, account=account, status=NOT_CONFIGURED,
                error="该账号未在 Antigravity Tools 中登录（按邮箱匹配）" if email else "账号邮箱未知，无法匹配 Antigravity 账号",
            )
        return self.snapshot_for_cached_account(match, account)

    def all_accounts(self) -> list[dict[str, Any]]:
        """Snapshots for every account cached by Antigravity Tools (for the benefits center)."""
        return [self.snapshot_for_cached_account(a, a.get("email") or a["id"]).to_dict() for a in self.list_accounts()]
