"""Offline unit tests for providers/ (no network, no Colab, no credentials).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import ProviderHub  # noqa: E402
from providers import netcfg, paths  # noqa: E402
from providers.base import ERROR, Metric, OK, ProviderSnapshot, QuotaProvider  # noqa: E402
from providers.google_storage import parse_storage_quota  # noqa: E402

PROXY_KEYS = netcfg.PROXY_VARS + netcfg.NO_PROXY_VARS + (netcfg.INHERITED_MARK,)


class _EnvSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in PROXY_KEYS + ("GAM_HOME",)}
        for k in PROXY_KEYS:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["GAM_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class NetcfgTests(_EnvSandbox):
    def test_defaults_when_no_settings_file(self) -> None:
        cfg = netcfg.load_proxy_settings()
        self.assertEqual(cfg["mode"], "auto")
        self.assertEqual(cfg["auto_ports"], [7897, 7890, 10809])

    def test_save_validates_and_persists_private_file(self) -> None:
        cfg = netcfg.save_proxy_settings({"mode": "manual", "manual_url": "http://127.0.0.1:8888",
                                          "auto_ports": "1080, 1080, 7890"})
        self.assertEqual(cfg["auto_ports"], [1080, 7890])
        self.assertEqual(netcfg.load_proxy_settings()["manual_url"], "http://127.0.0.1:8888")
        if os.name == "posix":
            self.assertEqual(os.stat(paths.settings_path()).st_mode & 0o777, 0o600)

    def test_rejects_bad_values(self) -> None:
        for patch in ({"mode": "vpn"}, {"manual_url": "127.0.0.1:7890"}, {"auto_ports": "99999"},
                      {"auto_hosts": "bad host!"}):
            with self.assertRaises(ValueError):
                netcfg.save_proxy_settings(patch)

    def test_off_and_manual_resolution(self) -> None:
        self.assertEqual(netcfg.resolve_proxy({**netcfg.DEFAULT_PROXY_SETTINGS, "mode": "off"})["url"], "")
        r = netcfg.resolve_proxy({**netcfg.DEFAULT_PROXY_SETTINGS, "mode": "manual",
                                  "manual_url": "socks5h://u:p@10.0.0.1:1080"})
        self.assertEqual(r["source"], "manual")
        self.assertNotIn("u:p", r["detail"])  # credentials are redacted in human-readable output

    def test_auto_prefers_inherited_env_and_survives_reapply(self) -> None:
        os.environ["HTTPS_PROXY"] = "http://10.9.9.9:3128"
        cfg = {**netcfg.DEFAULT_PROXY_SETTINGS, "auto_ports": []}
        self.assertEqual(netcfg.apply_proxy_env(cfg)["source"], "env")
        # Switch to manual then back to auto: the user's original env proxy must come back.
        netcfg.apply_proxy_env({**cfg, "mode": "manual", "manual_url": "http://127.0.0.1:1"})
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://127.0.0.1:1")
        r = netcfg.apply_proxy_env(cfg)
        self.assertEqual((r["source"], os.environ["HTTPS_PROXY"]), ("env", "http://10.9.9.9:3128"))

    def test_auto_detects_listening_port(self) -> None:
        import socket

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            r = netcfg.resolve_proxy({**netcfg.DEFAULT_PROXY_SETTINGS, "auto_hosts": ["127.0.0.1"],
                                      "auto_ports": [port]})
            self.assertEqual((r["source"], r["url"]), ("auto", f"http://127.0.0.1:{port}"))
        finally:
            srv.close()

    def test_off_clears_proxy_env_and_shell_env_unsets(self) -> None:
        os.environ["HTTP_PROXY"] = "http://1.2.3.4:5"
        netcfg.save_proxy_settings({"mode": "off"})
        out = netcfg.shell_env()
        self.assertIn("unset HTTPS_PROXY", out)
        self.assertNotIn("HTTP_PROXY", os.environ)
        self.assertIn("localhost", os.environ["NO_PROXY"])


class StorageParseTests(unittest.TestCase):
    def test_parse_limited_quota(self) -> None:
        tb = 1024 ** 4
        metrics = parse_storage_quota({"storageQuota": {"limit": str(5 * tb), "usage": str(tb),
                                                        "usageInDrive": str(tb // 2), "usageInDriveTrash": "0"}})
        total = metrics[0]
        self.assertEqual((total.total, total.used, total.remaining), (5 * tb, tb, 4 * tb))
        self.assertAlmostEqual(total.remaining_fraction, 0.8)
        self.assertEqual([m.key for m in metrics][-1], "storage_other")

    def test_parse_unlimited_quota(self) -> None:
        total = parse_storage_quota({"storageQuota": {"usage": "100"}})[0]
        self.assertIsNone(total.total)
        self.assertEqual(total.detail, "无上限")


class _FakeProvider(QuotaProvider):
    id = "fake"
    label = "Fake"
    official_api = True

    def __init__(self, fail: bool = False, delay: float = 0.0) -> None:
        self.calls = 0
        self.fail = fail
        self.delay = delay
        self._lock = threading.Lock()

    def snapshot(self, account: str) -> ProviderSnapshot:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return ProviderSnapshot(provider=self.id, account=account, metrics=[Metric(key="x", label="x", used=1, total=2)])


class HubTests(unittest.TestCase):
    def test_cache_and_force_floor(self) -> None:
        hub = ProviderHub(ttl_seconds=60)
        p = hub.register(_FakeProvider())
        hub.snapshot("fake", "a")
        hub.snapshot("fake", "a")
        self.assertEqual(p.calls, 1)
        hub.snapshot("fake", "a", force=True)  # within the 5s floor for official APIs
        self.assertEqual(p.calls, 1)
        hub.snapshot("fake", "b")
        self.assertEqual(p.calls, 2)

    def test_concurrent_refresh_is_serialised(self) -> None:
        hub = ProviderHub(ttl_seconds=60)
        p = hub.register(_FakeProvider(delay=0.2))
        threads = [threading.Thread(target=hub.snapshot, args=("fake", "a")) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(p.calls, 1)

    def test_exceptions_become_error_snapshots(self) -> None:
        hub = ProviderHub()
        hub.register(_FakeProvider(fail=True))
        snap = hub.snapshot_many("a")["fake"]
        self.assertEqual(snap["status"], ERROR)
        self.assertIn("boom", snap["error"])

    def test_ok_snapshot_serialises(self) -> None:
        hub = ProviderHub()
        hub.register(_FakeProvider())
        snap = hub.snapshot_many("a")["fake"]
        self.assertEqual(snap["status"], OK)
        self.assertEqual(snap["metrics"][0]["remaining"], 1)


if __name__ == "__main__":
    unittest.main()
