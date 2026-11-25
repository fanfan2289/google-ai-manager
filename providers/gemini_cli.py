"""Gemini CLI status (local files only).

Gemini CLI does not keep a usage counter on disk, and its live quota endpoint would need the
CLI's OAuth token — this app deliberately never reads or uses another tool's tokens. So this
provider reports what can be known safely: whether the CLI is installed, whether a login exists
(file presence only — the credential file is never opened), which Google account it is signed in
to, and the published Google AI Pro daily request allowance.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from . import paths
from .base import NOT_CONFIGURED, OK, Metric, ProviderSnapshot, QuotaProvider

PRO_DAILY_REQUESTS = 1500  # Google AI Pro: Gemini CLI / Code Assist requests per day (published)


def _homes() -> list[Path]:
    out: list[Path] = []
    for h in (paths.windows_home(), Path.home()):
        if h and h not in out:
            out.append(h)
    return out


class GeminiCliProvider(QuotaProvider):
    id = "gemini_cli"
    label = "Gemini CLI"
    category = "ai_quota"
    official_api = True  # no remote call at all; local status only
    min_refresh_seconds = 30
    docs_url = "https://github.com/google-gemini/gemini-cli"
    description = "安装与登录状态 + AI Pro 每日请求额度（CLI 不在本地记录用量，无法读取实时剩余）"

    def __init__(self, email_of: Callable[[str], str | None] | None = None, homes: list[Path] | None = None) -> None:
        self._email_of = email_of
        self._homes = homes

    def homes(self) -> list[Path]:
        return self._homes if self._homes is not None else _homes()

    def installed(self) -> str | None:
        for h in self.homes():
            for cand in (h / "AppData/Roaming/npm/gemini.cmd", h / ".local/bin/gemini"):
                if cand.is_file():
                    return str(cand)
        return shutil.which("gemini")

    def login_home(self) -> Path | None:
        return next((h for h in self.homes() if (h / ".gemini" / "oauth_creds.json").is_file()), None)

    def signed_in_email(self, home: Path) -> str | None:
        data = paths.read_json(home / ".gemini" / "google_accounts.json", {}) or {}
        active = data.get("active") if isinstance(data, dict) else None
        return active if isinstance(active, str) and "@" in active else None

    def snapshot(self, account: str) -> ProviderSnapshot:
        exe = self.installed()
        home = self.login_home()
        signed_in = self.signed_in_email(home) if home else None
        extra = {"installed": bool(exe), "logged_in": bool(home), "signed_in_email": signed_in}
        if not exe and not home:
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED, extra=extra,
                                    error="未安装 Gemini CLI（npm install -g @google/gemini-cli）")
        mine = self._email_of(account) if self._email_of else None
        if signed_in and mine and signed_in.lower() != mine.lower():
            return ProviderSnapshot(provider=self.id, account=account, status=NOT_CONFIGURED, extra=extra,
                                    error="Gemini CLI 当前登录的是其他 Google 账号")
        notes = []
        if not exe:
            notes.append("检测到登录记录但未找到 gemini 命令")
        if not home:
            notes.append("已安装但尚未登录（运行 gemini 并选择 Login with Google）")
        metric = Metric(
            key="daily_requests",
            label="每日请求上限（AI Pro）",
            unit="次",
            total=float(PRO_DAILY_REQUESTS),
            detail="官方公布额度；实时剩余需在 CLI 内 /stats 查看",
        )
        return ProviderSnapshot(provider=self.id, account=account, status=OK, metrics=[metric],
                                extra={**extra, "notes": notes})
