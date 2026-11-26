"""Offline tests for the held-GPU command runner (providers/remote_runner.py).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import remote_runner as rr  # noqa: E402


class RemoteRunnerTests(unittest.TestCase):
    def build(self, **kw):
        args = dict(session="gpuwork", gpu="G4", account="main", max_minutes=60, command="nvidia-smi")
        args.update(kw)
        return rr.build_runner_argv(**args)

    def test_runner_flags_come_first_and_command_is_last_single_arg(self) -> None:
        argv = self.build(command="python3 train.py --max-minutes 0 -s other")
        self.assertEqual(argv[:3], ["bash", str(rr.RUN_SCRIPT), "--reuse-session"])
        self.assertEqual(argv[-2:], ["-c", "python3 train.py --max-minutes 0 -s other"])
        self.assertEqual(argv[argv.index("-s") + 1], "gpuwork")
        self.assertEqual(argv[argv.index("--fallback-gpu") + 1], "none")
        self.assertNotIn("--mount-drive", argv)

    def test_minutes_are_clamped(self) -> None:
        self.assertEqual(self.build(max_minutes=5)[self.build(max_minutes=5).index("--max-minutes") + 1], "30")
        self.assertEqual(rr.clamp_minutes("abc"), 180)
        self.assertEqual(rr.clamp_minutes(9999), rr.MAX_MINUTES)

    def test_mount_and_account_optional(self) -> None:
        argv = self.build(account=None, mount_drive=True)
        self.assertNotIn("-a", argv)
        self.assertIn("--mount-drive", argv)

    def test_rejects_bad_input(self) -> None:
        for kw in ({"command": "   "}, {"command": "x" * (rr.MAX_COMMAND_CHARS + 1)}, {"command": "a\0b"},
                   {"session": "-s evil"}, {"session": "a b"}, {"gpu": "g4;rm"}):
            with self.subTest(kw=kw), self.assertRaises(rr.RunnerError):
                self.build(**kw)

    def test_runner_script_exists_and_has_no_drama_defaults(self) -> None:
        src = rr.RUN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--mount-drive", src)
        self.assertNotIn("run_drama.py", src)
        self.assertNotIn("pull_latest_outputs", src)


if __name__ == "__main__":
    unittest.main()
