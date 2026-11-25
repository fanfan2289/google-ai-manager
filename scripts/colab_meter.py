#!/usr/bin/env python3
"""Colab CCU Meter & Anti-Ghost-Billing Guard (Windows WSL2 adaptation).

Replaces and upgrades the macOS `colab_meter.py` utility:
  - `start`  : Record pre-run CCU balance & timestamp baseline, verify account status.
  - `status` : Show live CCU balance, hourly burn rate, and ALL server-side sessions
               (including orphaned '?' VMs missed by local `sessions.json`).
  - `stop`   : Force-terminate target or ALL server-side Colab VMs (including orphans),
               verify 0 remaining assignments, and print CCU delta reconciliation.
  - `watch`  : Dead-man's-switch background watchdog: if the parent runner process
               dies unexpectedly (e.g. SIGKILL / terminal crash) or exceeds max TTL,
               automatically force-kills all Colab VMs to prevent ghost billing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any


def _ensure_wsl_proxy_env() -> None:
    """Ensure NO_PROXY covers localhost and auto-detect local Windows Clash proxy if unset."""
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")
    if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
        for port in (7897, 7890, 10809):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    proxy_url = f"http://127.0.0.1:{port}"
                    os.environ["HTTP_PROXY"] = proxy_url
                    os.environ["HTTPS_PROXY"] = proxy_url
                    os.environ["http_proxy"] = proxy_url
                    os.environ["https_proxy"] = proxy_url
                    break
            except OSError:
                continue


_ensure_wsl_proxy_env()


def _find_colab_python() -> str | None:
    """Locate the Python interpreter inside uv's `google-colab-cli` tool venv."""
    candidates = [
        Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python",
        Path.home() / ".local/share/uv/tools/google-colab-cli/bin/python3",
    ]
    colab_bin = Path.home() / ".local/bin/colab"
    if colab_bin.is_file():
        try:
            first_line = colab_bin.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            if first_line.startswith("#!"):
                shebang_py = first_line[2:].strip().split()[-1]
                candidates.insert(0, Path(shebang_py))
        except OSError:
            pass
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _reexec_in_colab_venv_if_needed() -> None:
    """Re-exec into google-colab-cli's venv Python if `colab_cli` isn't importable here."""
    try:
        import colab_cli  # noqa: F401
        return
    except ImportError:
        pass

    target_py = _find_colab_python()
    if not target_py or os.path.realpath(target_py) == os.path.realpath(sys.executable):
        sys.stderr.write(
            "[colab_meter] ERROR: Cannot import `colab_cli` and could not find "
            "`google-colab-cli` uv tool venv.\n"
            "Please run setup first: `uv tool install google-colab-cli`\n"
        )
        sys.exit(2)

    os.execv(target_py, [target_py, *sys.argv])


_reexec_in_colab_venv_if_needed()

# Now running inside google-colab-cli's Python environment (with colab_ext.pth loaded if installed).
import colab_cli.cli  # noqa: E402, F401  (triggers colab_ext hook registration if present)
from colab_cli.auth import AuthProvider  # noqa: E402
from colab_cli.common import State  # noqa: E402
from colab_cli.runtime import ColabRuntime  # noqa: E402

