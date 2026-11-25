"""Colab provider: CCU balance, burn rate, live VMs and GPU eligibility (read-only).

Only the *read* side of Colab lives here. Probing, holding and force-releasing GPUs — the
anti-ghost-billing core — stays in `scripts/colab_gpu_monitor.py` / `scripts/colab_meter.py`.

Must run inside the `google-colab-cli` uv venv (the backend re-execs into it); the
`colab_meter` module is imported lazily so this file can be imported anywhere.
"""

from __future__ import annotations

from typing import Any

from .base import Metric, OK, ProviderSnapshot, QuotaProvider, UNAUTHORIZED, now_iso


def _meter():
    import colab_meter  # type: ignore  # noqa: PLC0415 - lives in scripts/, needs colab venv

    return colab_meter


def resolve_account_state(account: str | None):
    """Return `(account_name, colab_cli State)` for `account` (None = active account)."""
    return _meter()._iter_states(account)[0]


def fetch_account_snapshot(acct_name: str, st: Any) -> dict[str, Any]:
    """Live, token-free Colab account snapshot (same shape the dashboard has always used)."""
    meter = _meter()
    snap = meter._query_account(acct_name, st)
    eligible_gpus: list[str] = []
    ineligible_gpus: list[str] = []
    eligible_tpus: list[str] = []
    try:
        with meter._activate_ext_context(acct_name):
            raw_ccu = st.client._issue_request(f"{st.client.colab_domain}/tun/m/ccu-info", schema=dict)
            if isinstance(raw_ccu, dict):
                eligible_gpus = [str(x).upper() for x in (raw_ccu.get("eligibleGpus") or [])]
                ineligible_gpus = [str(x).upper() for x in (raw_ccu.get("ineligibleGpus") or [])]
                eligible_tpus = [str(x).upper() for x in (raw_ccu.get("eligibleTpus") or [])]
    except Exception:
        pass

    return {
        "account": snap["account"],
        "balance": snap["balance"],
        "rate_hourly": snap["rate_hourly"],
        # Deliberately drop proxy_url / proxy_token: never expose runtime credentials.
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
        "updated_at_iso": now_iso(),
    }


def snapshot_from_account_data(data: dict[str, Any]) -> ProviderSnapshot:
    """Convert a `fetch_account_snapshot()` dict into a generic provider snapshot."""
    bal = data.get("balance")
    rate = data.get("rate_hourly")
    vms = data.get("assignments") or []
    usage_err = data.get("usage_error")
    metrics = [
        Metric(key="ccu_balance", label="CCU 余额", unit="CCU",
               remaining=float(bal) if isinstance(bal, (int, float)) else None),
        Metric(key="burn_rate", label="实时燃速", unit="CCU/hr",
               used=float(rate) if isinstance(rate, (int, float)) else None),
        Metric(key="active_vms", label="活跃实例", unit="VM", used=float(len(vms)),
               detail=", ".join(f"{v.get('hardware')}({v.get('name')})" for v in vms)),
    ]
    status = OK
    if usage_err and bal is None and any(s in str(usage_err) for s in ("401", "403", "invalid_grant")):
        status = UNAUTHORIZED
    return ProviderSnapshot(
        provider="colab",
        account=str(data.get("account") or ""),
        status=status,
        metrics=metrics,
        error=usage_err if status != OK else None,
        updated_at_iso=data.get("updated_at_iso") or now_iso(),
        extra={
            "assignments": vms,
            "eligible_gpus": data.get("eligible_gpus") or [],
            "ineligible_gpus": data.get("ineligible_gpus") or [],
            "eligible_tpus": data.get("eligible_tpus") or [],
            "usage_warning": usage_err,
        },
    )


class ColabProvider(QuotaProvider):
    id = "colab"
    label = "Google Colab"
    category = "compute"
    official_api = False  # colab-cli talks to Colab's internal tunnel API
    min_refresh_seconds = 20
    docs_url = "https://colab.research.google.com/signup"
    description = "CCU 余额、实时燃速、活跃 VM 与可用 GPU 资格（Pro 每月 200 CCU）"

    def availability(self) -> tuple[bool, str]:
        try:
            _meter()
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"colab-cli 环境不可用: {exc}"

    def snapshot(self, account: str) -> ProviderSnapshot:
        acct_name, st = resolve_account_state(account or None)
        return snapshot_from_account_data(fetch_account_snapshot(acct_name, st))
