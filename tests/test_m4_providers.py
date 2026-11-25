"""Offline tests for M4: Antigravity cache provider, Gemini CLI status, benefits catalog, self-check.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import benefits, paths, selfcheck  # noqa: E402
from providers.antigravity import AntigravityProvider, parse_quota  # noqa: E402
from providers.base import NOT_CONFIGURED, OK  # noqa: E402
from providers.gemini_cli import PRO_DAILY_REQUESTS, GeminiCliProvider  # noqa: E402

QUOTA = {
    "last_updated": time.time() - 60,
    "subscription_tier": "PRO",
    "is_forbidden": False,
    "quota_groups": [
        {"display_name": "Gemini Models", "buckets": [
            {"bucket_id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.5, "reset_time": "2026-12-01T00:00:00Z"},
            {"bucket_id": "gemini-5h", "window": "5h", "remaining_fraction": 0.98, "reset_time": "2026-11-25T12:00:00Z"},
        ]},
        {"display_name": "Claude and GPT models", "buckets": [
            {"bucket_id": "3p-5h", "window": "5h"},                                   # missing fraction -> 0
            {"bucket_id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.9, "disabled": True},  # exhausted
        ]},
    ],
}


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class AntigravityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".antigravity_tools"
        _write(self.root / "accounts.json", {"current_account_id": "a1", "accounts": [
            {"id": "a1", "email": "Pro@Example.com", "name": "pro"},
            {"id": "a2", "email": "free@example.com", "name": "free"},
        ]})
        _write(self.root / "accounts" / "a1.json", {"email": "pro@example.com", "token": {"access_token": "SECRET"},
                                                    "quota": QUOTA})
        _write(self.root / "accounts" / "a2.json", {"email": "free@example.com", "quota": {"quota_groups": []}})
        emails = {"main": "pro@example.com", "alt": "free@example.com", "ghost": "nobody@example.com"}
        self.p = AntigravityProvider(email_of=emails.get, root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_orders_windows_and_handles_missing_or_disabled(self) -> None:
        ms = parse_quota(QUOTA)
        self.assertEqual([m.key for m in ms], ["ag_gemini-5h", "ag_gemini-weekly", "ag_3p-5h", "ag_3p-weekly"])
        self.assertEqual([m.remaining_fraction for m in ms], [0.98, 0.5, 0.0, 0.0])
        self.assertEqual(ms[3].detail, "已用尽")
        self.assertEqual((ms[0].unit, ms[0].total, ms[0].used), ("%", 100.0, 2.0))

    def test_snapshot_matches_by_email_case_insensitively_and_never_leaks_token(self) -> None:
        snap = self.p.snapshot("main")
        self.assertEqual((snap.status, snap.extra["tier"], snap.extra["current_in_tools"]), (OK, "PRO", True))
        self.assertFalse(snap.extra["stale"])
        self.assertNotIn("SECRET", json.dumps(snap.to_dict()))

    def test_unmatched_and_empty_accounts(self) -> None:
        self.assertEqual(self.p.snapshot("ghost").status, NOT_CONFIGURED)
        self.assertEqual(self.p.snapshot("alt").status, NOT_CONFIGURED)
        self.assertEqual(len(self.p.all_accounts()), 2)

    def test_unavailable_without_cache(self) -> None:
        p = AntigravityProvider(root=Path(self.tmp.name) / "missing")
        self.assertFalse(p.availability()[0])
        self.assertEqual(p.snapshot("x").status, NOT_CONFIGURED)

    def test_forbidden_is_error(self) -> None:
        _write(self.root / "accounts" / "a1.json", {"quota": {**QUOTA, "is_forbidden": True, "forbidden_reason": "banned"}})
        snap = self.p.snapshot("main")
        self.assertEqual(snap.status, "error")
        self.assertIn("banned", snap.error)


class GeminiCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def provider(self, email="me@example.com"):
        return GeminiCliProvider(email_of=lambda _a: email, homes=[self.home])

    def test_not_installed_not_logged_in(self) -> None:
        p = self.provider()
        p.installed = lambda: None  # ignore any gemini on the test machine's PATH
        self.assertEqual(p.snapshot("a").status, NOT_CONFIGURED)

    def test_logged_in_matching_account_reports_daily_limit(self) -> None:
        _write(self.home / ".gemini" / "oauth_creds.json", {"refresh_token": "SECRET"})
        _write(self.home / ".gemini" / "google_accounts.json", {"active": "me@example.com"})
        (self.home / "AppData/Roaming/npm").mkdir(parents=True)
        (self.home / "AppData/Roaming/npm/gemini.cmd").write_text("@echo off", encoding="utf-8")
        snap = self.provider().snapshot("a").to_dict()
        self.assertEqual((snap["status"], snap["metrics"][0]["total"]), (OK, PRO_DAILY_REQUESTS))
        self.assertNotIn("SECRET", json.dumps(snap))

    def test_other_signed_in_account(self) -> None:
        _write(self.home / ".gemini" / "oauth_creds.json", {})
        _write(self.home / ".gemini" / "google_accounts.json", {"active": "other@example.com"})
        self.assertEqual(self.provider().snapshot("a").status, NOT_CONFIGURED)


class BenefitsTests(unittest.TestCase):
    def test_catalog_shape_and_allowlisted_urls(self) -> None:
        ids = [b["id"] for b in benefits.BENEFITS]
        self.assertEqual(len(ids), len(set(ids)))
        for b in benefits.BENEFITS:
            self.assertIn(b["status"], ("tracked", "manual", "planned"))
            self.assertEqual(b["status"] == "tracked", bool(b.get("provider")))
            self.assertTrue(b["url"].startswith(benefits.EXTERNAL_URL_PREFIXES), b["url"])
        self.assertTrue(benefits.PLANS_URL.startswith(benefits.EXTERNAL_URL_PREFIXES))

    def test_desktop_allowlist_matches_catalog(self) -> None:
        src = (ROOT / "desktop_app" / "colab_gpu_tools.py").read_text(encoding="utf-8")
        m = re.search(r"^EXTERNAL_URL_PREFIXES = (\(.*?\n\))", src, re.S | re.M)
        self.assertIsNotNone(m)
        self.assertEqual(ast.literal_eval(m.group(1)), benefits.EXTERNAL_URL_PREFIXES)


class SelfCheckTests(unittest.TestCase):
    def test_run_checks_counts_and_isolates_crashes(self) -> None:
        def boom():
            raise RuntimeError("kaput")

        res = selfcheck.run_checks([
            ("a", "A", lambda: (selfcheck.OK, "fine")),
            ("b", "B", lambda: (selfcheck.WARN, "meh", "do x")),
            ("c", "C", boom),
            ("d", "D", lambda: ("weird", "?")),
        ])
        self.assertEqual([r["status"] for r in res["checks"]], ["ok", "warn", "fail", "fail"])
        self.assertEqual(res["checks"][1]["fix"], "do x")
        self.assertIn("kaput", res["checks"][2]["detail"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["counts"]["fail"], 2)


class WindowsPathTests(unittest.TestCase):
    def test_wsl_path_mapping(self) -> None:
        self.assertEqual(paths._wsl_path(r"C:\Users\A B").as_posix(), "/mnt/c/Users/A B")
        self.assertEqual(paths._wsl_path("D:/x/y").as_posix(), "/mnt/d/x/y")
        self.assertIsNone(paths._wsl_path("/home/u"))


if __name__ == "__main__":
    unittest.main()