STATE_DIR = Path(os.environ.get("COLAB_H3_STATE_DIR", Path.home() / ".cache/colab-h3"))
BASELINE_FILE = STATE_DIR / "ccu_baseline.json"
WATCHDOG_DISARM_FILE = STATE_DIR / "watchdog.disarm"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _iter_states(account: str | None = None) -> list[tuple[str, State]]:
    """Yield `(account_label, State)` across configured colab-ext accounts or default."""
    try:
        from colab_ext import accounts  # type: ignore
        from colab_cli.history import HistoryLogger

        reg = accounts.ensure_bootstrapped()
        accts = reg.get("accounts", {}) or {}
        if account:
            acct_name, _ = accounts.resolve_account(account, allow_create=False)
            target_names = [acct_name]
        else:
            active = reg.get("active") or accounts.DEFAULT_ACCOUNT
            # Put active account first, then any other registered accounts
            target_names = [active] + [k for k in accts if k != active]

        out: list[tuple[str, State]] = []
        seen = set()
        for name in target_names:
            if name in seen:
                continue
            seen.add(name)
            meta = accts.get(name, {}) if isinstance(accts.get(name), dict) else {}
            st = State()
            st.config_path = accounts.sessions_path(name)
            st._history = HistoryLogger(log_dir=accounts.history_dir(name))
            if (meta.get("auth_provider") or "").lower() == "adc":
                st.auth_provider = AuthProvider.ADC
            acct_oauth = accounts.oauth_config_path(name)
            if os.path.exists(acct_oauth):
                st.client_oauth_config = acct_oauth
            out.append((name, st))
        if out:
            return out
    except ImportError:
        pass
    except Exception as exc:
        sys.stderr.write(f"[colab_meter] Note: colab_ext account lookup fallback ({exc})\n")

    return [("default", State())]


def _activate_ext_context(acct_name: str):
    """Return a context manager activating `acct_name` in colab_ext if available."""
    try:
        from colab_ext import accounts  # type: ignore

        return accounts.account_context(acct_name, source="colab_meter", non_interactive=True)
    except ImportError:
        import contextlib

        return contextlib.nullcontext()


def _query_account(acct_name: str, st: State) -> dict[str, Any]:
    """Fetch live server-side assignments and CCU usage for one account."""
    with _activate_ext_context(acct_name):
        local_sessions = st.store.list()
        assignments = list(st.client.list_assignments() or [])
        usage_info = None
        usage_err = None
        try:
            usage_info = st.client.get_consumption_user_info()
        except Exception as exc:
            usage_err = str(exc)

        name_by_endpoint = {s.endpoint: s.name for s in local_sessions.values()}
        live_items = []
        for a in assignments:
            ep = a.endpoint
            accel = getattr(a.accelerator, "value", str(a.accelerator))
            shape = getattr(a.machine_shape, "name", str(a.machine_shape))
            variant = getattr(a.variant, "name", str(a.variant))
            live_items.append(
                {
                    "name": name_by_endpoint.get(ep, "?"),
                    "endpoint": ep,
                    "hardware": "CPU" if accel == "NONE" else accel,
                    "shape": shape,
                    "variant": variant,
                    "proxy_url": getattr(getattr(a, "runtime_proxy_info", None), "url", ""),
                    "proxy_token": getattr(getattr(a, "runtime_proxy_info", None), "token", ""),
                }
            )

        return {
            "account": acct_name,
            "local_sessions": local_sessions,
            "assignments": live_items,
            "balance": getattr(usage_info, "paid_compute_units_balance", None),
            "rate_hourly": getattr(usage_info, "consumption_rate_hourly", None),
            "reported_assignments_count": getattr(usage_info, "assignments_count", None),
            "usage_error": usage_err,
        }


def _print_snapshot(snap: dict[str, Any], header: str = "CCU & Session Status") -> None:
    acct = snap["account"]
    bal = snap["balance"]
    rate = snap["rate_hourly"]
    bal_str = f"{bal:.2f} CCU" if isinstance(bal, (int, float)) else "N/A"
    rate_str = f"{rate:.2f} CCU/hr" if isinstance(rate, (int, float)) else "N/A"
    live = snap["assignments"]

    print(f"=== [{acct}] {header} ({_now_iso()}) ===")
    print(f"  CCU Balance          : {bal_str}")
    print(f"  Burn Rate (reported) : {rate_str}")
    print(f"  Live Server Sessions : {len(live)}")
    for item in live:
        orphan_note = " [ORPHAN/UNTRACKED!]" if item["name"] == "?" else ""
        print(
            f"    - [{item['name']}] {item['endpoint']} | "
            f"Hardware: {item['hardware']} | Shape: {item['shape']}{orphan_note}"
        )
    if snap["usage_error"]:
        print(f"  Usage API Warning    : {snap['usage_error']}")


