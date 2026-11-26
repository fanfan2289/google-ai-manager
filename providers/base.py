"""Unified quota-provider interface.

Every Google quota source (Colab CCU, Google One storage, Antigravity windows, Flow credits
requests, ...) is a `QuotaProvider` that turns one account into a `ProviderSnapshot` made of
generic `Metric`s, so the UI can render any provider as a card without special-casing it.

Rules for implementations:
  * `snapshot()` must be read-only: never allocate, mutate or delete remote resources.
  * Never put access tokens / refresh tokens / API keys into a snapshot.
  * Raise freely: `ProviderHub` converts exceptions into `status="error"` snapshots.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

# Snapshot status values
OK = "ok"
ERROR = "error"
UNAUTHORIZED = "unauthorized"      # credentials exist but were rejected / expired
NOT_CONFIGURED = "not_configured"  # this account never authorised this provider
UNAVAILABLE = "unavailable"        # provider dependencies missing on this machine


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Metric:
    key: str
    label: str
    unit: str = ""
    used: float | None = None
    total: float | None = None
    remaining: float | None = None
    # 0..1 remaining fraction when the source only exposes a ratio (e.g. rate-limit windows)
    remaining_fraction: float | None = None
    reset_at_iso: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.remaining is None and self.total is not None and self.used is not None:
            self.remaining = max(0.0, float(self.total) - float(self.used))
        if self.remaining_fraction is None and self.total and self.remaining is not None:
            self.remaining_fraction = max(0.0, min(1.0, float(self.remaining) / float(self.total)))


@dataclass
class ProviderSnapshot:
    provider: str
    account: str
    status: str = OK
    metrics: list[Metric] = field(default_factory=list)
    error: str | None = None
    updated_at_iso: str = field(default_factory=now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QuotaProvider(ABC):
    id: str = ""
    label: str = ""
    category: str = "other"        # compute | storage | ai_quota | other
    official_api: bool = True      # False = undocumented / reverse-engineered endpoint
    experimental: bool = False     # experimental providers are disabled by default
    min_refresh_seconds: int = 60  # cache TTL floor enforced by ProviderHub
    docs_url: str = ""
    description: str = ""

    def availability(self) -> tuple[bool, str]:
        """(available, reason). Override to check optional dependencies."""
        return True, ""

    @abstractmethod
    def snapshot(self, account: str) -> ProviderSnapshot:
        """Return a read-only quota snapshot for `account`."""

    def describe(self) -> dict[str, Any]:
        available, reason = self.availability()
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "official_api": self.official_api,
            "experimental": self.experimental,
            "min_refresh_seconds": self.min_refresh_seconds,
            "docs_url": self.docs_url,
            "description": self.description,
            "available": available,
            "unavailable_reason": reason,
        }
