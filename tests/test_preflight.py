from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from word_formula_omml.preflight import run_preflight


class PreflightTests(unittest.TestCase):
    def test_supported_python_range_is_reported_without_truncating_upper_bound(self):
        report = run_preflight(pandoc="definitely-not-installed-pandoc", python_version=(3, 11))
        python = next(item for item in report.to_dict()["checks"] if item["name"] == "python")
        self.assertEqual(">=3.10,<3.13", python["required"])

    def test_missing_pandoc_is_actionable_and_does_not_claim_readiness(self):
        report = run_preflight(pandoc="definitely-not-installed-pandoc")
        self.assertEqual("FAIL", report.status)
        self.assertFalse(report.portable_ready)
        self.assertFalse(report.mutation_ready)
        pandoc = next(item for item in report.to_dict()["checks"] if item["name"] == "pandoc")
        self.assertIn("not found", pandoc["reason"])

    def _fake_pandoc(self, directory: Path, *, version: str = "3.1.11", api: str = "[1, 23]") -> Path:
        pandoc = directory / "pandoc"
        pandoc.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv:\n"
            f"    print('pandoc {version}')\n"
            "else:\n"
            f"    print(json.dumps({{'pandoc-api-version': {api}}}))\n",
            encoding="utf-8",
        )
        pandoc.chmod(pandoc.stat().st_mode | stat.S_IXUSR)
        return pandoc

    def _companion(self, directory: Path) -> Path:
        companion = directory / "docx-skill"
        companion.mkdir()
        (companion / "SKILL.md").write_text("reviewed companion guidance\n", encoding="utf-8")
        (companion / "ooxml.md").write_text("reviewed OOXML guidance\n", encoding="utf-8")
        return companion

    def test_companion_must_be_content_pinned_before_mutation_ready(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            pandoc = self._fake_pandoc(directory)
            companion = self._companion(directory)

            discovery = run_preflight(
                pandoc=str(pandoc),
                companion_root=companion,
                python_version=(3, 11),
            )
            companion_check = next(item for item in discovery.checks if item["name"] == "companion_docx_skill")
            self.assertEqual("UNPINNED", companion_check["state"])
            self.assertRegex(companion_check["fingerprint"], r"^[0-9a-f]{64}$")
            self.assertFalse(discovery.mutation_ready)

            pinned = run_preflight(
                pandoc=str(pandoc),
                companion_root=companion,
                companion_fingerprint=companion_check["fingerprint"],
                require_companion=True,
                python_version=(3, 11),
            )
            self.assertEqual("PASS", pinned.status)
            self.assertTrue(pinned.portable_ready)
            self.assertTrue(pinned.mutation_ready)
            self.assertEqual("PASS", next(item for item in pinned.checks if item["name"] == "companion_docx_skill")["state"])
            self.assertEqual("UNAVAILABLE", next(item for item in pinned.checks if item["name"] == "microsoft_word")["state"])

    def test_changed_companion_content_invalidates_reviewed_fingerprint(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            pandoc = self._fake_pandoc(directory)
            companion = self._companion(directory)
            discovery = run_preflight(pandoc=str(pandoc), companion_root=companion, python_version=(3, 11))
            fingerprint = next(item for item in discovery.checks if item["name"] == "companion_docx_skill")["fingerprint"]
            (companion / "ooxml.md").write_text("changed guidance\n", encoding="utf-8")
            report = run_preflight(
                pandoc=str(pandoc),
                companion_root=companion,
                companion_fingerprint=fingerprint,
                require_companion=True,
                python_version=(3, 11),
            )
            check = next(item for item in report.checks if item["name"] == "companion_docx_skill")
            self.assertEqual("FAIL", check["state"])
            self.assertFalse(report.mutation_ready)
            self.assertIn("does not match", check["reason"])

    def test_incompatible_pandoc_version_fails_with_actionable_reason(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            pandoc = self._fake_pandoc(directory, version="2.19.2")
            report = run_preflight(pandoc=str(pandoc))
            check = next(item for item in report.checks if item["name"] == "pandoc")
            self.assertEqual("FAIL", check["state"])
            self.assertIn("outside the supported range", check["reason"])

    def test_incompatible_pandoc_api_fails_closed(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            pandoc = self._fake_pandoc(directory, api="[1, 21]")
            report = run_preflight(pandoc=str(pandoc))
            check = next(item for item in report.checks if item["name"] == "pandoc")
            self.assertEqual("FAIL", check["state"])
            self.assertIn("API version is outside the supported range", check["reason"])

    def test_required_companion_without_pin_and_native_word_capabilities_fail_closed(self):
        report = run_preflight(
            pandoc="definitely-not-installed-pandoc",
            require_companion=True,
            require_native_word=True,
        )
        self.assertEqual("FAIL", report.status)
        checks = {item["name"]: item for item in report.checks}
        self.assertEqual("FAIL", checks["companion_docx_skill"]["state"])
        self.assertEqual("FAIL", checks["microsoft_word"]["state"])

    def test_report_is_deterministic_json(self):
        first = run_preflight(pandoc="definitely-not-installed-pandoc").to_dict()
        second = run_preflight(pandoc="definitely-not-installed-pandoc").to_dict()
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )


if __name__ == "__main__":
    unittest.main()