def cmd_status(args: argparse.Namespace) -> int:
    states = _iter_states(args.account)
    if not args.all_accounts:
        states = states[:1]

    any_active = False
    for acct_name, st in states:
        try:
            snap = _query_account(acct_name, st)
            _print_snapshot(snap)
            if snap["assignments"]:
                any_active = True
        except Exception as exc:
            sys.stderr.write(f"[colab_meter] ERROR querying account '{acct_name}': {exc}\n")
            return 1

    if args.assert_zero and any_active:
        sys.stderr.write("[colab_meter] ASSERTION FAILED: Active Colab sessions still exist!\n")
        return 3
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WATCHDOG_DISARM_FILE.unlink(missing_ok=True)

    acct_name, st = _iter_states(args.account)[0]
    snap = _query_account(acct_name, st)
    _print_snapshot(snap, header="Pre-Run Baseline")

    baseline = {
        "recorded_at_epoch": time.time(),
        "recorded_at_iso": _now_iso(),
        "account": acct_name,
        "session": args.session,
        "balance_start": snap["balance"],
        "rate_start": snap["rate_hourly"],
        "preexisting_endpoints": [x["endpoint"] for x in snap["assignments"]],
    }
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"[colab_meter] Baseline saved -> {BASELINE_FILE}")
    return 0


def _terminate_account_sessions(
    acct_name: str,
    st: State,
    target_session: str | None,
    kill_orphans: bool,
) -> tuple[int, list[dict[str, Any]]]:
    """Unassign target (or all) server-side sessions and verify removal."""
    killed_count = 0
    with _activate_ext_context(acct_name):
        local_sessions = st.store.list()
        assignments = list(st.client.list_assignments() or [])
        name_by_endpoint = {s.endpoint: s.name for s in local_sessions.values()}

        targets = []
        for a in assignments:
            ep = a.endpoint
            s_name = name_by_endpoint.get(ep, "?")
            if target_session is None:
                targets.append((s_name, ep, a))
            elif s_name == target_session:
                targets.append((s_name, ep, a))
            elif kill_orphans and s_name == "?":
                targets.append((s_name, ep, a))

        # Also clean stale local store entry if already gone on server
        if target_session and target_session in local_sessions:
            ep_set = {a.endpoint for a in assignments}
            if local_sessions[target_session].endpoint not in ep_set:
                st.store.remove(target_session)

        for s_name, ep, a in targets:
            accel = getattr(a.accelerator, "value", "UNKNOWN")
            print(f"[colab_meter] [{acct_name}] Force-stopping '{s_name}' ({ep}, GPU={accel})...")

            # 1) Best-effort kernel shutdown
            local_s = local_sessions.get(s_name) if s_name != "?" else None
            url = local_s.url if local_s else getattr(getattr(a, "runtime_proxy_info", None), "url", "")
            tok = local_s.token if local_s else getattr(getattr(a, "runtime_proxy_info", None), "token", "")
            kid = local_s.kernel_id if local_s else None
            if url and tok:
                try:
                    ColabRuntime(url, tok, kernel_id=kid).stop(shutdown_kernel=True)
                except Exception:
                    pass

            # 2) Backend VM unassign (with retries)
            last_err = None
            for attempt in range(1, 4):
                try:
                    st.client.unassign(ep)
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    sys.stderr.write(
                        f"[colab_meter] unassign attempt {attempt}/3 for {ep} failed: {exc}\n"
                    )
                    time.sleep(1.5 * attempt)

            if last_err is None:
                killed_count += 1
                if s_name != "?":
                    st.store.remove(s_name)

        # Prune any remaining local sessions that no longer exist on server
        for attempt in range(5):
            remaining_snap = _query_account(acct_name, st)
            remaining = remaining_snap["assignments"]
            if target_session is None:
                still_there = remaining
            else:
                still_there = [
                    x for x in remaining
                    if x["name"] == target_session or (kill_orphans and x["name"] == "?")
                ]
            if not still_there:
                break
            time.sleep(2.0)
            for item in still_there:
                try:
                    st.client.unassign(item["endpoint"])
                except Exception:
                    pass

        final_snap = _query_account(acct_name, st)
        live_eps = {x["endpoint"] for x in final_snap["assignments"]}
        for s_name, s_obj in list(st.store.list().items()):
            if s_obj.endpoint not in live_eps:
                st.store.remove(s_name)

        return killed_count, final_snap["assignments"]


