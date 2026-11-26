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
                                   "session_name": "gpuwork", "locked_at_ts": 0, "expires_at_ts": 9e12}

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

    # -------------------------------------------------------------- v2.3: config migration & command runner
    def test_migration_turns_off_auto_mount_once_and_renames_legacy_session(self) -> None:
        cfg, changed = mon.migrate_config({"auto_mount_drive": True, "hold_session_name": "h3work"})
        self.assertTrue(changed)
        self.assertEqual((cfg["auto_mount_drive"], cfg["hold_session_name"], cfg["config_version"]),
                         (False, "gpuwork", mon.CONFIG_VERSION))
        custom, _ = mon.migrate_config({"hold_session_name": "mybox"})
        self.assertEqual(custom["hold_session_name"], "mybox")
        again, changed = mon.migrate_config({**cfg, "auto_mount_drive": True})  # user re-enabled it later
        self.assertFalse(changed)
        self.assertTrue(again["auto_mount_drive"])

    def test_saved_v2_config_is_migrated_on_load(self) -> None:
        mon.CONFIG_PATH.write_text('{"auto_mount_drive": true, "hold_session_name": "h3work"}', encoding="utf-8")
        cfg = self.engine._load_config()
        self.assertFalse(cfg["auto_mount_drive"])
        self.assertIn('"config_version": 3', mon.CONFIG_PATH.read_text(encoding="utf-8"))

    def test_run_command_requires_hold(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.run_command("nvidia-smi")

    def test_run_command_runner_owns_safety_flags(self) -> None:
        self._hold("backup")
        started = {}

        class FakeProc:
            pid = 4242

            def poll(self):
                return None

        def fake_popen(argv, **kw):
            started["argv"] = argv
            return FakeProc()

        with mock.patch.object(mon.subprocess, "Popen", side_effect=fake_popen),                 mock.patch.object(mon, "LOGS_DIR", Path(self.tmp.name)):
            res = self.engine.run_command("echo hi --keep-alive", max_minutes=9999, mount_drive=True)
        argv = started["argv"]
        self.assertTrue(res["ok"])
        self.assertEqual(argv[-2:], ["-c", "echo hi --keep-alive"])  # user text stays one argv element
        self.assertNotIn("--keep-alive", argv[:-1])
        self.assertEqual(argv[argv.index("--max-minutes") + 1], "360")
        self.assertEqual(argv[argv.index("-a") + 1], "backup")
        self.assertIn("--mount-drive", argv)
        self.assertEqual(self.engine.active_hold["state"], "CLAIMED_JOB")
        self.engine._job_proc = None

    # -------------------------------------------------------------- v2.3: Colab session manager
    def _listing(self, *rows):
        q = {"account": "main", "balance": 50.0, "rate_hourly": 1.0, "usage_error": None, "assignments": list(rows)}
        return mock.patch.object(
            mon.colab_meter, "_query_account",
            side_effect=lambda n, st: q if n == "main" else {**q, "account": n, "assignments": []})

    def test_sessions_listing_marks_hold_and_untracked(self) -> None:
        self._hold("main")
        self.engine.active_hold["endpoint"] = "ep-hold"
        with self._listing({"name": "gpuwork", "endpoint": "ep-hold", "hardware": "T4", "shape": "STANDARD", "variant": "GPU"},
                           {"name": "?", "endpoint": "ep-x", "hardware": "CPU", "shape": "STANDARD", "variant": "DEFAULT"}):
            out = self.engine.colab_sessions_payload(force=True)
        main = next(a for a in out["accounts"] if a["account"] == "main")
        self.assertEqual([r["source"] for r in main["sessions"]], ["hold", "untracked"])

    def test_stop_session_only_targets_one_endpoint(self) -> None:
        calls = []
        with self._listing({"name": "?", "endpoint": "ep-x", "hardware": "CPU", "shape": "", "variant": ""}), \
                mock.patch.object(mon.colab_meter, "_terminate_endpoint",
                                  side_effect=lambda a, st, ep: calls.append((a, ep)) or (True, False)):
            res = self.engine.stop_session("main", "ep-x")
        self.assertEqual(calls, [("main", "ep-x")])
        self.assertTrue(res["ok"])
        self.assertEqual(self.killed, [])  # no account-wide kill

    def test_stop_session_on_hold_goes_through_release(self) -> None:
        self._hold("main")
        self.engine.active_hold["endpoint"] = "ep-hold"
        with self._listing({"name": "gpuwork", "endpoint": "ep-hold", "hardware": "T4", "shape": "", "variant": ""}):
            self.engine.stop_session("main", "ep-hold")
        self.assertEqual(self.killed, ["main"])
        self.assertIsNone(self.engine.active_hold)

    def test_session_actions_validate_input(self) -> None:
        with self._listing({"name": "?", "endpoint": "ep-x", "hardware": "CPU", "shape": "", "variant": ""},
                           {"name": "train", "endpoint": "ep-t", "hardware": "T4", "shape": "", "variant": ""}):
            for body, action in (({"account": "ghost", "name": "train"}, "info"),
                                 ({"account": "main", "name": "nope"}, "info"),
                                 ({"account": "main", "name": "-s x"}, "info"),
                                 ({"account": "main", "name": "train", "path": "--help"}, "ls"),
                                 ({"account": "main", "name": "train"}, "bogus")):
                with self.subTest(action=action, body=body), self.assertRaises(ValueError):
                    self.engine.session_action(action, body)
            ran = []

            def fake_cli(acct, args, **kw):
                ran.append((acct, args))
                return mon.subprocess.CompletedProcess(args, 0, "[colab] x\nfile.txt", "")

            with mock.patch.object(self.engine, "_colab_cli", side_effect=fake_cli):
                res = self.engine.session_action("ls", {"account": "main", "name": "train", "path": "/content/x"})
            self.assertEqual(ran, [("main", ["ls", "-s", "train", "/content/x"])])
            self.assertEqual(res["text"], "file.txt")


if __name__ == "__main__":
    unittest.main()
