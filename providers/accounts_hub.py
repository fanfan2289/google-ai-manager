"""Accounts Hub: multi Google account management on top of colab-ext.

Reads go straight to colab-ext's registry (in-process, offline, never blocks on a locked
keyring). Every write — OAuth login, DriveFS consent, removal — goes through colab-ext's
public `colab` CLI in a subprocess, so this app never touches tokens itself and colab-ext's
process-global state (ContextVars, `colab_cli.common.state`) inside the monitor stays clean.

App-level metadata (label / note / tags / colour) lives in ~/.google-ai-manager/accounts.json
(0600). It never contains tokens; it is keyed by the colab-ext profile name.

Switching the account the monitor works with is the engine's job (its own config), NOT
`colab account use`: the CLI's global "active" account is shared with other tools.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import urllib.parse
from typing import Any, Callable

try:  # package import (backend / tests) or plain script
    from . import paths
except ImportError:  # pragma: no cover
    import paths  # type: ignore

# New profile names: no '@' so e-mail addresses never end up as names in logs or URLs.
NEW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
# Google auth codes are URL-safe tokens such as "4/0Ab...".
CODE_RE = re.compile(r"^[A-Za-z0-9/_.~%+-]{10,1024}$")
OAUTH_URL_PREFIXES = ("https://accounts.google.com/",)
COLORS = ("", "blue", "emerald", "amber", "crimson", "violet", "slate")
MAX_LABEL, MAX_NOTE, MAX_TAGS, MAX_TAG = 40, 500, 8, 16

Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


class AccountsError(ValueError):
    """User-facing failure (bad input, CLI refused, ...)."""


def _load_backend():
    from colab_ext import accounts  # type: ignore

    return accounts


def meta_path():
    return paths.app_home() / "accounts.json"


def normalize_code(raw: str) -> str:
    """Accept a bare auth code or a pasted redirect URL containing ?code=..."""
    s = (raw or "").strip()
    if s.startswith(("http://", "https://")):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(s).query)
        s = (qs.get("code") or [""])[0].strip()
    if not CODE_RE.match(s):
        raise AccountsError("授权码格式不正确：请粘贴 Google 页面显示的完整授权码")
    return s


def _clean_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,，\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        raise AccountsError("tags 必须是列表或逗号分隔的字符串")
    out: list[str] = []
    for t in raw:
        t = str(t).strip()
        if not t:
            continue
        if len(t) > MAX_TAG:
            raise AccountsError(f"标签过长（最多 {MAX_TAG} 字）：{t[:MAX_TAG]}…")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise AccountsError(f"标签最多 {MAX_TAGS} 个")
    return out


def validate_meta_patch(patch: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "label" in patch:
        label = str(patch.get("label") or "").strip()
        if len(label) > MAX_LABEL:
            raise AccountsError(f"显示名称最多 {MAX_LABEL} 字")
        clean["label"] = label
    if "note" in patch:
        note = str(patch.get("note") or "").strip()
        if len(note) > MAX_NOTE:
            raise AccountsError(f"备注最多 {MAX_NOTE} 字")
        clean["note"] = note
    if "tags" in patch:
        clean["tags"] = _clean_tags(patch.get("tags") or [])
    if "color" in patch:
        color = str(patch.get("color") or "").strip()
        if color not in COLORS:
            raise AccountsError("不支持的颜色")
        clean["color"] = color
    return clean


def _first_url(text: str) -> str | None:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(OAUTH_URL_PREFIXES):
            return line
    return None


def _tail(text: str, n: int = 4) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return " | ".join(lines[-n:])[:600]


def aggregate_account_totals(per: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Sum Colab CCU / burn / VMs over accounts; storage is de-duplicated by Drive account."""

    def metric(snap: dict[str, Any], key: str) -> dict[str, Any] | None:
        if (snap or {}).get("status") != "ok":
            return None
        return next((m for m in snap.get("metrics", []) if m.get("key") == key), None)

    ccu = rate = 0.0
    vms = ccu_ok = storage_used = storage_total = 0
    seen_drive: set[str] = set()
    for name, snaps in per.items():
        colab = snaps.get("colab") or {}
        bal = metric(colab, "ccu_balance")
        if bal and isinstance(bal.get("remaining"), (int, float)):
            ccu += float(bal["remaining"])
            ccu_ok += 1
        burn = metric(colab, "burn_rate")
        if burn and isinstance(burn.get("used"), (int, float)):
            rate += float(burn["used"])
        live = metric(colab, "active_vms")
        if live and isinstance(live.get("used"), (int, float)):
            vms += int(live["used"])
        stor = snaps.get("google_storage") or {}
        tot = metric(stor, "storage_total")
        drive_key = (stor.get("extra") or {}).get("drive_account") or name
        if tot and drive_key not in seen_drive:
            seen_drive.add(drive_key)
            storage_used += int(tot.get("used") or 0)
            storage_total += int(tot.get("total") or 0)
    return {
        "accounts": len(per),
        "ccu_balance": round(ccu, 2),
        "ccu_accounts": ccu_ok,
        "burn_rate": round(rate, 3),
        "active_vms": vms,
        "storage_used": storage_used,
        "storage_total": storage_total,
        "storage_accounts": len(seen_drive),
    }


