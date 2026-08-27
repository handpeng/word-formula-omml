"""P0 gate policy built on the shared lifecycle and audit contracts.

This module records evidence; it does not invent a second completion state.
The job status remains derived by :mod:`word_formula_omml.contract`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from word_formula_omml.contract import (
    ArtifactState,
    ContractError,
    DeliveryStatus,
    FrozenJob,
    Gate,
    GateState,
    SUCCESSFUL_OCCURRENCE_STATUSES,
    bind_gate_evidence,
    load_job,
)


P0_REQUIRED_GATES = frozenset({Gate.STRUCTURAL_AUDIT.value, Gate.NATIVE_WORD.value})
P0_GATE_SCHEMA_VERSION = 1
NATIVE_WORD_VALIDATION_MODES = frozenset({"manual", "automated"})
VISUAL_INSPECTION_STATES = frozenset({GateState.PASS.value, GateState.FAIL.value})
P0_VISUAL_RISK_FAMILIES = (
    "inline",
    "display",
    "subscript",
    "superscript",
    "fraction",
    "inequality",
    "greek",
    "interval",
    "scientific_notation",
)
P0_ACCEPTANCE_REQUEST_ID_RE = re.compile(r"^p0-request-[0-9a-f]{32}$")


class GatePolicyError(ContractError):
    """Raised when an evidence or P0 finalization policy is not satisfied."""


def normalize_recorded_at(value: Any) -> str:
    """Require a timezone-qualified ISO-8601 evidence timestamp."""

    if not isinstance(value, str) or not value.strip():
        raise GatePolicyError("native Word evidence requires recorded_at")
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise GatePolicyError("recorded_at must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise GatePolicyError("recorded_at must include an explicit timezone")
    return normalized


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GatePolicyError(f"value is not deterministic JSON: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _coerce_job(value: FrozenJob | Mapping[str, Any] | str | Path) -> FrozenJob:
    if isinstance(value, FrozenJob):
        return load_job(value.to_dict())
    return load_job(value)


def _artifact(job: FrozenJob, artifact_id: str) -> dict[str, Any]:
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise GatePolicyError("artifact_id must be a non-empty string")
    matches = [
        dict(item)
        for item in job.artifacts
        if item["id"] == artifact_id or item["logical_id"] == artifact_id
    ]
    if len(matches) != 1:
        raise GatePolicyError(f"artifact ID {artifact_id!r} does not identify exactly one frozen artifact")
    return matches[0]


def _candidate_bytes(candidate: bytes | str | Path) -> bytes:
    if isinstance(candidate, bytes):
        return candidate
    if not isinstance(candidate, (str, Path)):
        raise GatePolicyError("candidate must be bytes or a regular-file path")
    path = Path(candidate)
    if not path.is_file():
        raise GatePolicyError(f"candidate must be a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise GatePolicyError(f"cannot read candidate {path}: {error}") from error


def _candidate_hash(artifact: Mapping[str, Any], candidate: bytes | str | Path) -> str:
    expected = artifact.get("content_sha256")
    if not isinstance(expected, str):
        raise GatePolicyError(f"artifact {artifact['logical_id']!r} has no staged content hash")
    actual = hashlib.sha256(_candidate_bytes(candidate)).hexdigest()
    if actual != expected:
        raise GatePolicyError(
            f"candidate hash does not match the frozen artifact {artifact['logical_id']!r}"
        )
    return actual


def _stable_evidence_id(prefix: str, value: Any) -> str:
    return f"evidence-{prefix}-{_digest(value)[:32]}"


def _audit_evidence_value(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GatePolicyError("structural audit input must be a report or evidence object")
    if "evidence" not in value:
        raise GatePolicyError("structural audit binding requires the complete W6 audit report")
    if value.get("status") != GateState.PASS.value or value.get("evidence_state") != GateState.PASS.value:
        raise GatePolicyError("structural audit report is not a passing candidate-bound report")
    audit = value.get("audit")
    if not isinstance(audit, Mapping) or audit.get("gate") != Gate.STRUCTURAL_AUDIT.value:
        raise GatePolicyError("structural audit report has no valid structural gate metadata")
    if audit.get("schema_version") != 1:
        raise GatePolicyError("unsupported structural audit report schema_version")
    if audit.get("state") != GateState.PASS.value:
        raise GatePolicyError("structural audit report has no passing structural gate state")
    if (
        audit.get("delivery_status") == DeliveryStatus.COMPLETE.value
        or audit.get("status") == DeliveryStatus.COMPLETE.value
        or value.get("delivery_status") == DeliveryStatus.COMPLETE.value
    ):
        raise GatePolicyError("structural audit report must not claim overall delivery completion")
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        raise GatePolicyError("structural audit report has no evidence object")
    result = dict(evidence)
    if result.get("schema_version") != 1:
        raise GatePolicyError("unsupported structural audit evidence schema_version")
    if result.get("gate") != Gate.STRUCTURAL_AUDIT.value or result.get("state") != GateState.PASS.value:
        raise GatePolicyError("structural audit evidence is not a passing STRUCTURAL_AUDIT result")
    if result.get("delivery_status") == DeliveryStatus.COMPLETE.value or result.get("status") == DeliveryStatus.COMPLETE.value:
        raise GatePolicyError("structural audit evidence must not claim overall delivery completion")
    return result


def _validate_audit_binding(
    job: FrozenJob,
    artifact: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidate_sha256: str,
) -> None:
    expected = {
        "source_sha256": job.source_sha256,
        "manifest_id": job.manifest_id,
        "manifest_sha256": job.manifest_sha256,
        "job_id": job.job_id,
        "artifact_id": artifact["id"],
        "artifact_logical_id": artifact["logical_id"],
        "artifact_kind": artifact["kind"],
        "artifact_type": artifact["kind"],
        "artifact_sha256": candidate_sha256,
    }
    if job.application_plan_sha256 is None:
        raise GatePolicyError("structural audit evidence requires a frozen application plan")
    expected["application_plan_sha256"] = job.application_plan_sha256
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise GatePolicyError(f"structural audit evidence {field} does not match the frozen artifact")
    expected_evidence_id = "audit-" + _digest(
        {key: value for key, value in evidence.items() if key != "evidence_id"}
    )[:32]
    if evidence.get("evidence_id") != expected_evidence_id:
        raise GatePolicyError("structural audit evidence evidence_id does not match its contents")
    if not isinstance(evidence.get("tool"), str) or not isinstance(evidence.get("tool_version"), str):
        raise GatePolicyError("structural audit evidence is missing tool identity")


def bind_structural_audit_evidence(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    artifact_id: str,
    candidate: bytes | str | Path,
    audit_report: Mapping[str, Any],
) -> FrozenJob:
    """Bind a validated W6 audit report to the exact staged artifact."""

    current = _coerce_job(job)
    artifact = _artifact(current, artifact_id)
    candidate_sha256 = _candidate_hash(artifact, candidate)
    evidence = _audit_evidence_value(audit_report)
    _validate_audit_binding(current, artifact, evidence, candidate_sha256)
    details = {"audit_evidence": copy.deepcopy(evidence)}
    evidence_id = _stable_evidence_id(
        "structural-audit",
        {"job_id": current.job_id, "artifact_id": artifact["id"], "candidate_sha256": candidate_sha256, "evidence": evidence},
    )
    return bind_gate_evidence(
        current,
        artifact["id"],
        Gate.STRUCTURAL_AUDIT.value,
        GateState.PASS.value,
        evidence_id=evidence_id,
        tool=str(evidence["tool"]),
        tool_version=str(evidence["tool_version"]),
        details=details,
    )


def bind_native_word_evidence(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    artifact_id: str,
    candidate: bytes | str | Path,
    *,
    state: str,
    open_no_repair: bool | None = None,
    visual_inspection: str | None = None,
    word_version: str | None = None,
    validation_mode: str = "manual",
    environment: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
    reason: str | None = None,
) -> FrozenJob:
    """Record candidate-bound Microsoft Word open and visual validation."""

    current = _coerce_job(job)
    artifact = _artifact(current, artifact_id)
    candidate_sha256 = _candidate_hash(artifact, candidate)
    if Gate.NATIVE_WORD.value not in artifact["required_gates"]:
        raise GatePolicyError(f"artifact {artifact['logical_id']!r} does not require the NATIVE_WORD gate")
    if state not in {GateState.PASS.value, GateState.FAIL.value, GateState.NOT_RUN.value}:
        raise GatePolicyError(f"native Word state {state!r} is unsupported")
    if state == GateState.NOT_RUN.value:
        if reason is None or not isinstance(reason, str) or not reason.strip():
            raise GatePolicyError("NOT_RUN native Word evidence requires an explicit reason")
        return bind_gate_evidence(
            current,
            artifact["id"],
            Gate.NATIVE_WORD.value,
            GateState.NOT_RUN.value,
            tool="Microsoft Word",
            tool_version="not-run",
            reason=reason,
        )

    if not isinstance(open_no_repair, bool):
        raise GatePolicyError("native Word evidence requires a boolean open_no_repair result")
    if visual_inspection not in VISUAL_INSPECTION_STATES:
        raise GatePolicyError("native Word evidence requires visual_inspection PASS or FAIL")
    if not isinstance(word_version, str) or not word_version.strip():
        raise GatePolicyError("native Word PASS/FAIL evidence requires the Word version")
    if validation_mode not in NATIVE_WORD_VALIDATION_MODES:
        raise GatePolicyError(f"unsupported native Word validation_mode {validation_mode!r}")
    if not isinstance(environment, Mapping) or not environment:
        raise GatePolicyError("native Word PASS/FAIL evidence requires a non-empty environment object")
    _canonical_json(environment)
    if not isinstance(details, (Mapping, type(None))):
        raise GatePolicyError("native Word details must be an object")
    extra = {} if details is None else dict(details)
    reserved = {"result", "open_no_repair", "visual_inspection", "word_version", "validation_mode"}
    if reserved & set(extra):
        raise GatePolicyError(f"native Word details cannot override reserved fields {sorted(reserved & set(extra))}")
    _canonical_json(extra)
    if state == GateState.PASS.value and (not open_no_repair or visual_inspection != GateState.PASS.value):
        raise GatePolicyError("native Word evidence cannot PASS after a failed open/no-repair or visual inspection")
    if state == GateState.FAIL.value and open_no_repair and visual_inspection == GateState.PASS.value:
        raise GatePolicyError("native Word evidence cannot FAIL when all required observations PASS")
    if state == GateState.FAIL.value and (reason is None or not isinstance(reason, str) or not reason.strip()):
        raise GatePolicyError("FAIL native Word evidence requires an explicit reason")
    normalized_recorded_at = normalize_recorded_at(recorded_at)

    native_details = {
        "result": state,
        "open_no_repair": open_no_repair,
        "visual_inspection": visual_inspection,
        "word_version": word_version,
        "validation_mode": validation_mode,
        **copy.deepcopy(extra),
    }
    evidence_id = _stable_evidence_id(
        "native-word",
        {
            "job_id": current.job_id,
            "artifact_id": artifact["id"],
            "candidate_sha256": candidate_sha256,
            "details": native_details,
            "environment": environment,
            "recorded_at": normalized_recorded_at,
        },
    )
    return bind_gate_evidence(
        current,
        artifact["id"],
        Gate.NATIVE_WORD.value,
        state,
        evidence_id=evidence_id,
        tool="Microsoft Word",
        tool_version=word_version,
        environment=environment,
        details=native_details,
        recorded_at=normalized_recorded_at,
        reason=reason,
    )


def _common_evidence_errors(job: FrozenJob, artifact: Mapping[str, Any], evidence: Any) -> list[str]:
    if not isinstance(evidence, Mapping):
        return ["gate PASS has no evidence object"]
    expected = {
        "source_sha256": job.source_sha256,
        "manifest_id": job.manifest_id,
        "manifest_sha256": job.manifest_sha256,
        "job_id": job.job_id,
        "artifact_id": artifact["id"],
        "artifact_sha256": artifact.get("content_sha256"),
    }
    return [f"evidence {field} does not match the frozen job" for field, value in expected.items() if evidence.get(field) != value]


def _structural_gate_errors(job: FrozenJob, artifact: Mapping[str, Any]) -> list[str]:
    gate = artifact["gates"].get(Gate.STRUCTURAL_AUDIT.value)
    if not isinstance(gate, Mapping) or gate.get("state") != GateState.PASS.value:
        return []
    evidence = gate.get("evidence")
    errors = _common_evidence_errors(job, artifact, evidence)
    if errors:
        return errors
    details = evidence.get("details")
    nested = details.get("audit_evidence") if isinstance(details, Mapping) else None
    if job.application_plan_sha256 is None or not isinstance(nested, Mapping) or nested.get("application_plan_sha256") != job.application_plan_sha256:
        errors.append("structural gate evidence is not bound to the frozen application plan")
    if not isinstance(nested, Mapping):
        errors.append("structural gate evidence does not contain W6 audit evidence")
    else:
        try:
            _validate_audit_binding(job, artifact, nested, str(artifact.get("content_sha256")))
        except GatePolicyError as error:
            errors.append(str(error))
        expected_evidence_id = _stable_evidence_id(
            "structural-audit",
            {
                "job_id": job.job_id,
                "artifact_id": artifact["id"],
                "candidate_sha256": artifact.get("content_sha256"),
                "evidence": dict(nested),
            },
        )
        if evidence.get("evidence_id") != expected_evidence_id:
            errors.append("structural gate evidence evidence_id does not match its contents")
        if evidence.get("tool") != nested.get("tool") or evidence.get("tool_version") != nested.get("tool_version"):
            errors.append("structural gate evidence tool identity does not match W6 audit evidence")
    return errors


def _native_gate_errors(job: FrozenJob, artifact: Mapping[str, Any]) -> list[str]:
    gate = artifact["gates"].get(Gate.NATIVE_WORD.value)
    if not isinstance(gate, Mapping) or gate.get("state") != GateState.PASS.value:
        return []
    evidence = gate.get("evidence")
    errors = _common_evidence_errors(job, artifact, evidence)
    if errors:
        return errors
    if evidence.get("tool") != "Microsoft Word":
        errors.append("native Word gate evidence has the wrong tool identity")
    if not isinstance(evidence.get("environment"), Mapping) or not evidence["environment"]:
        errors.append("native Word gate evidence has no environment")
    details = evidence.get("details")
    if not isinstance(details, Mapping):
        return errors + ["native Word gate evidence has no validation details"]
    if details.get("result") != GateState.PASS.value:
        errors.append("native Word gate evidence result is not PASS")
    if details.get("open_no_repair") is not True:
        errors.append("native Word gate evidence does not prove open/no-repair")
    if details.get("visual_inspection") != GateState.PASS.value:
        errors.append("native Word gate evidence does not prove visual inspection")
    if details.get("word_version") != evidence.get("tool_version"):
        errors.append("native Word gate evidence Word version is inconsistent")
    if details.get("validation_mode") not in NATIVE_WORD_VALIDATION_MODES:
        errors.append("native Word gate evidence has an unsupported validation mode")
    try:
        normalized_recorded_at = normalize_recorded_at(evidence.get("recorded_at"))
    except GatePolicyError as error:
        normalized_recorded_at = evidence.get("recorded_at")
        errors.append(str(error))
    if details.get("representative_acceptance") is not True:
        errors.append("native Word gate evidence is not from representative P0 acceptance")
    request_id = details.get("acceptance_request_id")
    if not isinstance(request_id, str) or P0_ACCEPTANCE_REQUEST_ID_RE.fullmatch(request_id) is None:
        errors.append("native Word gate evidence has no valid representative acceptance request ID")
    visual_checks = details.get("visual_checks")
    if not isinstance(visual_checks, Mapping) or set(visual_checks) != set(P0_VISUAL_RISK_FAMILIES):
        errors.append("native Word gate evidence has incomplete representative visual checks")
    else:
        non_pass = [
            family
            for family in P0_VISUAL_RISK_FAMILIES
            if visual_checks.get(family) != GateState.PASS.value
        ]
        if non_pass:
            errors.append(f"native Word gate evidence has non-PASS representative visual checks: {non_pass}")
    expected_evidence_id = _stable_evidence_id(
        "native-word",
        {
            "job_id": job.job_id,
            "artifact_id": artifact["id"],
            "candidate_sha256": artifact.get("content_sha256"),
            "details": dict(details),
            "environment": evidence.get("environment"),
            "recorded_at": normalized_recorded_at,
        },
    )
    if evidence.get("evidence_id") != expected_evidence_id:
        errors.append("native Word gate evidence evidence_id does not match its contents")
    return errors


def evaluate_p0_gate(job: FrozenJob | Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Evaluate P0 without mutating the job or deriving an alternate status."""

    current = _coerce_job(job)
    errors: list[str] = []
    pending: list[str] = []
    occurrence_report = []
    for occurrence in current.occurrences:
        status = occurrence.get("status")
        terminal_success = status in SUCCESSFUL_OCCURRENCE_STATUSES
        if not terminal_success:
            pending.append(f"occurrence {occurrence.get('id')!r} has status {status!r}")
        occurrence_report.append({"id": occurrence.get("id"), "status": status, "successful": terminal_success})

    artifact_report = []
    for artifact in current.artifacts:
        artifact_errors: list[str] = []
        artifact_pending: list[str] = []
        missing = sorted(P0_REQUIRED_GATES - set(artifact["required_gates"]))
        if missing:
            artifact_errors.append(f"missing P0 required gates: {missing}")
        if artifact.get("state") not in {ArtifactState.VALIDATED.value, ArtifactState.FINALIZED.value}:
            artifact_pending.append(f"artifact state is {artifact.get('state')!r}, not VALIDATED or FINALIZED")
        if artifact.get("content_sha256") is None:
            artifact_pending.append("artifact has no content hash")
        for gate_name in sorted(P0_REQUIRED_GATES):
            gate = artifact["gates"].get(gate_name)
            if not isinstance(gate, Mapping):
                artifact_errors.append(f"missing gate record {gate_name}")
                continue
            state = gate.get("state")
            if state == GateState.FAIL.value:
                artifact_errors.append(f"gate {gate_name} is FAIL")
            elif state != GateState.PASS.value:
                artifact_pending.append(f"gate {gate_name} is {state!r}")
        if not artifact_errors:
            artifact_errors.extend(_structural_gate_errors(current, artifact))
            artifact_errors.extend(_native_gate_errors(current, artifact))
        if artifact_errors:
            errors.extend(f"{artifact['logical_id']}: {item}" for item in artifact_errors)
        pending.extend(f"{artifact['logical_id']}: {item}" for item in artifact_pending)
        artifact_report.append(
            {
                "id": artifact["id"],
                "logical_id": artifact["logical_id"],
                "state": artifact.get("state"),
                "content_sha256": artifact.get("content_sha256"),
                "required_gates": list(artifact["required_gates"]),
                "gate_states": {name: artifact["gates"].get(name, {}).get("state") for name in artifact["gates"]},
                "errors": artifact_errors,
                "pending": artifact_pending,
            }
        )

    state = GateState.FAIL.value if errors else GateState.NOT_RUN.value if pending else GateState.PASS.value
    return {
        "schema_version": P0_GATE_SCHEMA_VERSION,
        "gate": "P0_PRODUCTION_PILOT",
        "state": state,
        "delivery_status": current.status,
        "job_id": current.job_id,
        "occurrences": occurrence_report,
        "artifacts": artifact_report,
        "errors": errors,
        "pending": pending,
    }


