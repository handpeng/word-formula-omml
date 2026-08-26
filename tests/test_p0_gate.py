from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.test_audit import load_audit, semantic_template
from tests.test_applicator import make_paragraph, make_row, write_case
from word_formula_omml.applicator import (
    ApplicationError,
    bind_staged_artifacts,
    prepare_application,
    stage_application,
    validate_staged_artifacts,
)
from word_formula_omml.gates import (
    GatePolicyError,
    assert_p0_ready,
    bind_native_word_evidence,
    bind_structural_audit_evidence,
    evaluate_p0_gate,
    finalize_p0_artifact_set,
)
from word_formula_omml.canonical import canonicalize_formula


def stage_case(directory: Path, *, include_refusal: bool = False):
    if include_refusal:
        texts = ["safe x_i", "unsafe x_i"]
        rows = [
            make_row(
                "safe",
                texts[0],
                1,
                style={
                    "math_font": "Cambria Math",
                    "math_font_policy": "CAMBRIA_MATH",
                    "color": "0000FF",
                    "size": "24",
                },
            ),
            make_row("unsafe", texts[1], 2, run_count=2),
        ]
        rows[0]["canonical"] = canonicalize_formula("x_i")
        source, manifest, templates = write_case(directory, [make_paragraph(text) for text in texts], rows)
        templates["safe"] = semantic_template()
    else:
        text = "before x_i after"
        row = make_row(
            "safe",
            text,
            1,
            style={
                "math_font": "Cambria Math",
                "math_font_policy": "CAMBRIA_MATH",
                "color": "0000FF",
                "size": "24",
            },
        )
        row["canonical"] = canonicalize_formula("x_i")
        source, manifest, templates = write_case(directory, [make_paragraph(text)], [row])
        templates["safe"] = semantic_template()
    job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
    paths = {"redlined": directory / "stage-redlined.docx", "clean": directory / "stage-clean.docx"}
    staged = stage_application(source, manifest, job, plan, templates, paths)
    return source, manifest, plan, staged, paths


def bind_structural_reports(source, manifest, plan, staged, paths):
    audit = load_audit()
    job = staged.job
    for logical_id in ("redlined", "clean"):
        report = audit.audit_artifact(
            paths[logical_id],
            baseline=source,
            manifest=manifest,
            application_plan=plan,
            job=job,
            artifact_id=logical_id,
            artifact_kind=logical_id,
        )
        if report["status"] != "PASS":
            raise AssertionError(report["errors"])
        job = bind_structural_audit_evidence(job, logical_id, paths[logical_id], report)
    return job


def bind_native_pass(job, paths):
    for logical_id in ("redlined", "clean"):
        job = bind_native_word_evidence(
            job,
            logical_id,
            paths[logical_id],
            state="PASS",
            open_no_repair=True,
            visual_inspection="PASS",
            word_version="16.0-test",
            validation_mode="manual",
            environment={"platform": "fixture-test", "runner": "controlled"},
            details={"fixture": True},
        )
    return job