class AccountsHub:
    def __init__(
        self,
        *,
        colab_bin: str = "colab",
        env: dict[str, str] | Callable[[], dict[str, str]] | None = None,
        backend: Any = None,
        runner: Runner | None = None,
    ) -> None:
        self._colab_bin = colab_bin
        self._env = env
        self._backend = backend
        self._runner = runner or self._default_runner
        self._meta_lock = threading.Lock()
        # Serialise CLI writes: colab-ext locks its registry, but pending OAuth files are per account.
        self._cli_lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    @property
    def backend(self):
        if self._backend is None:
            self._backend = _load_backend()
        return self._backend

    def _default_runner(self, args: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
        # A callable is re-evaluated per call so proxy changes made in the UI apply immediately.
        env = dict((self._env() if callable(self._env) else self._env) or os.environ)
        env.setdefault("NO_COLOR", "1")
        env["COLUMNS"] = "200"
        return subprocess.run(
            [self._colab_bin, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    def _cli(self, args: list[str], *, timeout: float = 90.0) -> "subprocess.CompletedProcess[str]":
        with self._cli_lock:
            try:
                proc = self._runner(args, timeout)
            except subprocess.TimeoutExpired as exc:
                raise AccountsError(f"colab 命令超时（{int(timeout)}s）") from exc
            except FileNotFoundError as exc:
                raise AccountsError("找不到 colab 命令行（请先运行 wsl_setup_env.sh 安装 google-colab-cli + colab-ext）") from exc
        if proc.returncode != 0:
            raise AccountsError(_tail(proc.stderr) or _tail(proc.stdout) or f"colab 退出码 {proc.returncode}")
        return proc

    # ------------------------------------------------------------------ app metadata
    def load_meta(self) -> dict[str, dict[str, Any]]:
        data = paths.read_json(meta_path(), {})
        accts = data.get("accounts") if isinstance(data, dict) else None
        return accts if isinstance(accts, dict) else {}

    def _save_meta(self, accts: dict[str, dict[str, Any]]) -> None:
        paths.write_private_json(meta_path(), {"version": 1, "accounts": accts})

    def set_meta(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        self.require_existing(name)
        clean = validate_meta_patch(patch or {})
        with self._meta_lock:
            accts = self.load_meta()
            entry = {**accts.get(name, {}), **clean}
            accts[name] = entry
            self._save_meta(accts)
        return entry

    def _drop_meta(self, name: str) -> None:
        with self._meta_lock:
            accts = self.load_meta()
            if accts.pop(name, None) is not None:
                self._save_meta(accts)

    # ------------------------------------------------------------------ reads
    def registry(self) -> dict[str, Any]:
        return self.backend.load_registry()

    def names(self) -> list[str]:
        return list((self.registry().get("accounts") or {}).keys())

    def email_of(self, name: str) -> str | None:
        """Google e-mail recorded by colab-ext for a profile (links it to Antigravity / Gemini CLI)."""
        try:
            meta = (self.registry().get("accounts") or {}).get(name) or {}
        except Exception:
            return None
        email = meta.get("email") or meta.get("dfs_email") if isinstance(meta, dict) else None
        return email if isinstance(email, str) and "@" in email else None

    def require_existing(self, name: str) -> str:
        name = str(name or "").strip()
        if not name or name not in self.names():
            raise AccountsError("账号不存在")
        return name

    def _drive_info(self, name: str) -> dict[str, Any]:
        b = self.backend
        try:
            drv, src = b.resolve_drive_account(name, for_consent=False)
        except Exception:
            drv, src = name, "self"
        return {
            "account": drv,
            "source": src,
            "shared": drv != name,
            "ready": os.path.exists(b.dfs_store_path(drv)),
            "pending": os.path.exists(b.dfs_pending_path(drv)),
        }

    def list_accounts(self) -> dict[str, Any]:
        b = self.backend
        reg = self.registry()
        meta = self.load_meta()
        rows: list[dict[str, Any]] = []
        for name, m in (reg.get("accounts") or {}).items():
            m = m if isinstance(m, dict) else {}
            try:
                auth = b.inspect_colab_auth_nonblocking(name)
            except Exception as exc:  # pragma: no cover - defensive
                auth = {"ready": None, "storage": "unknown", "detail": str(exc)}
            rows.append(
                {
                    "name": name,
                    "email": m.get("email") or m.get("dfs_email"),
                    "cli_active": name == reg.get("active"),
                    "auth": {
                        "ready": auth.get("ready"),
                        "provider": auth.get("provider"),
                        "storage": auth.get("storage"),
                        "pending": os.path.exists(b.oauth_pending_path(name)),
                    },
                    "drive": self._drive_info(name),
                    "sessions": sorted((b.read_local_sessions(name) or {}).keys()),
                    "created_at": m.get("created_at"),
                    "meta": {"label": "", "note": "", "tags": [], "color": "", **meta.get(name, {})},
                }
            )
        return {"cli_active": reg.get("active"), "accounts": rows}

    # ------------------------------------------------------------------ OAuth (two-step, copy & paste)
    def start_add(self, name: str, email: str | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        if not NEW_NAME_RE.match(name):
            raise AccountsError("账号名只能用字母、数字、. _ -（最多 32 位，不能含 @）")
        if name in self.names():
            raise AccountsError("同名账号已存在，请改用「重新登录」")
        args = ["account", "add", name, "--url-only", "--no-use", "--no-browser"]
        email = (email or "").strip()
        if email:
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 254:
                raise AccountsError("邮箱格式不正确")
            args += ["--email", email]
        return self._url_from(self._cli(args), name, "login")

    def start_login(self, name: str) -> dict[str, Any]:
        name = self.require_existing(name)
        return self._url_from(self._cli(["account", "login", name, "--url-only"]), name, "login")

    def finish_login(self, name: str, code: str) -> dict[str, Any]:
        name = self.require_existing(name)
        self._cli(["account", "login", name, "--code", normalize_code(code)])
        return {"ok": True, "name": name}

    def start_drive(self, name: str) -> dict[str, Any]:
        name = self.require_existing(name)
        return self._url_from(
            self._cli(["drive", "consent", "-a", name, "--url-only", "--no-browser"]), name, "drive"
        )

    def finish_drive(self, name: str, code: str) -> dict[str, Any]:
        name = self.require_existing(name)
        self._cli(["drive", "consent", "-a", name, "--code", normalize_code(code)])
        return {"ok": True, "name": name}

    @staticmethod
    def _url_from(proc: "subprocess.CompletedProcess[str]", name: str, step: str) -> dict[str, Any]:
        url = _first_url(proc.stdout) or _first_url(proc.stderr)
        if not url:
            raise AccountsError("colab 未返回授权链接：" + (_tail(proc.stderr) or _tail(proc.stdout) or "空输出"))
        return {"ok": True, "name": name, "step": step, "url": url}

    # ------------------------------------------------------------------ removal
    def remove(self, name: str) -> dict[str, Any]:
        """Remove a colab-ext profile (refused by colab-ext while it records local sessions)."""
        name = self.require_existing(name)
        if len(self.names()) <= 1:
            raise AccountsError("至少需要保留一个账号")
        self._cli(["account", "remove", name])
        self._drop_meta(name)
        return {"ok": True, "name": name}


# ---------------------------------------------------------------------- failover (M3)
def snapshot_usable(snap: dict[str, Any] | None, threshold: float) -> bool:
    """A Colab account snapshot can be probed when its live CCU balance is >= threshold."""
    if not snap or snap.get("usage_error"):
        return False
    bal = snap.get("balance")
    return isinstance(bal, (int, float)) and float(bal) >= float(threshold)


def choose_probe_account(
    primary: str,
    backups: list[str],
    *,
    failover_on: bool,
    threshold: float,
    blocked_until: dict[str, float],
    now: float,
    snapshot_of: Callable[[str], dict[str, Any] | None],
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Pick the account for the next probe / grab.

    Returns (account, snapshot, note). `account` is None only when failover is on and every
    candidate (primary included) is temporarily blocked (429 / 412 / auth) — skip the cycle.
    `snapshot_of` fetches a live snapshot (may raise / return None); it is called lazily so
    backups are only queried when the primary is not usable.
    """

    def fetch(name: str) -> dict[str, Any] | None:
        try:
            return snapshot_of(name)
        except Exception:  # noqa: BLE001 - an unreachable backup is simply skipped
            return None

    primary_blocked = blocked_until.get(primary, 0.0) > now
    if not failover_on:
        return primary, fetch(primary), ""

    primary_snap = None if primary_blocked else fetch(primary)
    if primary_snap is not None and snapshot_usable(primary_snap, threshold):
        return primary, primary_snap, ""

    why = "主号暂时受限" if primary_blocked else (
        "主号 CCU 低于阈值" if primary_snap is not None and isinstance(primary_snap.get("balance"), (int, float))
        else "主号额度查询失败"
    )
    seen = {primary}
    for name in backups:
        if not name or name in seen:
            continue
        seen.add(name)
        if blocked_until.get(name, 0.0) > now:
            continue
        snap = fetch(name)
        if snapshot_usable(snap, threshold):
            return name, snap, f"{why}，已切换到备用号 {name}"

    if not primary_blocked:
        return primary, primary_snap, f"{why}，且没有可用的备用号，继续使用主号"
    return None, None, f"{why}，备用号均不可用，本轮跳过"