def assert_p0_ready(job: FrozenJob | Mapping[str, Any] | str | Path) -> FrozenJob:
    """Require P0 evidence before calling the low-level atomic promoter."""

    current = _coerce_job(job)
    report = evaluate_p0_gate(current)
    if report["state"] != GateState.PASS.value or report["delivery_status"] != DeliveryStatus.COMPLETE.value:
        raise GatePolicyError(
            "P0 production-pilot gate is not PASS: " + _canonical_json(report)
        )
    return current


def finalize_p0_artifact_set(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    staging_paths: Mapping[str, str | Path],
    final_paths: Mapping[str, str | Path],
    *,
    source: str | Path,
) -> FrozenJob:
    """Apply P0 policy, then delegate promotion to W5's atomic primitive."""

    current = assert_p0_ready(job)
    from word_formula_omml.applicator import finalize_artifact_set

    return finalize_artifact_set(current, staging_paths, final_paths, source=source)


__all__ = [
    "GatePolicyError",
    "NATIVE_WORD_VALIDATION_MODES",
    "P0_GATE_SCHEMA_VERSION",
    "P0_REQUIRED_GATES",
    "P0_VISUAL_RISK_FAMILIES",
    "VISUAL_INSPECTION_STATES",
    "assert_p0_ready",
    "bind_native_word_evidence",
    "bind_structural_audit_evidence",
    "evaluate_p0_gate",
    "finalize_p0_artifact_set",
    "normalize_recorded_at",
]
