"""Offline tests for the Colab session manager helpers (providers/colab_sessions.py).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import colab_sessions as cs  # noqa: E402

RAW = [
    {"account": "main", "balance": 100.0, "rate_hourly": 12.5, "usage_error": None, "assignments": [
        {"name": "?", "endpoint": "ep-orphan", "hardware": "T4", "shape": "STANDARD", "variant": "GPU",
         "proxy_token": "SECRET", "proxy_url": "https://secret"},
        {"name": "train", "endpoint": "ep-train", "hardware": "A100", "shape": "HIGH_RAM", "variant": "GPU"},
        {"name": "gpuwork", "endpoint": "ep-hold", "hardware": "G4", "shape": "STANDARD", "variant": "GPU"},
    ]},
    {"account": "alt", "assignments": [], "error": "not authenticated"},
]
HOLD = {"account": "main", "endpoint": "ep-hold", "state": "HOLDING", "locked_at_ts": 900.0,
        "expires_at_ts": 1600.0, "gpu_model": "RTX PRO 6000", "drive_status": "SKIPPED"}


class PayloadTests(unittest.TestCase):
    def test_sources_order_totals_and_no_secrets(self) -> None:
        out = cs.build_sessions_payload(RAW, hold=HOLD, first_seen={"ep-orphan": 400.0}, now=1000.0)
        rows = out["accounts"][0]["sessions"]
        self.assertEqual([r["source"] for r in rows], ["hold", "cli", "untracked"])
        self.assertIsNone(rows[2]["name"])
        self.assertEqual(rows[2]["seen_for_s"], 600)
        self.assertEqual(rows[0]["hold"]["remaining_s"], 600)
        self.assertEqual(out["totals"], {"sessions": 3, "untracked": 1, "rate_hourly": 12.5})
        self.assertEqual(out["accounts"][1]["error"], "not authenticated")
        self.assertNotIn("SECRET", repr(out))
        self.assertNotIn("proxy", repr(out))

    def test_hold_on_other_account_is_not_matched(self) -> None:
        out = cs.build_sessions_payload(RAW, hold={**HOLD, "account": "alt"}, first_seen={}, now=1000.0)
        self.assertNotIn("hold", [r["source"] for r in out["accounts"][0]["sessions"]])


class ValidationTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(cs.check_session_name(" train-1 "), "train-1")
        for bad in ("", "-s", "a b", "x;rm", "../x", "a" * 65):
            with self.subTest(bad=bad), self.assertRaises(cs.SessionError):
                cs.check_session_name(bad)
        self.assertEqual(cs.check_account_name("main", ["main"]), "main")
        with self.assertRaises(cs.SessionError):
            cs.check_account_name("ghost", ["main"])

    def test_remote_paths(self) -> None:
        self.assertEqual(cs.check_remote_path(None), "/content")
        self.assertEqual(cs.check_remote_path("/content/a b.txt"), "/content/a b.txt")
        for bad in ("--help", "/x\n/y", "a\0", "x" * 501):
            with self.subTest(bad=bad), self.assertRaises(cs.SessionError):
                cs.check_remote_path(bad)

    def test_download_target_stays_inside_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            t = cs.download_target(base, "main", "train", "/content/out/model.pt")
            self.assertEqual(t, (base / "main" / "train" / "model.pt").resolve())
            for bad in ("/content/..", "/", ""):
                with self.subTest(bad=bad), self.assertRaises(cs.SessionError):
                    cs.download_target(base, "main", "train", bad)

    def test_url_parsing(self) -> None:
        url = ("https://colab.research.google.com/notebooks/empty.ipynb?dbu=%2Ftun%2Fm%2Fep"
               "#datalabBackendUrl=https://colab.research.google.com/tun/m/ep")
        self.assertEqual(cs.parse_session_url(f"[colab] building\n{url}\n"), url)
        with self.assertRaises(cs.SessionError):
            cs.parse_session_url("https://evil.example/x")

    def test_clean_output_drops_progress_lines(self) -> None:
        self.assertEqual(cs.clean_cli_output("[colab] connecting\nhello\n  [colab] x\nworld"), "hello\nworld")

    def test_desktop_regexes_match(self) -> None:
        src = (ROOT / "desktop_app" / "colab_gpu_tools.py").read_text(encoding="utf-8")
        sess = re.search(r'^_SESSION_RE = re\.compile\(r"(.*)"\)$', src, re.M).group(1)
        acct = re.search(r'^_ACCOUNT_RE = re\.compile\(r"(.*)"\)$', src, re.M).group(1)
        self.assertEqual((sess, acct), (cs.SESSION_RE.pattern, cs.ACCOUNT_RE.pattern))


if __name__ == "__main__":
    unittest.main()
