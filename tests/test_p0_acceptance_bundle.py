from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from word_formula_omml.acceptance import P0_VISUAL_RISK_FAMILIES


ROOT = Path(__file__).resolve().parents[1]


class RepresentativeP0BundleTests(unittest.TestCase):
    def test_builder_produces_structurally_audited_word_handoff(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "bundle"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_p0_acceptance_bundle.py"), str(output)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)
            bundle = json.loads((output / "p0-acceptance-bundle.json").read_text(encoding="utf-8"))
            self.assertEqual("P0_REPRESENTATIVE_ACCEPTANCE_BUNDLE", bundle["kind"])
            self.assertEqual("AWAITING_NATIVE_WORD", bundle["state"])
            self.assertEqual(
                {"raw-latex", "plain-scripts", "plain-operators", "unicode-operators", "display-omml", "interval"},
                set(bundle["selected_occurrence_ids"]),
            )
            self.assertRegex(bundle["acceptance_request_id"], r"^p0-request-[0-9a-f]{32}$")
            for required in (
                "source",
                "manifest",
                "application_plan",
                "job_structural",
                "redlined",
                "clean",
                "redlined_audit",
                "clean_audit",
                "acceptance_request",
                "word_observations",
            ):
                self.assertIn(required, bundle["files"])
                self.assertRegex(bundle["files"][required]["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue((output / bundle["files"][required]["path"]).is_file())

            request = json.loads((output / "p0-acceptance-request.json").read_text(encoding="utf-8"))
            self.assertEqual(set(P0_VISUAL_RISK_FAMILIES), set(request["coverage"]))
            self.assertEqual({"clean", "redlined"}, {item["logical_id"] for item in request["artifacts"]})
            observations = json.loads((output / "p0-word-observations.json").read_text(encoding="utf-8"))
            for artifact in observations.values():
                self.assertIsNone(artifact["open_no_repair"])
                self.assertEqual(
                    {family: "NOT_RUN" for family in P0_VISUAL_RISK_FAMILIES},
                    artifact["visual_checks"],
                )


if __name__ == "__main__":
    unittest.main()
