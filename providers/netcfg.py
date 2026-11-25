#!/usr/bin/env python3
"""User-configurable network proxy settings shared by the backend, CLI tools and shell scripts.

Replaces the old hard-coded "probe 127.0.0.1:7897/7890/10809" logic that was copy-pasted
into every script. Settings live in `~/.google-ai-manager/settings.json` under `"proxy"`:

    mode        "auto"   : inherited HTTPS_PROXY env wins; otherwise probe auto_hosts x auto_ports
                "manual" : always use `manual_url`
                "off"    : direct connection, clear any proxy env
    manual_url  e.g. "http://127.0.0.1:7890" (http / https / socks5 / socks5h)
    auto_hosts  hosts to probe; the special value "wsl-host" resolves to the Windows host IP
                as seen from a WSL2 NAT network (default gateway)
    auto_ports  ports to probe in order (editable defaults for common local proxy clients)
    no_proxy    always-direct hosts (localhost is always kept)

Usage from shell scripts (stdlib only, works with the system python3):

    eval "$(python3 /path/to/providers/netcfg.py shell-env)"
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any

try:  # imported as `providers.netcfg`
    from . import paths
except ImportError:  # executed directly as a script
    import paths  # type: ignore[no-redef]

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
# Original (pre-GAM) proxy env value, captured once and inherited by child processes so that
# "auto" mode can tell a user-provided proxy apart from the one we injected ourselves.
INHERITED_MARK = "GAM_PROXY_INHERITED"
MODES = ("auto", "manual", "off")
WSL_HOST_TOKEN = "wsl-host"
LOCAL_NO_PROXY = ("localhost", "127.0.0.1", "::1")

DEFAULT_PROXY_SETTINGS: dict[str, Any] = {
    "mode": "auto",
    "manual_url": "",
    "auto_hosts": ["127.0.0.1", WSL_HOST_TOKEN],
    # Default local ports of common proxy clients (Clash Verge, Clash, v2rayN). Editable in UI.
    "auto_ports": [7897, 7890, 10809],
    "auto_scheme": "http",
    "no_proxy": ",".join(LOCAL_NO_PROXY),
}

_URL_RE = re.compile(r"^(https?|socks5h?)://([^\s/@]+@)?(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]+):(\d{1,5})/?$")
_HOST_RE = re.compile(r"^(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]+)$")


# ----------------------------------------------------------------------------- settings I/O
def _validate(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if "mode" in raw:
        mode = str(raw["mode"] or "").strip().lower()
        if mode not in MODES:
            raise ValueError(f"代理模式必须是 {'/'.join(MODES)} 之一")
        out["mode"] = mode
    if "manual_url" in raw:
        url = str(raw["manual_url"] or "").strip()
        if url and not _URL_RE.match(url):
            raise ValueError("代理地址格式应为 scheme://host:port，例如 http://127.0.0.1:7890")
        if url and not (0 < int(_URL_RE.match(url).group(4)) < 65536):
            raise ValueError("代理端口必须在 1–65535 之间")
        out["manual_url"] = url
    if "auto_hosts" in raw:
        hosts_in = raw["auto_hosts"]
        if isinstance(hosts_in, str):
            hosts_in = [h for h in re.split(r"[,\s]+", hosts_in) if h]
        hosts: list[str] = []
        for h in list(hosts_in or [])[:8]:
            h = str(h).strip()
            if h != WSL_HOST_TOKEN and not _HOST_RE.match(h):
                raise ValueError(f"无效的探测主机: {h}")
            if h not in hosts:
                hosts.append(h)
        out["auto_hosts"] = hosts
    if "auto_ports" in raw:
        ports_in = raw["auto_ports"]
        if isinstance(ports_in, str):
            ports_in = [p for p in re.split(r"[,\s]+", ports_in) if p]
        ports: list[int] = []
        for p in list(ports_in or [])[:12]:
            try:
                port = int(p)
            except (TypeError, ValueError):
                raise ValueError(f"无效端口: {p}") from None
            if not 0 < port < 65536:
                raise ValueError(f"端口超出范围: {port}")
            if port not in ports:
                ports.append(port)
        out["auto_ports"] = ports
    if "auto_scheme" in raw:
        scheme = str(raw["auto_scheme"] or "http").strip().lower()
        if scheme not in ("http", "socks5", "socks5h"):
            raise ValueError("自动探测协议仅支持 http / socks5 / socks5h")
        out["auto_scheme"] = scheme
    if "no_proxy" in raw:
        items = [x.strip() for x in str(raw["no_proxy"] or "").split(",") if x.strip()]
        for must in LOCAL_NO_PROXY:
            if must not in items:
                items.append(must)
        out["no_proxy"] = ",".join(items)
    return out


def load_proxy_settings() -> dict[str, Any]:
    stored = paths.read_json(paths.settings_path(), {}) or {}
    raw = stored.get("proxy") if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        return dict(DEFAULT_PROXY_SETTINGS)
    try:
        return _validate(raw, DEFAULT_PROXY_SETTINGS)
    except ValueError:
        return dict(DEFAULT_PROXY_SETTINGS)


def save_proxy_settings(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("proxy 设置必须是对象")
    merged = _validate(patch, load_proxy_settings())
    paths.ensure_app_home()
    stored = paths.read_json(paths.settings_path(), {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    stored["proxy"] = merged
    paths.write_private_json(paths.settings_path(), stored)
    return merged


# ----------------------------------------------------------------------------- resolution
def _current_env_proxy() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(key)
        if val:
            return val
    return ""


def inherited_proxy() -> str:
    """Proxy the user's environment provided before we touched it ('' if none)."""
    if INHERITED_MARK in os.environ:
        return os.environ[INHERITED_MARK]
    return _current_env_proxy()