def cmd_stop(args: argparse.Namespace) -> int:
    # Signal any running watchdog that explicit cleanup is happening
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WATCHDOG_DISARM_FILE.write_text(_now_iso(), encoding="utf-8")

    states = _iter_states(args.account)
    if not args.all_accounts:
        states = states[:1]

    target_session = None if args.all else args.session
    exit_rc = 0

    for acct_name, st in states:
        try:
            killed, remaining = _terminate_account_sessions(
                acct_name=acct_name,
                st=st,
                target_session=target_session,
                kill_orphans=args.kill_orphans,
            )
            print(f"[colab_meter] [{acct_name}] Terminated {killed} session(s).")

            snap = _query_account(acct_name, st)
            _print_snapshot(snap, header="Post-Stop Verification")

            if remaining:
                # Check if remaining sessions were supposed to be stopped
                unexpected = (
                    remaining
                    if target_session is None
                    else [x for x in remaining if x["name"] == target_session]
                )
                if unexpected:
                    sys.stderr.write(
                        f"\n[colab_meter] CRITICAL GHOST-BILLING ALERT: "
                        f"{len(unexpected)} session(s) STILL ACTIVE on '{acct_name}'!\n"
                        f"  Check https://colab.research.google.com immediately!\n"
                    )
                    exit_rc = 4
                else:
                    sys.stderr.write(
                        f"\n[colab_meter] WARNING: {len(remaining)} OTHER session(s) are still "
                        f"running on '{acct_name}'. Run `colab_meter.py stop --all` to kill everything.\n"
                    )
            else:
                print(
                    f"[colab_meter] VERIFIED SAFE: 0 active Colab VMs on '{acct_name}' "
                    "(零残留实例，无幽灵计费风险)."
                )
                if isinstance(snap["rate_hourly"], (int, float)) and snap["rate_hourly"] > 0:
                    print(
                        "  (Note: Colab's reported hourly rate updates asynchronously with a ~30-60s lag; "
                        "since Live Server Sessions = 0, billing has already stopped.)"
                    )

            # Reconcile against baseline if available
            if BASELINE_FILE.is_file():
                try:
                    base = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
                    if base.get("account") == acct_name:
                        elapsed_s = max(0.0, time.time() - float(base.get("recorded_at_epoch", time.time())))
                        elapsed_min = elapsed_s / 60.0
                        b_start = base.get("balance_start")
                        b_end = snap["balance"]
                        print(f"=== [{acct_name}] CCU Reconciliation (本次运行对账) ===")
                        print(f"  Started At     : {base.get('recorded_at_iso')}")
                        print(f"  Elapsed Time   : {elapsed_min:.1f} min ({elapsed_s:.0f}s)")
                        if isinstance(b_start, (int, float)) and isinstance(b_end, (int, float)):
                            delta = max(0.0, b_start - b_end)
                            print(f"  Start Balance  : {b_start:.2f} CCU")
                            print(f"  End Balance    : {b_end:.2f} CCU")
                            print(f"  Net Consumed   : {delta:.2f} CCU (Note: Colab settles CCU balance with slight delay)")
                        BASELINE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            sys.stderr.write(f"[colab_meter] ERROR stopping sessions on '{acct_name}': {exc}\n")
            exit_rc = 1

    return exit_rc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cmd_watch(args: argparse.Namespace) -> int:
    """Background dead-man's-switch watchdog."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "watchdog.log"
    deadline = time.time() + (args.max_minutes * 60.0)

    def _log(msg: str) -> None:
        line = f"[{_now_iso()}] [watchdog pid={os.getpid()}] {msg}\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    _log(
        f"Armed: watching parent_pid={args.parent_pid}, session={args.session or 'ALL'}, "
        f"max_minutes={args.max_minutes}"
    )

    trigger_reason = None
    while True:
        time.sleep(args.poll_seconds)
        if WATCHDOG_DISARM_FILE.is_file():
            _log("Disarm signal detected. Exiting cleanly.")
            return 0
        if args.parent_pid and not _pid_alive(args.parent_pid):
            # Give parent's EXIT trap a brief 8s grace window to run stop itself
            time.sleep(8.0)
            if WATCHDOG_DISARM_FILE.is_file():
                _log("Parent exited and disarmed watchdog within grace window. Exiting.")
                return 0
            trigger_reason = f"parent PID {args.parent_pid} died without disarming"
            break
        if time.time() >= deadline:
            trigger_reason = f"exceeded max_minutes={args.max_minutes} TTL"
            break

    _log(f"TRIGGERED ({trigger_reason})! Executing emergency stop...")
    stop_ns = argparse.Namespace(
        account=args.account,
        session=args.session,
        all=(args.session is None),
        all_accounts=False,
        kill_orphans=True,
    )
    rc = cmd_stop(stop_ns)
    _log(f"Emergency stop finished with exit_code={rc}.")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Colab CCU Meter & Anti-Ghost-Billing Controller"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", aliases=["check"], help="Check live sessions & CCU balance")
    p_status.add_argument("-a", "--account", help="Colab account profile")
    p_status.add_argument("--all-accounts", action="store_true", help="Check all registered accounts")
    p_status.add_argument("--assert-zero", action="store_true", help="Exit non-zero if any VM is active")
    p_status.set_defaults(func=cmd_status)

    p_start = sub.add_parser("start", help="Record pre-run CCU baseline before launching a VM")
    p_start.add_argument("-a", "--account", help="Colab account profile")
    p_start.add_argument("-s", "--session", default="h3work", help="Session name")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Force-kill Colab VM(s), verify 0 active, reconcile CCU")
    p_stop.add_argument("-a", "--account", help="Colab account profile")
    p_stop.add_argument("-s", "--session", default=None, help="Specific session name (default: stop ALL)")
    p_stop.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Stop ALL active sessions on the account (default when -s is omitted)",
    )
    p_stop.add_argument("--all-accounts", action="store_true", help="Stop across all registered accounts")
    p_stop.add_argument(
        "--no-kill-orphans",
        dest="kill_orphans",
        action="store_false",
        default=True,
        help="Do not kill unnamed '?' orphan server sessions when -s is specified",
    )
    p_stop.set_defaults(func=cmd_stop)

    p_watch = sub.add_parser("watch", help="Background dead-man's-switch anti-ghost-billing watchdog")
    p_watch.add_argument("-a", "--account", help="Colab account profile")
    p_watch.add_argument("-s", "--session", default=None, help="Session name (omit to kill all)")
    p_watch.add_argument("--parent-pid", type=int, default=0, help="Parent runner PID to monitor")
    p_watch.add_argument("--max-minutes", type=float, default=180.0, help="Max runtime TTL in minutes")
    p_watch.add_argument("--poll-seconds", type=float, default=5.0, help="Poll interval in seconds")
    p_watch.set_defaults(func=cmd_watch)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "cmd", None) == "stop" and not args.session:
        args.all = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
