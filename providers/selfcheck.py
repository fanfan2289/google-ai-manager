"""First-run environment self-check framework.

A check is `(id, label, fn)`; `fn()` returns `(status, detail)` or `(status, detail, fix)`.
Exceptions become `fail` results, so one broken check never hides the others.
"""

from __future__ import annotations

import time
from typing import Any, Callable

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
LEVELS = (OK, WARN, FAIL, INFO)

CheckFn = Callable[[], tuple]


def run_checks(checks: list[tuple[str, str, CheckFn]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for cid, label, fn in checks:
        t0 = time.monotonic()
        try:
            out = tuple(fn())
            status, detail = out[0], str(out[1] if len(out) > 1 else "")
            fix = str(out[2]) if len(out) > 2 and out[2] else ""
            if status not in LEVELS:
                status, detail = FAIL, f"invalid status {status!r}: {detail}"
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a failure
            status, detail, fix = FAIL, f"{type(exc).__name__}: {str(exc)[:300]}", ""
        results.append({"id": cid, "label": label, "status": status, "detail": detail, "fix": fix,
                        "ms": int((time.monotonic() - t0) * 1000)})
    counts = {lvl: sum(1 for r in results if r["status"] == lvl) for lvl in LEVELS}
    return {"ok": counts[FAIL] == 0, "counts": counts, "checks": results,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
