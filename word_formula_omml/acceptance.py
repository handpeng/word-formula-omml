"""Representative P0 native-Word acceptance handoff and receipt policy.

The generic NATIVE_WORD gate remains reusable for ordinary candidate evidence.
This module adds the stricter repository-level evidence required to close W7:
a reviewed redlined+clean pair, candidate-bound W6 evidence, explicit visual
risk-family coverage, and real Microsoft Word observations for the exact bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from word_formula_omml.applicator import validate_staged_artifacts
from word_formula_omml.contract import (
    ArtifactState,
    FrozenJob,
    Gate,
    GateState,
    SUCCESSFUL_OCCURRENCE_STATUSES,
    load_job,
)
from word_formula_omml.gates import (
    NATIVE_WORD_VALIDATION_MODES,
    P0_VISUAL_RISK_FAMILIES,
    GatePolicyError,
    bind_native_word_evidence,
    evaluate_p0_gate,
    normalize_recorded_at,
)


P0_ACCEPTANCE_SCHEMA_VERSION = 1
P0_ACCEPTANCE_GATE = "P0_REPRESENTATIVE_ACCEPTANCE"
P0_REPRESENTATIVE_ARTIFACTS = frozenset({"redlined", "clean"})


class AcceptanceError(GatePolicyError):
    """Raised when representative P0 acceptance evidence is incomplete or stale."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AcceptanceError(f"value is not deterministic JSON: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _coerce_job(value: FrozenJob | Mapping[str, Any] | str | Path) -> FrozenJob:
    if isinstance(value, FrozenJob):
        return load_job(value.to_dict())
    return load_job(value)


def _candidate_records(job: FrozenJob, candidates: Mapping[str, str | Path]) -> list[dict[str, Any]]:
    if not isinstance(candidates, Mapping):
        raise AcceptanceError("candidate paths must be an object keyed by artifact logical ID")
    logical_ids = {artifact["logical_id"] for artifact in job.artifacts}
    if logical_ids != P0_REPRESENTATIVE_ARTIFACTS:
        raise AcceptanceError(
            "representative P0 acceptance requires the frozen redlined+clean artifact pair"
        )
    if set(candidates) != logical_ids:
        raise AcceptanceError("candidate path set does not match the frozen representative artifact pair")
    records: list[dict[str, Any]] = []
    for artifact in sorted(job.artifacts, key=lambda item: item["logical_id"]):
        logical_id = artifact["logical_id"]
        if artifact.get("state") != ArtifactState.STAGING.value:
            raise AcceptanceError(
                f"artifact {logical_id!r} must remain STAGING before native Word acceptance"
            )
        structural = artifact.get("gates", {}).get(Gate.STRUCTURAL_AUDIT.value, {})
        if structural.get("state") != GateState.PASS.value:
            raise AcceptanceError(
                f"artifact {logical_id!r} requires passing candidate-bound W6 evidence before Word handoff"
            )
        expected = artifact.get("content_sha256")
        if not isinstance(expected, str):
            raise AcceptanceError(f"artifact {logical_id!r} has no frozen candidate SHA-256")
        path = Path(candidates[logical_id])
        if path.is_symlink() or not path.is_file():
            raise AcceptanceError(f"candidate for {logical_id!r} must be a regular non-symlink file")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise AcceptanceError(f"cannot read candidate {path}: {error}") from error
        if actual != expected:
            raise AcceptanceError(f"candidate hash for {logical_id!r} does not match the frozen artifact")
        records.append(
            {
                "artifact_id": artifact["id"],
                "logical_id": logical_id,
                "kind": artifact["kind"],
                "candidate_sha256": actual,
            }
        )
    return records