def wsl_host_ip() -> str | None:
    """Windows host IP from inside WSL2 (NAT mode default gateway), else None."""
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            next(fh, None)
            for line in fh:
                cols = line.split()
                if len(cols) > 2 and cols[1] == "00000000" and cols[2] != "00000000":
                    raw = bytes.fromhex(cols[2])
                    return ".".join(str(b) for b in reversed(raw))
    except (OSError, ValueError):
        pass
    return None


def _expand_hosts(hosts: list[str]) -> list[str]:
    out: list[str] = []
    for h in hosts:
        real = wsl_host_ip() if h == WSL_HOST_TOKEN else h
        if real and real not in out:
            out.append(real)
    return out


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=timeout):
            return True
    except OSError:
        return False


def redact(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url or "")


def resolve_proxy(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = settings or load_proxy_settings()
    mode = cfg.get("mode", "auto")
    if mode == "off":
        return {"mode": mode, "url": "", "source": "off", "detail": "已关闭代理（直连）"}
    if mode == "manual":
        url = cfg.get("manual_url") or ""
        if not url:
            return {"mode": mode, "url": "", "source": "off", "detail": "手动模式但未填写代理地址，当前直连"}
        return {"mode": mode, "url": url, "source": "manual", "detail": f"手动代理 {redact(url)}"}

    inherited = inherited_proxy()
    if inherited:
        return {"mode": mode, "url": inherited, "source": "env", "detail": f"沿用环境变量代理 {redact(inherited)}"}
    scheme = cfg.get("auto_scheme", "http")
    for host in _expand_hosts(list(cfg.get("auto_hosts") or [])):
        for port in cfg.get("auto_ports") or []:
            if _port_open(host, int(port)):
                h = f"[{host}]" if ":" in host and not host.startswith("[") else host
                url = f"{scheme}://{h}:{port}"
                return {"mode": mode, "url": url, "source": "auto", "detail": f"自动探测到本地代理 {url}"}
    return {"mode": mode, "url": "", "source": "direct", "detail": "未探测到本地代理端口，当前直连"}


def apply_proxy_env(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the proxy and export it into `os.environ` (inherited by child processes)."""
    if INHERITED_MARK not in os.environ:
        os.environ[INHERITED_MARK] = _current_env_proxy()
    cfg = settings or load_proxy_settings()
    resolved = resolve_proxy(cfg)
    no_proxy = cfg.get("no_proxy") or DEFAULT_PROXY_SETTINGS["no_proxy"]
    for key in NO_PROXY_VARS:
        os.environ[key] = no_proxy
    for key in PROXY_VARS:
        if resolved["url"]:
            os.environ[key] = resolved["url"]
        else:
            os.environ.pop(key, None)
    return resolved


def shell_env(settings: dict[str, Any] | None = None) -> str:
    """Bash statements that reproduce `apply_proxy_env()` in a shell."""
    import shlex

    resolved = apply_proxy_env(settings)
    lines = [f"export {INHERITED_MARK}={shlex.quote(os.environ.get(INHERITED_MARK, ''))}"]
    for key in NO_PROXY_VARS:
        lines.append(f"export {key}={shlex.quote(os.environ[key])}")
    for key in PROXY_VARS:
        if resolved["url"]:
            lines.append(f"export {key}={shlex.quote(resolved['url'])}")
        else:
            lines.append(f"unset {key}")
    return "\n".join(lines)


def test_proxy(url: str | None, target: str = "https://www.google.com/generate_204", timeout: float = 8.0) -> dict[str, Any]:
    """Probe `target` through `url` ('' = direct). Returns ok / status / latency_ms / error."""
    url = (url or "").strip()
    if url and not _URL_RE.match(url):
        return {"ok": False, "error": "代理地址格式无效"}
    if url.startswith("socks"):
        m = _URL_RE.match(url)
        ok = _port_open(m.group(3), int(m.group(4)), timeout=2.0)
        return {"ok": ok, "status": None, "latency_ms": None,
                "error": None if ok else "SOCKS 端口无法连接",
                "detail": "SOCKS 代理仅检测端口可达（完整连通性测试仅支持 http 代理）"}
    handler = urllib.request.ProxyHandler({"http": url, "https": url} if url else {})
    opener = urllib.request.build_opener(handler)
    t0 = time.monotonic()
    try:
        with opener.open(urllib.request.Request(target, method="GET"), timeout=timeout) as resp:
            status = resp.status
        return {"ok": status < 400, "status": status, "latency_ms": int((time.monotonic() - t0) * 1000), "error": None}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "latency_ms": int((time.monotonic() - t0) * 1000), "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return {"ok": False, "status": None, "latency_ms": None, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def public_view() -> dict[str, Any]:
    cfg = load_proxy_settings()
    resolved = resolve_proxy(cfg)
    return {
        "settings": cfg,
        "resolved": {**resolved, "url": redact(resolved["url"])},
        "modes": list(MODES),
        "wsl_host_ip": wsl_host_ip(),
        "settings_path": str(paths.settings_path()),
    }


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "shell-env":
        print(shell_env())
        return 0
    if cmd == "show":
        print(json.dumps(public_view(), indent=2, ensure_ascii=False))
        return 0
    if cmd == "test":
        url = argv[2] if len(argv) > 2 else resolve_proxy()["url"]
        print(json.dumps(test_proxy(url), indent=2, ensure_ascii=False))
        return 0
    sys.stderr.write("usage: netcfg.py [show|shell-env|test [url]]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