class P0GateTests(unittest.TestCase):
    def test_missing_native_word_evidence_blocks_p0_and_finalization(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            report = evaluate_p0_gate(job)
            self.assertEqual("NOT_RUN", report["state"])
            self.assertTrue(any("NATIVE_WORD" in item for item in report["pending"]))
            with self.assertRaisesRegex(GatePolicyError, "P0 production-pilot gate"):
                assert_p0_ready(job)
            with self.assertRaisesRegex(ApplicationError, "cannot be VALIDATED"):
                validate_staged_artifacts(job, paths)

    def test_native_evidence_is_candidate_bound_and_observations_are_consistent(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            with self.assertRaisesRegex(GatePolicyError, "candidate hash"):
                bind_native_word_evidence(
                    job,
                    "redlined",
                    paths["clean"],
                    state="PASS",
                    open_no_repair=True,
                    visual_inspection="PASS",
                    word_version="16.0-test",
                    environment={"platform": "fixture-test"},
                )
            with self.assertRaisesRegex(GatePolicyError, "cannot PASS"):
                bind_native_word_evidence(
                    job,
                    "redlined",
                    paths["redlined"],
                    state="PASS",
                    open_no_repair=False,
                    visual_inspection="PASS",
                    word_version="16.0-test",
                    environment={"platform": "fixture-test"},
                )

    def test_structural_binding_requires_a_complete_w6_report(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            audit = load_audit()
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            with self.assertRaisesRegex(GatePolicyError, "complete W6 audit report"):
                bind_structural_audit_evidence(
                    staged.job,
                    "redlined",
                    paths["redlined"],
                    report["evidence"],
                )

            report["audit"]["delivery_status"] = "COMPLETE"
            with self.assertRaisesRegex(GatePolicyError, "must not claim overall delivery completion"):
                bind_structural_audit_evidence(
                    staged.job,
                    "redlined",
                    paths["redlined"],
                    report,
                )

            report["audit"]["delivery_status"] = "NOT_DERIVED"
            report["audit"]["schema_version"] = 99
            with self.assertRaisesRegex(GatePolicyError, "unsupported structural audit report schema_version"):
                bind_structural_audit_evidence(
                    staged.job,
                    "redlined",
                    paths["redlined"],
                    report,
                )

    def test_native_fail_and_not_run_states_remain_non_complete(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            job = bind_native_word_evidence(
                job,
                "redlined",
                paths["redlined"],
                state="FAIL",
                open_no_repair=False,
                visual_inspection="PASS",
                word_version="16.0-test",
                environment={"platform": "fixture-test"},
                reason="fixture repair prompt",
            )
            self.assertEqual("FAIL", evaluate_p0_gate(job)["state"])

            job = bind_native_word_evidence(
                job,
                "clean",
                paths["clean"],
                state="NOT_RUN",
                reason="Microsoft Word unavailable in this test environment",
            )
            clean = next(item for item in job.artifacts if item["logical_id"] == "clean")
            self.assertEqual("NOT_RUN", clean["gates"]["NATIVE_WORD"]["state"])
            self.assertNotIn("evidence", clean["gates"]["NATIVE_WORD"])
            self.assertNotEqual("PASS", evaluate_p0_gate(job)["state"])

    def test_full_candidate_bound_p0_sequence_allows_atomic_finalization(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source, manifest, plan, staged, paths = stage_case(directory)
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            job = bind_native_pass(job, paths)
            self.assertEqual("NOT_RUN", evaluate_p0_gate(job)["state"])
            validated = validate_staged_artifacts(job, paths)
            self.assertEqual("PASS", evaluate_p0_gate(validated)["state"])
            finals = {"redlined": directory / "final-redlined.docx", "clean": directory / "final-clean.docx"}
            finalized = finalize_p0_artifact_set(validated, paths, finals, source=source)
            self.assertEqual("COMPLETE", finalized.status)
            self.assertTrue(all(path.is_file() for path in finals.values()))
            self.assertTrue(all(not path.exists() for path in paths.values()))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), staged.source_sha256)

    def test_partial_occurrence_batch_cannot_become_p0_complete(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name), include_refusal=True)
            job = bind_structural_reports(source, manifest, plan, staged, paths)
            job = bind_native_pass(job, paths)
            validated = validate_staged_artifacts(job, paths)
            report = evaluate_p0_gate(validated)
            self.assertEqual("NOT_RUN", report["state"])
            self.assertEqual("PARTIAL_REVIEW_OUTPUT", report["delivery_status"])
            with self.assertRaisesRegex(GatePolicyError, "P0 production-pilot gate"):
                assert_p0_ready(validated)

    def test_candidate_mutation_stales_audit_and_native_evidence_before_finalization(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            job = bind_native_pass(bind_structural_reports(source, manifest, plan, staged, paths), paths)
            paths["clean"].write_bytes(paths["clean"].read_bytes() + b"mutation")
            stale = bind_staged_artifacts(job, paths)
            clean = next(item for item in stale.artifacts if item["logical_id"] == "clean")
            self.assertEqual("STALE", clean["gates"]["STRUCTURAL_AUDIT"]["state"])
            self.assertEqual("STALE", clean["gates"]["NATIVE_WORD"]["state"])
            self.assertNotEqual("PASS", evaluate_p0_gate(stale)["state"])
            with self.assertRaisesRegex(GatePolicyError, "P0 production-pilot gate"):
                assert_p0_ready(stale)

    def test_serialized_gate_evidence_tampering_is_rejected_by_p0_policy(self):
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = stage_case(Path(name))
            job = bind_native_pass(bind_structural_reports(source, manifest, plan, staged, paths), paths)
            serialized = job.to_dict()
            clean = next(item for item in serialized["artifacts"] if item["logical_id"] == "clean")
            clean["gates"]["NATIVE_WORD"]["evidence"]["details"]["open_no_repair"] = False
            self.assertEqual("FAIL", evaluate_p0_gate(serialized)["state"])

            serialized = job.to_dict()
            redlined = next(item for item in serialized["artifacts"] if item["logical_id"] == "redlined")
            audit_evidence = redlined["gates"]["STRUCTURAL_AUDIT"]["evidence"]["details"]["audit_evidence"]
            audit_evidence["artifact_sha256"] = "0" * 64
            self.assertEqual("FAIL", evaluate_p0_gate(serialized)["state"])

            serialized = job.to_dict()
            clean = next(item for item in serialized["artifacts"] if item["logical_id"] == "clean")
            clean["gates"]["STRUCTURAL_AUDIT"]["evidence"]["tool"] = "forged-audit-tool"
            self.assertEqual("FAIL", evaluate_p0_gate(serialized)["state"])


if __name__ == "__main__":
    unittest.main()
