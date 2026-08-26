from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from word_formula_omml.contract import (
    ContractError,
    DeliveryStatus,
    GateState,
    OccurrenceStatus,
    bind_gate_evidence,
    derive_job_status,
    deterministic_occurrence_id,
    dump_job,
    dump_manifest,
    freeze_job,
    load_job,
    load_manifest,
    set_artifact_content,
    set_occurrence_status,
    verify_frozen_job,
)


SOURCE_SHA = "a" * 64
OTHER_SOURCE_SHA = "b" * 64


def minimal_manifest(source_sha256: str | None = SOURCE_SHA) -> dict:
    result = {
        "formulas": [
            {"id": "F-001", "latex": "x_i^2", "layout": "inline"},
            {"id": "F-002", "latex": "x_{i^2}", "layout": "display"},
        ]
    }
    if source_sha256 is not None:
        result["source_sha256"] = source_sha256
    return result


def make_complete_job():
    manifest = load_manifest(minimal_manifest())
    job = freeze_job(
        manifest,
        SOURCE_SHA,
        [
            {"id": "redlined", "kind": "redlined", "required_gates": ["STRUCTURAL_AUDIT", "NATIVE_WORD"]},
            {"id": "clean", "kind": "clean", "required_gates": ["STRUCTURAL_AUDIT", "NATIVE_WORD"]},
        ],
    )
    for occurrence in manifest.formulas:
        job = set_occurrence_status(job, occurrence["id"], OccurrenceStatus.APPLIED.value)
    for artifact in job.artifacts:
        job = set_artifact_content(job, artifact["id"], "c" * 64)
        for gate in artifact["required_gates"]:
            job = bind_gate_evidence(
                job,
                artifact["id"],
                gate,
                GateState.PASS.value,
                tool="test",
                tool_version="1",
            )
    return manifest, job


