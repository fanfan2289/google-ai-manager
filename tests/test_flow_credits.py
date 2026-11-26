"""Offline tests for Google Flow credits (backend store/provider + desktop page-result parsing).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop_app"))

from providers import benefits  # noqa: E402
from providers.base import ERROR, NOT_CONFIGURED, OK  # noqa: E402
from providers.flow_credits import (  # noqa: E402
    FlowCreditsProvider,
    FlowCreditsStore,
    FlowReportError,
    validate_report,
)

try:
    import flow_credits as desktop_flow  # desktop_app/flow_credits.py (stdlib only at import time)
except Exception:  # pragma: no cover
    desktop_flow = None


class ReportValidationTests(unittest.TestCase):
    def test_accepts_numbers_and_drops_everything_else(self) -> None:
        rep = validate_report({"account": "main", "ok": True, "credits": 980, "email_verified": True,
                               "token": "SECRET", "email": "x@example.com"}, ["main"], now=100.0)
        self.assertEqual(rep, {"account": "main", "record": {"ok": True, "connected": True, "checked_at": 100.0,
                                                              "credits": 980, "email_verified": True}})
        self.assertNotIn("SECRET", json.dumps(rep))

    def test_rejects_bad_input(self) -> None:
        for body in ({"account": "ghost", "ok": True, "credits": 1},
                     {"account": "main", "ok": True, "credits": -1},
                     {"account": "main", "ok": True, "credits": "980"},
                     {"account": "main", "ok": True, "credits": True}):
            with self.subTest(body=body), self.assertRaises(FlowReportError):
                validate_report(body, ["main"])

    def test_error_text_is_capped(self) -> None:
        rec = validate_report({"account": "main", "ok": False, "error": "x" * 999}, ["main"])["record"]
        self.assertEqual(len(rec["error"]), 200)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FlowCreditsStore(Path(self.tmp.name) / "flow.json")
        self.enabled = True
        self.p = FlowCreditsProvider(self.store, enabled=lambda: self.enabled)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_disabled_and_unconnected(self) -> None:
        self.enabled = False
        self.assertEqual(self.p.snapshot("main").status, NOT_CONFIGURED)
        self.enabled = True
        self.assertEqual(self.p.snapshot("main").status, NOT_CONFIGURED)

    def test_ok_then_error_keeps_last_balance(self) -> None:
        self.store.put("main", {"ok": True, "connected": True, "checked_at": time.time(), "credits": 980})
        snap = self.p.snapshot("main")
        self.assertEqual((snap.status, snap.metrics[0].remaining, snap.metrics[0].total), (OK, 980.0, 1000.0))
        self.assertFalse(snap.extra["stale"])
        self.store.put("main", {"ok": False, "connected": True, "checked_at": time.time(), "error": "boom"})
        snap = self.p.snapshot("main")
        self.assertEqual((snap.status, snap.extra["last_credits"]), (ERROR, 980))

    def test_topped_up_balance_has_no_fake_total(self) -> None:
        self.store.put("main", {"ok": True, "connected": True, "checked_at": time.time() - 4 * 3600, "credits": 3500})
        snap = self.p.snapshot("main")
        self.assertIsNone(snap.metrics[0].total)
        self.assertTrue(snap.extra["stale"])

    def test_benefit_is_tracked_and_url_allowlisted(self) -> None:
        b = next(x for x in benefits.BENEFITS if x["id"] == "flow")
        self.assertEqual((b["status"], b["provider"]), ("tracked", "flow_credits"))
        self.assertTrue(b["url"].startswith(benefits.EXTERNAL_URL_PREFIXES))


@unittest.skipIf(desktop_flow is None, "desktop flow reader not importable")
class PageResultTests(unittest.TestCase):
    def test_balance(self) -> None:
        self.assertEqual(desktop_flow.normalize({"ok": True, "stage": "rpc", "status": 200, "credits": 980,
                                                 "email_match": True}),
                         {"ok": True, "credits": 980, "email_verified": True})

    def test_wrong_account_never_shows_number(self) -> None:
        out = desktop_flow.normalize({"ok": True, "credits": 980, "email_match": False})
        self.assertFalse(out["ok"])
        self.assertNotIn("credits", out)

    def test_signed_out_and_changes(self) -> None:
        self.assertIn("未登录", desktop_flow.normalize({"ok": False, "stage": "shell", "signed_in": False})["error"])
        self.assertIn("格式", desktop_flow.normalize({"ok": False, "stage": "rpc", "status": 200,
                                                      "error": "no_frame"})["error"])
        self.assertIn("限流", desktop_flow.normalize({"ok": False, "stage": "rpc", "status": 200,
                                                      "error": "rpc_error"})["error"])
        self.assertFalse(desktop_flow.normalize({"ok": True, "credits": -5})["ok"])
        self.assertFalse(desktop_flow.normalize(None)["ok"])

    def test_profile_names_are_validated(self) -> None:
        for bad in ("", "../x", "a b", "-x"):
            with self.subTest(bad=bad), self.assertRaises(desktop_flow.FlowError):
                desktop_flow.profile_dir(bad)

    def test_page_script_keeps_token_in_page(self) -> None:
        script = desktop_flow.PAGE_SCRIPT
        self.assertIn("SNlM0e", script)
        self.assertNotIn("out.at", script)  # the token is never copied into the returned object
        self.assertIn("nzlxg", script)


if __name__ == "__main__":
    unittest.main()
