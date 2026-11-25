"""Offline tests for providers/accounts_hub.py (fake colab-ext backend + fake CLI runner).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import ProviderHub  # noqa: E402
from providers.accounts_hub import (  # noqa: E402
    AccountsError,
    AccountsHub,
    aggregate_account_totals,
    choose_probe_account,
    meta_path,
    normalize_code,
    validate_meta_patch,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/auth?client_id=x&state=y"


class FakeBackend:
    """Mimics the subset of colab_ext.accounts the hub reads."""

    def __init__(self, root: str, names=("main", "backup")) -> None:
        self.root = root
        self.reg = {"active": names[0], "accounts": {n: {"email": f"{n}@example.com"} for n in names}}
        self.ready = {n: True for n in names}

    def load_registry(self):
        return self.reg

    def inspect_colab_auth_nonblocking(self, name):
        return {"provider": "oauth2", "ready": self.ready.get(name), "storage": "file"}

    def resolve_drive_account(self, name, for_consent=False):
        return name, "self"

    def _p(self, name, leaf):
        return os.path.join(self.root, name, leaf)

    def dfs_store_path(self, name):
        return self._p(name, "dfs_credentials.json")

    def dfs_pending_path(self, name):
        return self._p(name, "dfs_pending.json")

    def oauth_pending_path(self, name):
        return self._p(name, "oauth_pending.json")

    def read_local_sessions(self, name):
        return {}


class FakeRunner:
    def __init__(self, stdout: str = AUTH_URL + "\n", rc: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout, self.rc, self.stderr = stdout, rc, stderr

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.rc, self.stdout, self.stderr)


class _HubCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_home = os.environ.get("GAM_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["GAM_HOME"] = os.path.join(self._tmp.name, "gam")
        self.backend = FakeBackend(os.path.join(self._tmp.name, "colab"))
        self.runner = FakeRunner()
        self.hub = AccountsHub(backend=self.backend, runner=self.runner)

    def tearDown(self) -> None:
        if self._saved_home is None:
            os.environ.pop("GAM_HOME", None)
        else:
            os.environ["GAM_HOME"] = self._saved_home
        self._tmp.cleanup()


class ValidationTests(unittest.TestCase):
    def test_normalize_code_accepts_bare_code_and_redirect_url(self) -> None:
        self.assertEqual(normalize_code("  4/0AbCdEfGhIjK  "), "4/0AbCdEfGhIjK")
        self.assertEqual(normalize_code("https://sdk.cloud.google.com/authcode.html?state=s&code=4%2F0AbCdEfGhIjK"),
                         "4/0AbCdEfGhIjK")

    def test_normalize_code_rejects_garbage(self) -> None:
        for bad in ("", "short", "4/0Ab; rm -rf /", "https://example.com/?nocode=1"):
            with self.assertRaises(AccountsError):
                normalize_code(bad)

    def test_meta_patch(self) -> None:
        clean = validate_meta_patch({"label": " Main ", "tags": "Pro, 主号，Pro", "color": "blue", "note": "x"})
        self.assertEqual(clean, {"label": "Main", "tags": ["Pro", "主号"], "color": "blue", "note": "x"})
        for bad in ({"label": "x" * 41}, {"color": "pink"}, {"tags": ["t"] * 0 + [f"t{i}" for i in range(9)]},
                    {"tags": ["x" * 17]}):
            with self.assertRaises(AccountsError):
                validate_meta_patch(bad)


class HubTests(_HubCase):
    def test_start_add_runs_url_only_without_switching_cli_account(self) -> None:
        res = self.hub.start_add("backup2", "b2@example.com")
        self.assertEqual(res["url"], AUTH_URL)
        self.assertEqual(self.runner.calls[0],
                         ["account", "add", "backup2", "--url-only", "--no-use", "--no-browser", "--email", "b2@example.com"])

    def test_start_add_rejects_bad_or_existing_names(self) -> None:
        for bad in ("me@example.com", "-x", "", "a" * 33, "main"):
            with self.assertRaises(AccountsError):
                self.hub.start_add(bad)
        self.assertEqual(self.runner.calls, [])

    def test_missing_url_and_cli_failure_surface_as_errors(self) -> None:
        self.runner.stdout = "no link here"
        with self.assertRaises(AccountsError):
            self.hub.start_login("main")
        self.runner.rc, self.runner.stderr = 1, "# hint\n[account] Account 'main' is locked"
        with self.assertRaises(AccountsError) as ctx:
            self.hub.start_drive("main")
        self.assertIn("locked", str(ctx.exception))
        self.assertNotIn("hint", str(ctx.exception))

    def test_finish_steps_pass_normalized_code(self) -> None:
        self.hub.finish_login("backup", "https://x.example/cb?code=4/0AbCdEfGhIjK")
        self.hub.finish_drive("backup", "4/0AbCdEfGhIjK")
        self.assertEqual(self.runner.calls, [
            ["account", "login", "backup", "--code", "4/0AbCdEfGhIjK"],
            ["drive", "consent", "-a", "backup", "--code", "4/0AbCdEfGhIjK"],
        ])
        with self.assertRaises(AccountsError):
            self.hub.finish_login("ghost", "4/0AbCdEfGhIjK")

    def test_meta_is_private_and_merged_into_listing(self) -> None:
        self.hub.set_meta("backup", {"label": "备用", "tags": ["Pro"]})
        if os.name == "posix":
            self.assertEqual(os.stat(meta_path()).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(meta_path().parent).st_mode & 0o777, 0o700)
        rows = {r["name"]: r for r in self.hub.list_accounts()["accounts"]}
        self.assertEqual(rows["backup"]["meta"]["label"], "备用")
        self.assertEqual(rows["main"]["meta"]["tags"], [])
        self.assertTrue(rows["main"]["cli_active"])
        self.assertFalse(rows["backup"]["drive"]["ready"])

    def test_remove_drops_meta_and_keeps_last_account(self) -> None:
        self.hub.set_meta("backup", {"note": "bye"})
        self.hub.remove("backup")
        self.assertEqual(self.runner.calls[-1], ["account", "remove", "backup"])
        self.assertNotIn("backup", self.hub.load_meta())
        del self.backend.reg["accounts"]["backup"]
        with self.assertRaises(AccountsError):
            self.hub.remove("main")


def _snap(status="ok", **metrics):
    return {"status": status, "metrics": [{"key": k, **v} for k, v in metrics.items()]}


class AggregateTests(unittest.TestCase):
    def test_totals_skip_errors_and_dedupe_shared_drive(self) -> None:
        tb = 1024 ** 4
        storage = _snap(storage_total={"used": tb, "total": 5 * tb})
        per = {
            "a": {"colab": _snap(ccu_balance={"remaining": 100.5}, burn_rate={"used": 1.2}, active_vms={"used": 1}),
                  "google_storage": {**storage, "extra": {"drive_account": "a"}}},
            "b": {"colab": _snap(ccu_balance={"remaining": 50.0}, burn_rate={"used": 0}, active_vms={"used": 0}),
                  "google_storage": {**storage, "extra": {"drive_account": "a"}}},  # shares a's Drive
            "c": {"colab": _snap("unauthorized"), "google_storage": _snap("not_configured")},
        }
        t = aggregate_account_totals(per)
        self.assertEqual((t["accounts"], t["ccu_balance"], t["ccu_accounts"]), (3, 150.5, 2))
        self.assertEqual((t["burn_rate"], t["active_vms"]), (1.2, 1))
        self.assertEqual((t["storage_used"], t["storage_total"], t["storage_accounts"]), (tb, 5 * tb, 1))


class FailoverTests(unittest.TestCase):
    def pick(self, snaps, *, on=True, blocked=None, backups=("b", "c"), threshold=5.0):
        calls = []

        def snapshot_of(name):
            calls.append(name)
            val = snaps.get(name)
            if isinstance(val, Exception):
                raise val
            return val

        res = choose_probe_account("a", list(backups), failover_on=on, threshold=threshold,
                                   blocked_until=blocked or {}, now=100.0, snapshot_of=snapshot_of)
        return res, calls

    def test_off_always_uses_primary(self):
        (name, snap, note), calls = self.pick({"a": {"balance": 0.0}}, on=False, blocked={"a": 999})
        self.assertEqual((name, note, calls), ("a", "", ["a"]))

    def test_healthy_primary_never_queries_backups(self):
        (name, _, note), calls = self.pick({"a": {"balance": 50.0}})
        self.assertEqual((name, note, calls), ("a", "", ["a"]))

    def test_low_ccu_switches_to_first_usable_backup(self):
        snaps = {"a": {"balance": 1.0}, "b": {"balance": 2.0}, "c": {"balance": 80.0}}
        (name, snap, note), calls = self.pick(snaps)
        self.assertEqual((name, snap["balance"], calls), ("c", 80.0, ["a", "b", "c"]))
        self.assertIn("CCU", note)

    def test_blocked_primary_and_broken_backup(self):
        snaps = {"b": RuntimeError("auth"), "c": {"balance": 9.0, "usage_error": None}}
        (name, _, note), calls = self.pick(snaps, blocked={"a": 200.0})
        self.assertEqual((name, calls), ("c", ["b", "c"]))  # blocked primary is not queried
        self.assertIn("受限", note)

    def test_usage_error_snapshot_is_not_usable(self):
        snaps = {"a": {"balance": 1.0}, "b": {"balance": 99.0, "usage_error": "boom"}, "c": None}
        (name, _, note), _ = self.pick(snaps)
        self.assertEqual(name, "a")  # no usable backup -> keep primary
        self.assertIn("继续使用主号", note)

    def test_everything_blocked_skips_cycle(self):
        (name, snap, note), calls = self.pick({}, blocked={"a": 200.0, "b": 200.0, "c": 300.0})
        self.assertEqual((name, snap, calls), (None, None, []))
        self.assertIn("跳过", note)

    def test_expired_blocks_and_duplicate_backups(self):
        snaps = {"a": {"balance": 0.0}, "b": {"balance": 10.0}}
        (name, _, _), calls = self.pick(snaps, blocked={"b": 50.0}, backups=("a", "b", "b"))
        self.assertEqual((name, calls), ("b", ["a", "b"]))


class HubInvalidateTests(unittest.TestCase):
    def test_invalidate_one_account(self) -> None:
        from providers.base import ProviderSnapshot

        hub = ProviderHub()
        hub.put(ProviderSnapshot(provider="colab", account="a"))
        hub.put(ProviderSnapshot(provider="colab", account="b"))
        hub.invalidate("a")
        self.assertIsNone(hub.cached("colab", "a"))
        self.assertIsNotNone(hub.cached("colab", "b"))


if __name__ == "__main__":
    unittest.main()
