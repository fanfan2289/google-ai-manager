"""Google AI Manager quota providers.

`ProviderHub` keeps the registered providers, caches their snapshots per (provider, account)
with a TTL, serialises concurrent refreshes and turns provider exceptions into error snapshots.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .base import ERROR, Metric, ProviderSnapshot, QuotaProvider, UNAVAILABLE, now_iso

__all__ = [
    "Metric",
    "PLANNED_PROVIDERS",
    "ProviderHub",
    "ProviderSnapshot",
    "QuotaProvider",
]

# Roadmap entries shown in the UI so users can see what is coming (no code behind them yet).
PLANNED_PROVIDERS: list[dict[str, Any]] = [
]


class ProviderHub:
    def __init__(self, ttl_seconds: int = 120, max_workers: int = 4) -> None:
        self.ttl_seconds = ttl_seconds
        self._providers: dict[str, QuotaProvider] = {}
        self._cache: dict[tuple[str, str], tuple[float, ProviderSnapshot]] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="provider")

    # -------------------------------------------------------------- registry
    def register(self, provider: QuotaProvider) -> QuotaProvider:
        if not provider.id:
            raise ValueError("provider.id is required")
        self._providers[provider.id] = provider
        return provider

    def get(self, provider_id: str) -> QuotaProvider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ValueError(f"Unknown provider: {provider_id}") from None

    def ids(self) -> list[str]:
        return list(self._providers)

    def describe_all(self) -> list[dict[str, Any]]:
        return [p.describe() for p in self._providers.values()]

    # -------------------------------------------------------------- snapshots
    def _lock_for(self, key: tuple[str, str]) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def cached(self, provider_id: str, account: str) -> ProviderSnapshot | None:
        hit = self._cache.get((provider_id, account))
        return hit[1] if hit else None

    def snapshot(self, provider_id: str, account: str, *, force: bool = False) -> ProviderSnapshot:
        provider = self.get(provider_id)
        key = (provider_id, account)
        ttl = max(self.ttl_seconds, provider.min_refresh_seconds)
        hit = self._cache.get(key)
        # Even a forced refresh is throttled to protect undocumented endpoints.
        floor = provider.min_refresh_seconds if not provider.official_api else 5
        if hit and (time.time() - hit[0] < (floor if force else ttl)):
            return hit[1]
        with self._lock_for(key):
            hit = self._cache.get(key)
            if hit and (time.time() - hit[0] < (floor if force else ttl)):
                return hit[1]
            available, reason = provider.availability()
            if not available:
                snap = ProviderSnapshot(provider=provider_id, account=account, status=UNAVAILABLE, error=reason)
            else:
                try:
                    snap = provider.snapshot(account)
                except Exception as exc:  # noqa: BLE001 - provider failures must not crash the hub
                    snap = ProviderSnapshot(
                        provider=provider_id,
                        account=account,
                        status=ERROR,
                        error=f"{type(exc).__name__}: {str(exc)[:240]}",
                        updated_at_iso=now_iso(),
                    )
            self._cache[key] = (time.time(), snap)
            return snap

    def snapshot_many(self, account: str, provider_ids: list[str] | None = None, *, force: bool = False) -> dict[str, dict[str, Any]]:
        ids = provider_ids or self.ids()
        futures = {pid: self._pool.submit(self.snapshot, pid, account, force=force) for pid in ids}
        return {pid: fut.result().to_dict() for pid, fut in futures.items()}

    def put(self, snap: ProviderSnapshot) -> None:
        """Seed the cache with a snapshot produced elsewhere (e.g. the monitor engine)."""
        self._cache[(snap.provider, snap.account)] = (time.time(), snap)

    def invalidate(self, account: str | None = None) -> None:
        """Drop cached snapshots (for one account, or all) after its credentials changed."""
        for key in list(self._cache):
            if account is None or key[1] == account:
                self._cache.pop(key, None)
