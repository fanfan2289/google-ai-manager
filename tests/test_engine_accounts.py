"""Engine-level multi-account safety tests (mocked Colab; never touches real VMs).

Needs google-colab-cli importable (run with its uv venv python inside WSL):
    ~/.local/share/uv/tools/google-colab-cli/bin/python -m unittest tests.test_engine_accounts -v
Skipped automatically elsewhere (importing the monitor without colab_cli would re-exec).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HAVE_COLAB = importlib.util.find_spec("colab_cli") is not None

if HAVE_COLAB:
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT))
    import colab_gpu_monitor as mon  # noqa: E402


def _acct_snap(name: str, balance: float | None = 50.0, vms: int = 0) -> dict:
    return {
        "account": name, "balance": balance, "rate_hourly": 0.0,
        "assignments": [{"endpoint": f"ep-{name}-{i}"} for i in range(vms)],
        "eligible_gpus": ["T4"], "ineligible_gpus": [], "eligible_tpus": [],
        "usage_error": None if balance is not None else "boom", "updated_at_iso": None,
    }


@unittest.skipUnless(HAVE_COLAB, "google-colab-cli not importable in this interpreter")
class EngineAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(mon, "CONFIG_PATH", tmp / "cfg.json"),
            mock.patch.object(mon, "HOLD_STATE_PATH", tmp / "hold.json"),
        ]
        for p in self.patches:
            p.start()
        self.balances = {"main": 50.0, "backup": 80.0, "spare": 30.0}
        self.killed: list[str] = []
        self.remaining: dict[str, list] = {}
        states = lambda acct=None: (  # noqa: E731 - mimic colab_meter._iter_states
            [(acct, object())] if acct else [(n, object()) for n in ("main", "backup", "spare")]
        )
        self.patches += [
            mock.patch.object(mon.colab_meter, "_iter_states", side_effect=states),
            mock.patch.object(mon.colab_provider, "fetch_account_snapshot",
                              side_effect=lambda n, st: _acct_snap(n, self.balances.get(n))),
            mock.patch.object(mon.colab_meter, "_terminate_account_sessions", side_effect=self._terminate),
            mock.patch.object(mon, "_send_windows_toast", lambda *a, **k: None),  # no real desktop popups
        ]
        for p in self.patches[2:]:
            p.start()
        self.engine = mon.MonitorEngine(mon.ProbeStore(tmp / "t.db"), mon.SSEBroker(), account="main",
                                        probe_disabled=True)
        self.engine.accounts.names = lambda: ["main", "backup", "spare"]

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _terminate(self, *, acct_name, st, target_session, kill_orphans):
        if acct_name == "spare":
            raise RuntimeError("not authenticated")
        self.killed.append(acct_name)
        return 1, self.remaining.get(acct_name, [])

    def _hold(self, account: str) -> None:
        self.engine.active_hold = {"account": account, "endpoint": "ep", "state": "HOLDING", "gpu": "T4",
                                   "session_name": "h3work", "locked_at_ts": 0, "expires_at_ts": 9e12}

    # -------------------------------------------------------------- stop-all targets
    def test_default_stop_covers_hold_account_and_clears_hold(self) -> None:
        self._hold("backup")
        res = self.engine.emergency_stop_all()
        self.assertEqual(self.killed, ["backup", "main"])
        self.assertIsNone(self.engine.active_hold)
        self.assertEqual(res["remaining_count"], 0)

    def test_hold_kept_when_its_account_still_has_vms(self) -> None:
        self._hold("backup")
        self.remaining["backup"] = ["ep"]
        self.engine.emergency_stop_all()
        self.assertIsNotNone(self.engine.active_hold)
        self.assertIn("残留", self.engine.critical_alert)

    def test_stop_other_account_never_clears_hold(self) -> None:
        self._hold("backup")
        self.engine.emergency_stop_all(accounts=["main"])
        self.assertEqual(self.killed, ["main"])
        self.assertIsNotNone(self.engine.active_hold)

    def test_all_accounts_reports_unreachable_ones(self) -> None:
        res = self.engine.emergency_stop_all(all_accounts=True)
        self.assertEqual(self.killed, ["main", "backup"])
        self.assertEqual(res["failed_accounts"], ["spare"])

    def test_refresh_and_release_follow_hold_account(self) -> None:
        self._hold("backup")
        self.assertEqual(self.engine.refresh_account_snapshot()["account"], "backup")
        self.engine.release_hold(reason="test")
        self.assertEqual(self.killed, ["backup"])
        self.assertIsNone(self.engine.active_hold)
        self.assertEqual(self.engine.refresh_account_snapshot()["account"], "main")

    # -------------------------------------------------------------- failover selection
    def test_failover_picks_backup_and_blocks_are_per_account(self) -> None:
        self.engine.config.update(failover_on=True, failover_accounts=["backup", "spare"], min_ccu_threshold=60)
        name, _st, snap = self.engine._select_probe_account()
        self.assertEqual((name, snap["account"]), ("backup", "backup"))
        self.engine._block_account("backup", "RATE_LIMITED")
        self.assertEqual(self.engine._select_probe_account()[0], "main")  # spare 30 < 60 -> keep primary
        self.assertEqual(self.engine.rate_limit_until_ts, 0.0)

    def test_failover_all_blocked_skips(self) -> None:
        self.engine.config.update(failover_on=True, failover_accounts=["backup"])
        for n in ("main", "backup"):
            self.engine._block_account(n, "AUTH_ERROR")
        self.assertIsNone(self.engine._select_probe_account())
        self.assertGreater(self.engine.rate_limit_until_ts, 0)

    def test_switch_refused_while_holding(self) -> None:
        self._hold("main")
        with self.assertRaises(mon.AccountsError):
            self.engine.use_account("backup")

    def test_config_validation(self) -> None:
        cfg = self.engine.update_config({"failover_accounts": ["backup", "ghost", "backup"],
                                         "min_ccu_threshold": "-3", "failover_on": 1})
        self.assertEqual((cfg["failover_accounts"], cfg["min_ccu_threshold"], cfg["failover_on"]),
                         (["backup"], 0.0, True))


if __name__ == "__main__":
    unittest.main()
