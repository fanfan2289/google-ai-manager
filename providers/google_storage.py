"""Google One / Drive storage provider (official Drive v3 `about.storageQuota`, read-only).

Reuses the per-account DriveFS refresh token that `colab dfs consent` already stores
(it carries the `drive` scope), so no extra authorisation is needed. The storage pool is
shared by Drive, Gmail and Photos, so this is the account's whole Google One quota.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .base import ERROR, Metric, NOT_CONFIGURED, OK, ProviderSnapshot, QuotaProvider, UNAUTHORIZED, now_iso

ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=storageQuota,user(emailAddress,displayName)"
GIB = 1024 ** 3


def _dfs():
    from colab_ext import accounts, dfs_creds  # type: ignore  # noqa: PLC0415

    return accounts, dfs_creds


class _TokenCache:
    """In-memory access-token cache keyed by Drive account (never persisted)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item and item[1] - time.time() > 60:
                return item[0]
            return None

    def put(self, key: str, token: str, expires_in: int) -> None:
        with self._lock:
            self._items[key] = (token, time.time() + max(60, int(expires_in)))

    def drop(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


def refresh_failure_reason(exc: Exception) -> str | None:
    """User-facing reason when a stored DriveFS refresh token can no longer mint a token."""
    text = str(exc)
    if "invalid_scope" in text:
        # The consent was granted without the Drive scope (checkbox left unticked).
        return "云盘授权缺少 Google Drive 权限，请点「重新授权云盘」并在 Google 授权页勾选 Drive 访问权限"
    if "invalid_grant" in text:
        return "云盘授权已失效或被撤销，请点「重新授权云盘」"
    return None


def parse_storage_quota(about: dict[str, Any]) -> list[Metric]:
    q = about.get("storageQuota") or {}

    def num(key: str) -> float | None:
        try:
            return float(q[key])
        except (KeyError, TypeError, ValueError):
            return None

    limit = num("limit")  # absent => unlimited storage (e.g. some Workspace plans)
    usage = num("usage")
    in_drive = num("usageInDrive")
    in_trash = num("usageInDriveTrash")
    metrics = [
        Metric(key="storage_total", label="总存储（Drive + Gmail + Photos）", unit="bytes",
               used=usage, total=limit, detail="" if limit is not None else "无上限"),
        Metric(key="storage_drive", label="Drive 占用", unit="bytes", used=in_drive),
        Metric(key="storage_trash", label="Drive 回收站", unit="bytes", used=in_trash),
    ]
    if usage is not None and in_drive is not None:
        metrics.append(Metric(key="storage_other", label="Gmail + Photos 等", unit="bytes",
                              used=max(0.0, usage - in_drive)))
    return metrics


class GoogleStorageProvider(QuotaProvider):
    id = "google_storage"
    label = "Google One 存储"
    category = "storage"
    official_api = True
    min_refresh_seconds = 300
    docs_url = "https://one.google.com/storage"
    description = "Drive / Gmail / Photos 共享存储容量（Pro 5 TB 或 10 TB）"

    def __init__(self) -> None:
        self._tokens = _TokenCache()

    def availability(self) -> tuple[bool, str]:
        try:
            _dfs()
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"colab-ext 未安装或不可导入: {exc}"

    def _access_token(self, account: str) -> tuple[str | None, str]:
        accounts, dfs_creds = _dfs()
        _path, _colab_acct, drive_acct = dfs_creds.store_path(account=account or None, for_consent=False)
        cached = self._tokens.get(drive_acct)
        if cached:
            return cached, drive_acct
        data = dfs_creds.load(account=account or None)
        if not data.get("refresh_token"):
            return None, drive_acct
        token, expires_in = dfs_creds.current_access_token(data)
        self._tokens.put(drive_acct, token, expires_in)
        return token, drive_acct

    def snapshot(self, account: str) -> ProviderSnapshot:
        try:
            token, drive_acct = self._access_token(account)
        except Exception as exc:  # noqa: BLE001 - google.auth RefreshError and friends
            reason = refresh_failure_reason(exc)
            if reason is None:
                raise
            return ProviderSnapshot(provider=self.id, account=account, status=UNAUTHORIZED, error=reason)
        extra: dict[str, Any] = {"drive_account": drive_acct}
        if not token:
            return ProviderSnapshot(
                provider=self.id, account=account, status=NOT_CONFIGURED, extra=extra,
                error="尚未完成 Google Drive 授权（运行首次授权或 `colab dfs consent`）",
            )
        req = urllib.request.Request(ABOUT_URL, headers={"Authorization": f"Bearer {token}"})
        # Fresh opener per call so runtime proxy changes (os.environ) take effect immediately.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler())
        try:
            with opener.open(req, timeout=15) as resp:
                about = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self._tokens.drop(drive_acct)
                return ProviderSnapshot(provider=self.id, account=account, status=UNAUTHORIZED, extra=extra,
                                        error=f"Drive 授权失效或缺少权限 (HTTP {exc.code})，请重新授权")
            return ProviderSnapshot(provider=self.id, account=account, status=ERROR, extra=extra,
                                    error=f"Drive API HTTP {exc.code}")
        user = about.get("user") or {}
        extra["email"] = user.get("emailAddress")
        extra["display_name"] = user.get("displayName")
        return ProviderSnapshot(provider=self.id, account=account, status=OK,
                                metrics=parse_storage_quota(about), extra=extra, updated_at_iso=now_iso())


__all__ = ["GoogleStorageProvider", "parse_storage_quota", "GIB"]