def _coverage(job: FrozenJob, coverage: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(coverage, Mapping):
        raise AcceptanceError("visual coverage must be an object")
    required = set(P0_VISUAL_RISK_FAMILIES)
    supplied = set(coverage)
    if supplied != required:
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        raise AcceptanceError(f"visual coverage families mismatch; missing={missing}, extra={extra}")
    occurrences = {item.get("id"): item for item in job.occurrences}
    normalized: dict[str, Any] = {}
    for family in P0_VISUAL_RISK_FAMILIES:
        record = coverage[family]
        if not isinstance(record, Mapping) or set(record) != {"occurrence_ids", "rationale"}:
            raise AcceptanceError(
                f"coverage {family!r} must contain only occurrence_ids and rationale"
            )
        occurrence_ids = record.get("occurrence_ids")
        rationale = record.get("rationale")
        if not isinstance(occurrence_ids, list) or not occurrence_ids:
            raise AcceptanceError(f"coverage {family!r} requires a non-empty occurrence_ids array")
        if any(not isinstance(value, str) or not value for value in occurrence_ids):
            raise AcceptanceError(f"coverage {family!r} occurrence IDs must be non-empty strings")
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise AcceptanceError(f"coverage {family!r} contains duplicate occurrence IDs")
        if not isinstance(rationale, str) or not rationale.strip():
            raise AcceptanceError(f"coverage {family!r} requires a reviewable rationale")
        for occurrence_id in occurrence_ids:
            occurrence = occurrences.get(occurrence_id)
            if occurrence is None:
                raise AcceptanceError(
                    f"coverage {family!r} references unknown occurrence {occurrence_id!r}"
                )
            if occurrence.get("status") not in SUCCESSFUL_OCCURRENCE_STATUSES:
                raise AcceptanceError(
                    f"coverage {family!r} references non-successful occurrence {occurrence_id!r}"
                )
        normalized[family] = {
            "occurrence_ids": list(occurrence_ids),
            "rationale": rationale.strip(),
        }
    return normalized


def prepare_representative_acceptance(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    candidates: Mapping[str, str | Path],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the exact candidates and risk-family coverage for controlled Word review."""

    current = _coerce_job(job)
    if not isinstance(current.application_plan_sha256, str):
        raise AcceptanceError("representative acceptance requires a frozen application plan")
    artifacts = _candidate_records(current, candidates)
    normalized_coverage = _coverage(current, coverage)
    payload = {
        "schema_version": P0_ACCEPTANCE_SCHEMA_VERSION,
        "gate": P0_ACCEPTANCE_GATE,
        "job_id": current.job_id,
        "source_sha256": current.source_sha256,
        "manifest_id": current.manifest_id,
        "manifest_sha256": current.manifest_sha256,
        "application_plan_sha256": current.application_plan_sha256,
        "artifacts": artifacts,
        "required_visual_families": list(P0_VISUAL_RISK_FAMILIES),
        "coverage": normalized_coverage,
    }
    return {
        **payload,
        "acceptance_request_id": "p0-request-" + _digest(payload)[:32],
    }


def acceptance_observation_template(request: Mapping[str, Any]) -> dict[str, Any]:
    """Create a fill-in observation file without inventing a Word result."""

    if not isinstance(request, Mapping) or request.get("gate") != P0_ACCEPTANCE_GATE:
        raise AcceptanceError("acceptance request is not a P0 representative request")
    artifacts = request.get("artifacts")
    if not isinstance(artifacts, list):
        raise AcceptanceError("acceptance request has no artifact list")
    result: dict[str, Any] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise AcceptanceError("acceptance request artifact record is invalid")
        logical_id = artifact.get("logical_id")
        candidate_sha256 = artifact.get("candidate_sha256")
        if not isinstance(logical_id, str) or not isinstance(candidate_sha256, str):
            raise AcceptanceError("acceptance request artifact identity is invalid")
        result[logical_id] = {
            "candidate_sha256": candidate_sha256,
            "open_no_repair": None,
            "word_version": "",
            "validation_mode": "manual",
            "environment": {},
            "recorded_at": "",
            "visual_checks": {family: "NOT_RUN" for family in P0_VISUAL_RISK_FAMILIES},
            "notes": "",
        }
    return result


def _recorded_at(value: Any) -> str:
    try:
        return normalize_recorded_at(value)
    except GatePolicyError as error:
        raise AcceptanceError(str(error)) from error


def _observations(request: Mapping[str, Any], observations: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observations, Mapping):
        raise AcceptanceError("native Word observations must be an object keyed by artifact logical ID")
    artifacts = request["artifacts"]
    expected = {item["logical_id"]: item for item in artifacts}
    if set(observations) != set(expected):
        raise AcceptanceError("native Word observation set does not match the frozen artifact pair")
    normalized: dict[str, Any] = {}
    for logical_id in sorted(expected):
        value = observations[logical_id]
        if not isinstance(value, Mapping):
            raise AcceptanceError(f"observation for {logical_id!r} must be an object")
        if value.get("candidate_sha256") != expected[logical_id]["candidate_sha256"]:
            raise AcceptanceError(f"observation for {logical_id!r} is bound to the wrong candidate hash")
        if value.get("open_no_repair") is not True:
            raise AcceptanceError(f"observation for {logical_id!r} does not prove open-without-repair")
        word_version = value.get("word_version")
        if not isinstance(word_version, str) or not word_version.strip():
            raise AcceptanceError(f"observation for {logical_id!r} requires Microsoft Word version")
        validation_mode = value.get("validation_mode")
        if validation_mode not in NATIVE_WORD_VALIDATION_MODES:
            raise AcceptanceError(f"observation for {logical_id!r} has unsupported validation mode")
        environment = value.get("environment")
        if not isinstance(environment, Mapping) or not environment:
            raise AcceptanceError(f"observation for {logical_id!r} requires a non-empty environment")
        _canonical_json(environment)
        visual_checks = value.get("visual_checks")
        if not isinstance(visual_checks, Mapping) or set(visual_checks) != set(P0_VISUAL_RISK_FAMILIES):
            raise AcceptanceError(f"observation for {logical_id!r} has incomplete visual risk-family checks")
        failed = [family for family in P0_VISUAL_RISK_FAMILIES if visual_checks.get(family) != GateState.PASS.value]
        if failed:
            raise AcceptanceError(
                f"observation for {logical_id!r} cannot pass representative acceptance; non-PASS={failed}"
            )
        notes = value.get("notes", "")
        if not isinstance(notes, str):
            raise AcceptanceError(f"observation notes for {logical_id!r} must be a string")
        normalized[logical_id] = {
            "candidate_sha256": expected[logical_id]["candidate_sha256"],
            "open_no_repair": True,
            "word_version": word_version.strip(),
            "validation_mode": validation_mode,
            "environment": copy.deepcopy(dict(environment)),
            "recorded_at": _recorded_at(value.get("recorded_at")),
            "visual_checks": {family: GateState.PASS.value for family in P0_VISUAL_RISK_FAMILIES},
            "notes": notes,
        }
    return normalized


def complete_representative_acceptance(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    candidates: Mapping[str, str | Path],
    request: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> tuple[FrozenJob, dict[str, Any]]:
    """Bind real Word observations, validate candidates, and issue the W7 receipt."""

    current = _coerce_job(job)
    if not isinstance(request, Mapping):
        raise AcceptanceError("acceptance request must be an object")
    coverage = request.get("coverage")
    expected_request = prepare_representative_acceptance(current, candidates, coverage)
    if dict(request) != expected_request:
        raise AcceptanceError("acceptance request is stale, tampered, or does not match the current job/candidates")
    normalized = _observations(expected_request, observations)
    updated = current
    for logical_id in sorted(normalized):
        observation = normalized[logical_id]
        updated = bind_native_word_evidence(
            updated,
            logical_id,
            candidates[logical_id],
            state=GateState.PASS.value,
            open_no_repair=True,
            visual_inspection=GateState.PASS.value,
            word_version=observation["word_version"],
            validation_mode=observation["validation_mode"],
            environment=observation["environment"],
            recorded_at=observation["recorded_at"],
            details={
                "representative_acceptance": True,
                "acceptance_request_id": expected_request["acceptance_request_id"],
                "visual_checks": observation["visual_checks"],
                "notes": observation["notes"],
            },
        )
    validated = validate_staged_artifacts(updated, candidates)
    gate_report = evaluate_p0_gate(validated)
    if gate_report.get("state") != GateState.PASS.value or gate_report.get("delivery_status") != "COMPLETE":
        raise AcceptanceError("representative Word observations did not produce a COMPLETE passing P0 job")
    receipt_payload = {
        "schema_version": P0_ACCEPTANCE_SCHEMA_VERSION,
        "gate": P0_ACCEPTANCE_GATE,
        "state": GateState.PASS.value,
        "acceptance_request_id": expected_request["acceptance_request_id"],
        "job_id": validated.job_id,
        "source_sha256": validated.source_sha256,
        "manifest_id": validated.manifest_id,
        "manifest_sha256": validated.manifest_sha256,
        "application_plan_sha256": validated.application_plan_sha256,
        "coverage": copy.deepcopy(expected_request["coverage"]),
        "artifacts": [
            {
                "logical_id": item["logical_id"],
                **copy.deepcopy(normalized[item["logical_id"]]),
            }
            for item in expected_request["artifacts"]
        ],
        "p0_gate": {
            "state": gate_report["state"],
            "delivery_status": gate_report["delivery_status"],
        },
    }
    receipt = {
        **receipt_payload,
        "acceptance_receipt_id": "p0-receipt-" + _digest(receipt_payload)[:32],
    }
    return validated, receipt


__all__ = [
    "AcceptanceError",
    "P0_ACCEPTANCE_GATE",
    "P0_ACCEPTANCE_SCHEMA_VERSION",
    "P0_REPRESENTATIVE_ARTIFACTS",
    "P0_VISUAL_RISK_FAMILIES",
    "acceptance_observation_template",
    "complete_representative_acceptance",
    "prepare_representative_acceptance",
]
