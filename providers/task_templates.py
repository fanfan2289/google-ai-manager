"""Remote task templates (`tasks/templates/*.json`) -> validated `wsl_run_h3.sh` arguments.

Safety: the runner owns every flag that controls billing (session, GPU, account, fallback,
`--max-minutes` watchdog, reuse) — templates and user params can never set them, and
`--keep-alive` (no auto-shutdown) is rejected outright.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "tasks" / "templates"

ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+ -]{0,299}$")
PARAM_TYPES = ("path", "text", "number", "shell")
RESERVED_FLAGS = {
    "--keep-alive", "--dry-run", "--max-minutes", "--reuse-session", "-s", "--session",
    "-g", "--gpu", "--fallback-gpu", "--gpu-retry-minutes", "-a", "--account", "-h", "--help",
}
MAX_SHELL = 4000


class TemplateError(ValueError):
    pass


def _check_flag(flag: Any, where: str) -> str:
    flag = str(flag or "")
    if not FLAG_RE.match(flag):
        raise TemplateError(f"{where}: 非法参数名 {flag!r}")
    if flag in RESERVED_FLAGS:
        raise TemplateError(f"{where}: 参数 {flag} 由运行器控制，模板不能设置")
    return flag


def validate_template(raw: dict[str, Any], *, file_stem: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TemplateError("模板必须是 JSON 对象")
    tid = str(raw.get("id") or "")
    if not ID_RE.match(tid) or (file_stem is not None and tid != file_stem):
        raise TemplateError(f"模板 id 非法或与文件名不一致：{tid!r}")
    args = raw.get("args") or []
    if not isinstance(args, list):
        raise TemplateError(f"{tid}: args 必须是列表")
    for a in args:
        if str(a).startswith("-"):
            _check_flag(a, tid)
    params = []
    keys: set[str] = set()
    for p in raw.get("params") or []:
        key = str(p.get("key") or "")
        if not re.match(r"^[a-z][a-z0-9_]{0,30}$", key) or key in keys:
            raise TemplateError(f"{tid}: 参数 key 非法或重复：{key!r}")
        keys.add(key)
        ptype = p.get("type") or "text"
        if ptype not in PARAM_TYPES:
            raise TemplateError(f"{tid}.{key}: 不支持的类型 {ptype!r}")
        params.append({
            "key": key,
            "label": str(p.get("label") or key),
            "flag": _check_flag(p.get("flag"), f"{tid}.{key}"),
            "type": ptype,
            "default": p.get("default", ""),
            "required": bool(p.get("required")),
            "min": p.get("min"),
            "max": p.get("max"),
        })
    return {
        "id": tid,
        "name": str(raw.get("name") or tid),
        "description": str(raw.get("description") or ""),
        "order": int(raw.get("order") or 100),
        "ui_extras": [str(x) for x in raw.get("ui_extras") or []],
        "args": [str(a) for a in args],
        "params": params,
    }


def load_templates(directory: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """(valid templates sorted by order, error messages for invalid files)."""
    d = directory or TEMPLATES_DIR
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append(validate_template(json.loads(f.read_text(encoding="utf-8")), file_stem=f.stem))
        except (OSError, ValueError) as exc:
            errors.append(f"{f.name}: {exc}")
    out.sort(key=lambda t: (t["order"], t["id"]))
    return out, errors


def build_args(template: dict[str, Any], values: dict[str, Any] | None) -> list[str]:
    """Validated extra `wsl_run_h3.sh` arguments for one run of `template`."""
    values = values or {}
    args = list(template["args"])
    for p in template["params"]:
        v = values.get(p["key"], p["default"])
        v = "" if v is None else v
        if p["type"] == "number":
            if v == "":
                if p["required"]:
                    raise TemplateError(f"请填写「{p['label']}」")
                continue
            try:
                num = float(v)
            except (TypeError, ValueError) as exc:
                raise TemplateError(f"「{p['label']}」必须是数字") from exc
            if (p["min"] is not None and num < float(p["min"])) or (p["max"] is not None and num > float(p["max"])):
                raise TemplateError(f"「{p['label']}」超出范围")
            sval = str(int(num)) if num.is_integer() else str(num)
        else:
            sval = str(v).strip()
            if not sval:
                if p["required"]:
                    raise TemplateError(f"请填写「{p['label']}」")
                continue
            if p["type"] == "path" and not PATH_RE.match(sval):
                raise TemplateError(f"「{p['label']}」包含不允许的字符")
            if p["type"] == "text" and (len(sval) > 300 or any(c in sval for c in "\r\n\0")):
                raise TemplateError(f"「{p['label']}」过长或包含换行")
            if p["type"] == "shell" and (len(sval) > MAX_SHELL or "\0" in sval):
                raise TemplateError(f"「{p['label']}」过长（最多 {MAX_SHELL} 字符）")
            if sval.startswith("-") and p["type"] != "shell":
                raise TemplateError(f"「{p['label']}」不能以 - 开头")
        args += [p["flag"], sval]
    return args