class ManifestContractTests(unittest.TestCase):
    def test_legacy_array_and_object_load_without_schema_version(self):
        array = [{"id": "F-001", "latex": "x_i", "layout": "inline"}]
        manifest_from_array = load_manifest(array)
        manifest_from_object = load_manifest({"formulas": array})

        self.assertEqual(manifest_from_array.to_dict(), manifest_from_object.to_dict())
        self.assertEqual(manifest_from_array.formulas[0]["status"], "DISCOVERED")
        self.assertEqual(manifest_from_array.formulas[0]["confidence"], "REVIEW_REQUIRED")

    def test_round_trip_is_deterministic(self):
        first = load_manifest(minimal_manifest())
        second = load_manifest(first.to_dict())

        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertEqual(first.manifest_sha256, second.manifest_sha256)
        self.assertEqual(
            json.dumps(first.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(second.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            dump_manifest(first, path)
            self.assertEqual(first.to_dict(), load_manifest(path).to_dict())

    def test_minimal_generator_loader_uses_shared_contract(self):
        from scripts.generate_omml_library import load_formulas

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps([{"id": "F-001", "latex": "x_i"}]), encoding="utf-8")
            self.assertEqual(load_formulas(path), [{"id": "F-001", "latex": "x_i", "layout": "inline"}])

    def test_invalid_values_and_unknown_fields_fail_closed(self):
        with self.assertRaisesRegex(ContractError, "duplicate ids"):
            load_manifest({"formulas": [{"id": "F-001", "latex": "x"}, {"id": "F-001", "latex": "y"}]})
        with self.assertRaisesRegex(ContractError, "unsupported value"):
            load_manifest({"formulas": [{"id": "F-001", "latex": "x", "layout": "block"}]})
        with self.assertRaisesRegex(ContractError, "unsupported fields"):
            load_manifest({"formulas": [{"id": "F-001", "latex": "x", "future_field": 1}]})
        with self.assertRaisesRegex(ContractError, "unsupported manifest.schema_version"):
            load_manifest({"schema_version": 99, "formulas": []})

    def test_stable_ids_are_unchanged_for_unchanged_input(self):
        first = load_manifest(minimal_manifest())
        second = load_manifest(copy.deepcopy(minimal_manifest()))
        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertEqual(
            freeze_job(first, SOURCE_SHA, ["clean"]).job_id,
            freeze_job(second, SOURCE_SHA, ["clean"]).job_id,
        )

    def test_occurrence_id_derivation_is_anchor_and_source_bound(self):
        row = {
            "package_part": "word/document.xml",
            "story": "main",
            "paragraph": 4,
            "sequence_in_paragraph": 2,
            "anchor_before": "loss = ",
            "raw_source": "x_i^2",
            "anchor_after": ".",
            "run_index": 1,
            "run_start": 7,
            "run_end": 12,
        }
        first = deterministic_occurrence_id(row, source_sha256=SOURCE_SHA, position=1)
        second = deterministic_occurrence_id(copy.deepcopy(row), source_sha256=SOURCE_SHA, position=1)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first,
            deterministic_occurrence_id(row, source_sha256=OTHER_SOURCE_SHA, position=1),
        )
        self.assertNotEqual(first, deterministic_occurrence_id(row, source_sha256=SOURCE_SHA, position=2))
        with self.assertRaisesRegex(ContractError, "stable paragraph/anchor/run location"):
            deterministic_occurrence_id({"raw_source": "x"}, source_sha256=SOURCE_SHA)

    def test_freeze_does_not_accept_output_status_from_input_manifest(self):
        manifest_data = minimal_manifest()
        manifest_data["formulas"][0]["status"] = OccurrenceStatus.APPLIED.value
        manifest = load_manifest(manifest_data)
        with self.assertRaisesRegex(ContractError, "output lifecycle status"):
            freeze_job(manifest, SOURCE_SHA, ["clean"])

    def test_freeze_rejects_manifest_source_conflict(self):
        manifest = load_manifest(minimal_manifest(OTHER_SOURCE_SHA))
        with self.assertRaisesRegex(ContractError, "does not match the source"):
            freeze_job(manifest, SOURCE_SHA, ["clean"])

    def test_frozen_job_rejects_changed_source_or_requested_set(self):
        manifest = load_manifest(minimal_manifest())
        job = freeze_job(manifest, SOURCE_SHA, ["clean"])
        with self.assertRaisesRegex(ContractError, "source_sha256"):
            verify_frozen_job(job, manifest, OTHER_SOURCE_SHA, ["clean"])
        with self.assertRaisesRegex(ContractError, "artifact set changed"):
            verify_frozen_job(job, manifest, SOURCE_SHA, ["redlined"])

    def test_complete_is_derived_and_requires_every_artifact_gate(self):
        manifest, job = make_complete_job()
        self.assertEqual(derive_job_status(job), DeliveryStatus.COMPLETE.value)
        serialized = job.to_dict()
        serialized["status"] = DeliveryStatus.COMPLETE.value
        clean = next(item for item in serialized["artifacts"] if item["logical_id"] == "clean")
        clean["gates"]["NATIVE_WORD"] = {"state": GateState.NOT_RUN.value}
        with self.assertRaisesRegex(ContractError, "does not match derived completion"):
            load_job(serialized)
        self.assertEqual(
            derive_job_status(
                load_job({**serialized, "status": DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value})
            ),
            DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value,
        )
        self.assertEqual(manifest.manifest_id, job.manifest_id)

    def test_job_round_trip_preserves_derived_status(self):
        _manifest, job = make_complete_job()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            dump_job(job, path)
            restored = load_job(path)
            self.assertEqual(restored.to_dict(), job.to_dict())
            self.assertEqual(restored.status, DeliveryStatus.COMPLETE.value)

    def test_partial_and_failed_statuses_are_distinct(self):
        manifest = load_manifest(minimal_manifest())
        job = freeze_job(manifest, SOURCE_SHA, ["clean"])
        self.assertEqual(derive_job_status(job), DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value)
        job = set_occurrence_status(job, "F-001", OccurrenceStatus.FAILED.value)
        self.assertEqual(derive_job_status(job), DeliveryStatus.FAILED.value)

    def test_candidate_mutation_stales_existing_gate_evidence(self):
        _manifest, job = make_complete_job()
        artifact = job.artifacts[0]
        changed = set_artifact_content(job, artifact["id"], "d" * 64)
        self.assertEqual(changed.artifacts[0]["gates"]["STRUCTURAL_AUDIT"]["state"], GateState.STALE.value)
        self.assertEqual(derive_job_status(changed), DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value)

        tampered = job.to_dict()
        tampered["artifacts"][0]["content_sha256"] = "d" * 64
        with self.assertRaisesRegex(ContractError, "artifact_sha256"):
            load_job(tampered)

    def test_evidence_cannot_cross_use_artifacts(self):
        _manifest, job = make_complete_job()
        serialized = job.to_dict()
        artifacts = {item["logical_id"]: item for item in serialized["artifacts"]}
        redlined = artifacts["redlined"]
        clean = artifacts["clean"]
        clean["gates"]["NATIVE_WORD"]["evidence"] = redlined["gates"]["NATIVE_WORD"]["evidence"]
        clean["gates"]["NATIVE_WORD"]["state"] = GateState.PASS.value
        serialized["status"] = DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value
        with self.assertRaisesRegex(ContractError, "artifact_id"):
            load_job(serialized)

    def test_excluded_occurrence_requires_explicit_approval(self):
        with self.assertRaisesRegex(ContractError, "approved must be true"):
            load_manifest(
                {
                    "formulas": [
                        {
                            "id": "F-001",
                            "latex": "x",
                            "status": OccurrenceStatus.EXCLUDED.value,
                            "exclusion": {"approved": False, "reason": "not in scope"},
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
