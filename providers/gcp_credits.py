"""Google Cloud credits from the Google Developer Program (experimental, opt-in).

Google AI Pro includes monthly Google Cloud credits through the Google Developer Program. There is
no API for a promotional credit's remaining value, so the Windows desktop shell reads it from the
account's own web pages (Developer Program benefits page + Cloud Billing credits page) with the
same dedicated Edge profile used for Flow, and reports only a state and amounts here. Billing
account ids, names and e-mails never reach this module.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import paths
from .base import ERROR, NOT_CONFIGURED, OK, Metric, ProviderSnapshot, QuotaProvider
from .flow_credits import STALE_SECONDS, FlowCreditsStore, FlowReportError, _iso

PRO_MONTHLY_USD = 10
STATES = ("no_billing", "needs_billing", "claimable", "claimed", "active", "billing_no_program_credit", "unknown")
STATE_TEXT = {
    "no_billing": "未领取：需要先创建 Google Cloud 结算账号，再到开发者计划页面领取",
    "needs_billing": "未领取：需要先在开发者计划中关联 Google Cloud 结算账号",
    "claimable": "可领取：到开发者计划页面领取本月赠金",
    "claimed": "已领取，但结算页没有读到剩余金额",
    "billing_no_program_credit": "结算账号中没有找到开发者计划赠金",
    "unknown": "无法判断领取状态",
}
SETUP_URL = "https://developers.google.com/program/my-benefits"
TRIAL_URL = "https://console.cloud.google.com/freetrial"
TRIAL_STATES = ("available", "active", "none", "unknown")
CREDIT_KINDS = ("developer_program", "free_trial", "other")
EXPIRY_WARN_DAYS = 14
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_credits(raw: Any) -> list[dict[str, Any]]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or _money(c.get("remaining")) is None:
            continue
        exp = c.get("expires")
        out.append({
            "kind": c.get("kind") if c.get("kind") in CREDIT_KINDS else "other",
            "remaining": _money(c.get("remaining")),
            "original": _money(c.get("original")),
            "currency": str(c.get("currency") or "$")[:4],
            "expires": exp if isinstance(exp, str) and DATE_RE.match(exp) else None,
            "expired": bool(c.get("expired")),
        })
    return out[:10]


def expiring(credits: list[dict[str, Any]], now: float, days: int = EXPIRY_WARN_DAYS) -> list[dict[str, Any]]:
    """Unexpired credits with money left whose expiry date is within `days`."""
    soon = []
    today = dt.date.fromtimestamp(now)
    for c in credits:
        if c["expired"] or not c["expires"] or not c["remaining"]:
            continue
        left = (dt.date.fromisoformat(c["expires"]) - today).days
        if 0 <= left <= days:
            soon.append({**c, "days_left": left})
    return soon


def store_path() -> Path:
    return paths.app_home() / "gcp_credits.json"


def _money(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= float(v) <= 1_000_000:
        return None
    return round(float(v), 2)


def validate_gcp_report(body: dict[str, Any], known_accounts: list[str], *, now: float | None = None) -> dict[str, Any]:
    account = str(body.get("account") or "").strip()
    if account not in set(known_accounts):
        raise FlowReportError(f"未知账号：{account!r}")
    rec: dict[str, Any] = {"ok": bool(body.get("ok")), "connected": bool(body.get("connected", True)),
                           "checked_at": float(now if now is not None else time.time())}
    if rec["ok"]:
        state = body.get("state")
        if state not in STATES:
            raise FlowReportError("state 非法")
        rec.update(
            state=state,
            billing_accounts=int(body.get("billing_accounts") or 0) if str(body.get("billing_accounts") or 0).isdigit() else 0,
            dev_remaining=_money(body.get("dev_remaining")),
            other_remaining=_money(body.get("other_remaining")),
            currency=str(body.get("currency") or "$")[:4],
            trial_state=body.get("trial_state") if body.get("trial_state") in TRIAL_STATES else "unknown",
            credits=_clean_credits(body.get("credits")),
        )
    else:
        rec["error"] = str(body.get("error") or "读取失败")[:200]
    return {"account": account, "record": rec}


class GcpCreditsProvider(QuotaProvider):
    id = "gcp_credits"
    label = "Google Cloud 赠金"
    category = "cloud"
    official_api = False
    experimental = True
    min_refresh_seconds = 30
    docs_url = "https://developers.google.com/profile/help/benefits"
    description = "开发者计划每月 Cloud 赠金的领取状态与剩余金额（由桌面端专用浏览器配置读取网页，实验性，默认关闭）"

    def __init__(self, store: FlowCreditsStore | None = None, enabled: Callable[[], bool] | None = None) -> None:
        self.store = store or FlowCreditsStore(store_path())
        self._enabled = enabled or (lambda: True)

    def availability(self) -> tuple[bool, str]:
        return (True, "") if self._enabled() else (False, "未开启：设置 → 实验功能 → Google Cloud 赠金")

    def snapshot(self, account: str) -> ProviderSnapshot:
        now = time.time()
        if not self._enabled():
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED,
                                    error="未开启 Cloud 赠金读取（设置 → 实验功能）", extra={"enabled": False})
        rec = self.store.get(account)
        if not rec:
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED,
                                    error="尚未读取（在桌面端账号卡片上「连接」后自动读取）",
                                    extra={"enabled": True, "connected": False})
        checked = float(rec.get("checked_at") or 0)
        extra = {"enabled": True, "connected": bool(rec.get("connected", True)), "checked_at_iso": _iso(checked),
                 "stale": now - checked > 4 * STALE_SECONDS, "setup_url": SETUP_URL}
        if not rec.get("ok"):
            return ProviderSnapshot(provider=self.id, account=account, status=ERROR,
                                    error=str(rec.get("error") or "读取失败"), extra=extra)
        state = rec.get("state") or "unknown"
        credits = rec.get("credits") or []
        extra.update(state=state, billing_accounts=rec.get("billing_accounts", 0), currency=rec.get("currency", "$"),
                     trial_state=rec.get("trial_state", "unknown"), trial_url=TRIAL_URL, credits=credits,
                     expiring=expiring(credits, now))
        trial = [c for c in credits if c["kind"] == "free_trial" and not c["expired"]]
        trial_metric = ([Metric(key="gcp_trial_credit", label="免费试用赠金剩余", unit=trial[0]["currency"],
                                remaining=sum(c["remaining"] for c in trial), detail="新用户 $300，90 天有效")]
                        if trial else [])
        if state == "active" and rec.get("dev_remaining") is not None:
            remaining = float(rec["dev_remaining"])
            metric = Metric(key="gcp_dev_credit", label="开发者计划赠金剩余", unit=rec.get("currency", "$"),
                            remaining=remaining,
                            detail=f"AI Pro 每月 ${PRO_MONTHLY_USD}（每笔自发放起 12 个月有效）")
            metrics = [metric, *trial_metric]
            if rec.get("other_remaining") and not trial_metric:
                metrics.append(Metric(key="gcp_other_credit", label="其他赠金剩余", unit=rec.get("currency", "$"),
                                      remaining=float(rec["other_remaining"])))
            return ProviderSnapshot(provider=self.id, account=account, status=OK, metrics=metrics,
                                    updated_at_iso=_iso(checked) or "", extra=extra)
        extra["state_text"] = STATE_TEXT.get(state, STATE_TEXT["unknown"])
        return ProviderSnapshot(provider=self.id, account=account, status=OK, metrics=trial_metric,
                                updated_at_iso=_iso(checked) or "", extra=extra)
