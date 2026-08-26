from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_p0_gate import bind_structural_reports, stage_case
from word_formula_omml.acceptance import (
    AcceptanceError,
    P0_VISUAL_RISK_FAMILIES,
    acceptance_observation_template,
    complete_representative_acceptance,
    prepare_representative_acceptance,
)
from word_formula_omml.gates import evaluate_p0_gate


class RepresentativeP0AcceptanceTests(unittest.TestCase):
    def _coverage(self) -> dict:
        return {
            family: {
                "occurrence_ids": ["safe"],
                "rationale": f"synthetic test occurrence exercises {family}",
            }
            for family in P0_VISUAL_RISK_FAMILIES
        }

    def _prepared(self, directory: Path):
        source, manifest, plan, staged, paths = stage_case(directory)
        job = bind_structural_reports(source, manifest, plan, staged, paths)
        request = prepare_representative_acceptance(job, paths, self._coverage())
        return job, paths, request

    def _passing_observations(self, request: dict) -> dict:
        observations = acceptance_observation_template(request)
        for value in observations.values():
            value.update(
                {
                    "open_no_repair": True,
                    "word_version": "16.0-test",
                    "validation_mode": "manual",
                    "environment": {"os": "Windows 11 fixture", "word_build": "16.0-test"},
                    "recorded_at": "2026-08-26T14:00:00-07:00",
                    "visual_checks": {family: "PASS" for family in P0_VISUAL_RISK_FAMILIES},
                    "notes": "synthetic policy test; not production acceptance evidence",
                }
            )
        return observations

    def test_prepare_requires_every_visual_risk_family(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source, manifest, plan, staged, paths = stage_case(directory)
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            coverage = self._coverage()
            del coverage["display"]
            with self.assertRaisesRegex(AcceptanceError, "visual coverage families mismatch"):
                prepare_representative_acceptance(job, paths, coverage)

    def test_prepare_requires_structural_audit_before_word_handoff(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            with self.assertRaisesRegex(AcceptanceError, "W6 evidence"):
                prepare_representative_acceptance(staged.job, paths, self._coverage())

    def test_incomplete_visual_observation_cannot_issue_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            job, paths, request = self._prepared(Path(name))
            observations = self._passing_observations(request)
            observations["clean"]["visual_checks"]["fraction"] = "FAIL"
            with self.assertRaisesRegex(AcceptanceError, "non-PASS"):
                complete_representative_acceptance(job, paths, request, observations)

    def test_candidate_mutation_invalidates_acceptance_request(self):
        with tempfile.TemporaryDirectory() as name:
            job, paths, request = self._prepared(Path(name))
            observations = self._passing_observations(request)
            paths["clean"].write_bytes(paths["clean"].read_bytes() + b"mutation")
            with self.assertRaisesRegex(AcceptanceError, "candidate hash"):
                complete_representative_acceptance(job, paths, request, observations)

    def test_complete_receipt_binds_native_evidence_and_reaches_p0_complete(self):
        with tempfile.TemporaryDirectory() as name:
            job, paths, request = self._prepared(Path(name))
            validated, receipt = complete_representative_acceptance(
                job,
                paths,
                request,
                self._passing_observations(request),
            )
            report = evaluate_p0_gate(validated)
            self.assertEqual("PASS", report["state"])
            self.assertEqual("COMPLETE", report["delivery_status"])
            self.assertEqual("PASS", receipt["state"])
            self.assertRegex(receipt["acceptance_receipt_id"], r"^p0-receipt-[0-9a-f]{32}$")
            self.assertEqual(request["acceptance_request_id"], receipt["acceptance_request_id"])
            self.assertEqual(set(P0_VISUAL_RISK_FAMILIES), set(receipt["coverage"]))


if __name__ == "__main__":
    unittest.main()
