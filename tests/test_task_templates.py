"""Offline tests for task templates (tasks/templates/*.json)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers.task_templates import TemplateError, build_args, load_templates, validate_template  # noqa: E402


class TemplateTests(unittest.TestCase):
    def test_shipped_templates_are_valid(self) -> None:
        templates, errors = load_templates()
        self.assertEqual(errors, [])
        ids = [t["id"] for t in templates]
        self.assertEqual(ids, ["minimax_h3_drama", "custom_command"])

    def test_drama_defaults_match_previous_behaviour(self) -> None:
        t = next(t for t in load_templates()[0] if t["id"] == "minimax_h3_drama")
        args = build_args(t, {"workflow": "w.json"})
        self.assertIn("--workflow-dir", args)
        self.assertEqual(args[args.index("--workflow") + 1], "w.json")
        self.assertEqual(args[args.index("--config") + 1], "drama_config.json")

    def test_reserved_flags_are_rejected(self) -> None:
        for flag in ("--keep-alive", "--max-minutes", "-a", "--fallback-gpu", "--reuse-session"):
            with self.assertRaises(TemplateError):
                validate_template({"id": "x", "params": [{"key": "k", "flag": flag}]})
            with self.assertRaises(TemplateError):
                validate_template({"id": "x", "args": [flag]})

    def test_param_validation(self) -> None:
        t = validate_template({"id": "x", "params": [
            {"key": "p", "flag": "--workflow", "type": "path", "default": "a.json"},
            {"key": "n", "flag": "--n", "type": "number", "min": 1, "max": 5},
            {"key": "c", "flag": "-c", "type": "shell", "required": True},
        ]})
        self.assertEqual(build_args(t, {"n": "3", "c": "echo hi; nvidia-smi"}),
                         ["--workflow", "a.json", "--n", "3", "-c", "echo hi; nvidia-smi"])
        for bad in ({"c": ""}, {"c": "x", "n": 9}, {"c": "x", "p": "--keep-alive"}, {"c": "x", "p": "a;rm -rf /"},
                    {"c": "x" * 5000}):
            with self.assertRaises(TemplateError, msg=str(bad)):
                build_args(t, bad)

    def test_invalid_files_are_reported_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "good.json").write_text(json.dumps({"id": "good"}), encoding="utf-8")
            Path(d, "mismatch.json").write_text(json.dumps({"id": "other"}), encoding="utf-8")
            Path(d, "broken.json").write_text("{", encoding="utf-8")
            templates, errors = load_templates(Path(d))
        self.assertEqual([t["id"] for t in templates], ["good"])
        self.assertEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
