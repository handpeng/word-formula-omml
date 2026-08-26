"""Versioned manifest, job, artifact, and lifecycle contracts.

This module is intentionally stdlib-only.  It is the single source of truth
for the data values shared by inventory, recovery, generation, application,
and audit stages.  Callers receive a ``ContractError`` instead of a silently
coerced or partially valid object.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class ContractError(ValueError):
    """Raised when a contract cannot be safely consumed."""


class SourceType(str, Enum):
    RAW_LATEX = "RAW_LATEX"
    PARTIAL_LATEX = "PARTIAL_LATEX"
    PLAIN_MATH = "PLAIN_MATH"
    UNICODE_MATH = "UNICODE_MATH"
    CORRUPTED_TEXT = "CORRUPTED_TEXT"
    EXISTING_OMML = "EXISTING_OMML"
    EQ_FIELD = "EQ_FIELD"
    EMBEDDED_EQUATION_OBJECT = "EMBEDDED_EQUATION_OBJECT"
    UNKNOWN_FORMULA = "UNKNOWN_FORMULA"


class Confidence(str, Enum):
    AUTHORITATIVE = "AUTHORITATIVE"
    HIGH = "HIGH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNRECOVERABLE = "UNRECOVERABLE"


class OccurrenceStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    RECOVERED = "RECOVERED"
    APPROVED = "APPROVED"
    STAGED = "STAGED"
    AUDITED = "AUDITED"
    APPLIED = "APPLIED"
    PRESERVED = "PRESERVED"
    EXCLUDED = "EXCLUDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_SPECIAL_HANDLER = "NEEDS_SPECIAL_HANDLER"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


class DeliveryStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL_REVIEW_OUTPUT = "PARTIAL_REVIEW_OUTPUT"
    FAILED = "FAILED"


class Gate(str, Enum):
    STRUCTURAL_AUDIT = "STRUCTURAL_AUDIT"
    NATIVE_WORD = "NATIVE_WORD"
    SEMANTIC = "SEMANTIC"
    STYLE = "STYLE"
    PACKAGE = "PACKAGE"
    REVISION = "REVISION"
    RENDER = "RENDER"
    SOURCE_ALIGNMENT = "SOURCE_ALIGNMENT"


class GateState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    STALE = "STALE"


class ArtifactState(str, Enum):
    NOT_CREATED = "NOT_CREATED"
    STAGING = "STAGING"
    VALIDATED = "VALIDATED"
    FINALIZED = "FINALIZED"


TERMINAL_OCCURRENCE_STATUSES = frozenset(
    {
        OccurrenceStatus.APPLIED.value,
        OccurrenceStatus.PRESERVED.value,
        OccurrenceStatus.EXCLUDED.value,
        OccurrenceStatus.NEEDS_REVIEW.value,
        OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        OccurrenceStatus.REFUSED.value,
        OccurrenceStatus.FAILED.value,
    }
)
SUCCESSFUL_OCCURRENCE_STATUSES = frozenset(
    {
        OccurrenceStatus.APPLIED.value,
        OccurrenceStatus.PRESERVED.value,
        OccurrenceStatus.EXCLUDED.value,
    }
)
KNOWN_SOURCE_TYPES = frozenset(item.value for item in SourceType)
KNOWN_CONFIDENCES = frozenset(item.value for item in Confidence)
KNOWN_OCCURRENCE_STATUSES = frozenset(item.value for item in OccurrenceStatus)
KNOWN_GATES = frozenset(item.value for item in Gate)
KNOWN_GATE_STATES = frozenset(item.value for item in GateState)
KNOWN_ARTIFACT_STATES = frozenset(item.value for item in ArtifactState)
DEFAULT_REQUIRED_GATES = (
    Gate.STRUCTURAL_AUDIT.value,
    Gate.NATIVE_WORD.value,
)


MANIFEST_FIELDS = {
    "schema_version",
    "manifest_id",
    "source_sha256",
    "revision_author",
    "formulas",
    "extensions",
}
OCCURRENCE_FIELDS = {
    "id",
    "latex",
    "layout",
    "paragraph",
    "sequence_in_paragraph",
    "anchor_before",
    "source",
    "anchor_after",
    "run_index",
    "run_start",
    "run_end",
    "color",
    "paragraph_style",
    "inside_existing_revision",
    "adjacent_bookmark",
    "adjacent_field",
    "adjacent_hyperlink",
    "adjacent_drawing",
    "package_part",
    "story",
    "source_type",
    "raw_source",
    "normalized_latex",
    "canonical",
    "evidence",
    "confidence",
    "ambiguity",
    "anchors",
    "run_boundaries",
    "revision_ancestry",
    "protected_containers",
    "style_snapshot",
    "resolved_style",
    "target_layout",
    "expected_matches",
    "application",
    "omml",
    "semantic",
    "audit",
    "status",
    "exclusion",
    "extensions",
}
REQUESTED_ARTIFACT_FIELDS = {
    "id",
    "kind",
    "required_gates",
    "extensions",
}
ARTIFACT_FIELDS = {
    "id",
    "logical_id",
    "kind",
    "requested",
    "state",
    "content_sha256",
    "required_gates",
    "gates",
    "extensions",
}
GATE_FIELDS = {
    "state",
    "evidence",
    "reason",
}
EVIDENCE_FIELDS = {
    "evidence_id",
    "source_sha256",
    "manifest_id",
    "manifest_sha256",
    "job_id",
    "artifact_id",
    "artifact_sha256",
    "tool",
    "tool_version",
    "environment",
    "recorded_at",
    "details",
    "extensions",
}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ContractError(f"value is not deterministic JSON: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(
            f"{path} contains unsupported fields {unknown}; put forward-compatible data under extensions"
        )


def _require_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ContractError(f"{path} must be a {'non-empty ' if nonempty else ''}string")
    return value


def _optional_string(value: Any, path: str) -> None:
    if value is not None:
        _require_string(value, path)


def _require_sha256(value: Any, path: str) -> str:
    value = _require_string(value, path)
    if not SHA256_RE.fullmatch(value):
        raise ContractError(f"{path} must be a lowercase SHA-256 hex digest")
    return value


def _optional_sha256(value: Any, path: str) -> None:
    if value is not None:
        _require_sha256(value, path)


def _require_enum(value: Any, allowed: frozenset[str], path: str) -> str:
    value = _require_string(value, path)
    if value not in allowed:
        raise ContractError(f"{path} has unsupported value {value!r}; expected one of {sorted(allowed)}")
    return value


def _optional_enum(value: Any, allowed: frozenset[str], path: str) -> None:
    if value is not None:
        _require_enum(value, allowed, path)


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path} must be a boolean")
    return value


def _optional_bool(value: Any, path: str) -> None:
    if value is not None:
        _require_bool(value, path)


def _require_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{path} must be >= {minimum}")
    return value


def _optional_int(value: Any, path: str, *, minimum: int | None = None) -> None:
    if value is not None:
        _require_int(value, path, minimum=minimum)


def _require_extensions(value: Any, path: str) -> None:
    if value is not None:
        _require_mapping(value, path)


def _normalize_manifest_input(data: Any) -> tuple[Mapping[str, Any], bool]:
    if isinstance(data, list):
        return {"formulas": data}, True
    mapping = _require_mapping(data, "manifest")
    return mapping, False


def deterministic_occurrence_id(
    row: Mapping[str, Any],
    *,
    source_sha256: str | None = None,
    position: int | None = None,
) -> str:
    """Derive a retry-stable ID from an occurrence's frozen source identity.

    Inventory code should supply package/story, paragraph/sequence, anchors,
    run offsets, and raw source where available.  Position is included only as
    an explicit deterministic tie-breaker for records without stable anchors;
    it is never used to relocate an already frozen occurrence.
    """

    if source_sha256 is not None:
        _require_sha256(source_sha256, "source_sha256")
    if position is not None:
        _require_int(position, "position", minimum=1)
    value = _require_mapping(row, "occurrence")
    if position is None and not any(
        value.get(field) is not None
        for field in (
            "paragraph",
            "sequence_in_paragraph",
            "anchor_before",
            "anchor_after",
            "run_index",
            "run_start",
            "run_end",
        )
    ):
        raise ContractError(
            "occurrence needs a stable paragraph/anchor/run location or an explicit position for ID derivation"
        )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "package_part": value.get("package_part", "word/document.xml"),
        "story": value.get("story", "main"),
        "paragraph": value.get("paragraph"),
        "sequence_in_paragraph": value.get("sequence_in_paragraph"),
        "anchor_before": value.get("anchor_before"),
        "source": value.get("raw_source", value.get("source", value.get("latex"))),
        "anchor_after": value.get("anchor_after"),
        "run_index": value.get("run_index"),
        "run_start": value.get("run_start"),
        "run_end": value.get("run_end"),
        "layout": value.get("layout", "inline"),
        "position": position,
    }
    return f"occ-{_digest(identity)[:32]}"


def _validate_occurrence(row: Any, position: int) -> dict[str, Any]:
    value = dict(_require_mapping(row, f"formulas[{position}]"))
    path = f"formulas[{position}]"
    _reject_unknown(value, OCCURRENCE_FIELDS, path)
    value["id"] = _require_string(value.get("id"), f"{path}.id")
    if not ID_RE.fullmatch(value["id"]):
        raise ContractError(f"{path}.id has invalid format {value['id']!r}")
    value["latex"] = _require_string(value.get("latex"), f"{path}.latex")
    layout = value.get("layout", "inline")
    if layout not in {"inline", "display"}:
        raise ContractError(f"{path}.layout has unsupported value {layout!r}; expected 'inline' or 'display'")
    value["layout"] = layout

    for field in (
        "anchor_before",
        "source",
        "anchor_after",
        "color",
        "paragraph_style",
        "package_part",
        "story",
        "raw_source",
        "normalized_latex",
        "target_layout",
    ):
        _optional_string(value.get(field), f"{path}.{field}")
    if value.get("target_layout") is not None and value["target_layout"] not in {"inline", "display"}:
        raise ContractError(
            f"{path}.target_layout has unsupported value {value['target_layout']!r}; expected 'inline' or 'display'"
        )
    for field in ("paragraph", "sequence_in_paragraph", "run_index", "run_start", "run_end"):
        _optional_int(value.get(field), f"{path}.{field}", minimum=0)
    for field in (
        "inside_existing_revision",
        "adjacent_bookmark",
        "adjacent_field",
        "adjacent_hyperlink",
        "adjacent_drawing",
    ):
        _optional_bool(value.get(field), f"{path}.{field}")
    _optional_enum(value.get("source_type"), KNOWN_SOURCE_TYPES, f"{path}.source_type")
    _optional_enum(value.get("confidence"), KNOWN_CONFIDENCES, f"{path}.confidence")
    _optional_enum(value.get("status"), KNOWN_OCCURRENCE_STATUSES, f"{path}.status")
    expected_matches = value.get("expected_matches", 1)
    value["expected_matches"] = _require_int(expected_matches, f"{path}.expected_matches", minimum=1)
    if value.get("status") is None:
        value["status"] = OccurrenceStatus.DISCOVERED.value
    if value.get("source_type") is None:
        value["source_type"] = SourceType.UNKNOWN_FORMULA.value
    if value.get("confidence") is None:
        value["confidence"] = Confidence.REVIEW_REQUIRED.value
    for field in (
        "canonical",
        "evidence",
        "ambiguity",
        "anchors",
        "run_boundaries",
        "revision_ancestry",
        "protected_containers",
        "style_snapshot",
        "resolved_style",
        "application",
        "omml",
        "semantic",
        "audit",
    ):
        if field in value:
            _canonical_json(value[field])
    if "extensions" in value:
        _require_extensions(value["extensions"], f"{path}.extensions")
    if value["status"] == OccurrenceStatus.EXCLUDED.value:
        exclusion = _require_mapping(value.get("exclusion"), f"{path}.exclusion")
        if exclusion.get("approved") is not True:
            raise ContractError(f"{path}.exclusion.approved must be true for EXCLUDED occurrences")
        _require_string(exclusion.get("reason"), f"{path}.exclusion.reason")
    return value


def _manifest_identity_payload(
    *,
    source_sha256: str | None,
    revision_author: str | None,
    formulas: Sequence[Mapping[str, Any]],
    extensions: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "revision_author": revision_author,
        "formulas": [_copy(item) for item in formulas],
        "extensions": _copy(dict(extensions)),
    }


@dataclasses.dataclass(frozen=True)
class Manifest:
    """Validated occurrence manifest.

    ``manifest_id`` and ``manifest_sha256`` are derived from the immutable
    identity payload.  Lifecycle and delivery state belongs to ``FrozenJob``;
    this separation prevents a caller from setting a job complete by editing a
    manifest status field.
    """

    schema_version: int
    formulas: tuple[dict[str, Any], ...]
    source_sha256: str | None = None
    revision_author: str | None = None
    extensions: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def manifest_sha256(self) -> str:
        return _digest(
            _manifest_identity_payload(
                source_sha256=self.source_sha256,
                revision_author=self.revision_author,
                formulas=self.formulas,
                extensions=self.extensions,
            )
        )

    @property
    def manifest_id(self) -> str:
        return f"manifest-{self.manifest_sha256[:32]}"

    def identity_payload(self) -> dict[str, Any]:
        return _manifest_identity_payload(
            source_sha256=self.source_sha256,
            revision_author=self.revision_author,
            formulas=self.formulas,
            extensions=self.extensions,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "formulas": [_copy(item) for item in self.formulas],
        }
        if self.source_sha256 is not None:
            result["source_sha256"] = self.source_sha256
        if self.revision_author is not None:
            result["revision_author"] = self.revision_author
        if self.extensions:
            result["extensions"] = _copy(dict(self.extensions))
        return result


def load_manifest(data_or_path: Any) -> Manifest:
    """Load a V1 manifest or the historical bare array/object form.

    A missing ``schema_version`` is the only compatibility exception.  An
    explicitly newer version is rejected instead of being silently coerced.
    """

    if isinstance(data_or_path, (str, Path)):
        path = Path(data_or_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(f"cannot load manifest {path}: {error}") from error
    else:
        data = data_or_path
    mapping, _legacy = _normalize_manifest_input(data)
    _reject_unknown(mapping, MANIFEST_FIELDS, "manifest")
    version = mapping.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError("manifest.schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise ContractError(
            f"unsupported manifest.schema_version {version}; supported version is {SCHEMA_VERSION}"
        )
    formulas_value = mapping.get("formulas")
    if not isinstance(formulas_value, list):
        raise ContractError("manifest.formulas must be an array")
    formulas = tuple(_validate_occurrence(row, index) for index, row in enumerate(formulas_value, 1))
    ids = [row["id"] for row in formulas]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise ContractError(f"manifest.formulas contains duplicate ids {duplicates}")
    source_sha256 = mapping.get("source_sha256")
    _optional_sha256(source_sha256, "manifest.source_sha256")
    revision_author = mapping.get("revision_author")
    _optional_string(revision_author, "manifest.revision_author")
    extensions = mapping.get("extensions", {})
    _require_extensions(extensions, "manifest.extensions")
    manifest = Manifest(
        schema_version=version,
        formulas=formulas,
        source_sha256=source_sha256,
        revision_author=revision_author,
        extensions=_copy(dict(extensions)),
    )
    supplied_id = mapping.get("manifest_id")
    if supplied_id is not None:
        _require_string(supplied_id, "manifest.manifest_id")
        if supplied_id != manifest.manifest_id:
            raise ContractError(
                "manifest.manifest_id does not match the deterministic identity of its contents"
            )
    return manifest


def dump_manifest(manifest: Manifest, path: str | Path) -> None:
    """Write a validated manifest with deterministic JSON formatting."""

    if not isinstance(manifest, Manifest):
        raise ContractError("dump_manifest requires a Manifest")
    validated = load_manifest(manifest.to_dict())
    Path(path).write_text(_canonical_json(validated.to_dict()) + "\n", encoding="utf-8")


def _normalize_requested_artifacts(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractError("requested_artifacts must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {
                "id": item,
                "kind": item,
                "required_gates": list(DEFAULT_REQUIRED_GATES),
            }
        mapping = dict(_require_mapping(item, f"requested_artifacts[{index}]"))
        _reject_unknown(mapping, REQUESTED_ARTIFACT_FIELDS, f"requested_artifacts[{index}]")
        artifact_id = _require_string(mapping.get("id"), f"requested_artifacts[{index}].id")
        if not ID_RE.fullmatch(artifact_id):
            raise ContractError(f"requested_artifacts[{index}].id has invalid format {artifact_id!r}")
        if artifact_id in seen:
            raise ContractError(f"duplicate requested artifact id: {artifact_id}")
        seen.add(artifact_id)
        kind = _require_string(mapping.get("kind", artifact_id), f"requested_artifacts[{index}].kind")
        gates = mapping.get("required_gates")
        if not isinstance(gates, (list, tuple)) or not gates:
            raise ContractError(f"requested_artifacts[{index}].required_gates must be a non-empty array")
        normalized_gates = []
        for gate_index, gate in enumerate(gates, 1):
            normalized_gates.append(
                _require_enum(gate, KNOWN_GATES, f"requested_artifacts[{index}].required_gates[{gate_index}]")
            )
        if len(set(normalized_gates)) != len(normalized_gates):
            raise ContractError(f"requested_artifacts[{index}].required_gates contains duplicates")
        extensions = mapping.get("extensions", {})
        _require_extensions(extensions, f"requested_artifacts[{index}].extensions")
        normalized.append(
            {
                "id": artifact_id,
                "kind": kind,
                "required_gates": normalized_gates,
                **({"extensions": _copy(dict(extensions))} if extensions else {}),
            }
        )
    normalized.sort(key=lambda item: item["id"])
    for item in normalized:
        item["required_gates"] = sorted(item["required_gates"])
    return tuple(normalized)


def _requested_artifacts_sha256(requested: Sequence[Mapping[str, Any]]) -> str:
    return _digest({"requested_artifacts": [_copy(item) for item in requested]})


def _artifact_id(job_id: str, logical_id: str) -> str:
    return f"artifact-{_digest({'job_id': job_id, 'logical_id': logical_id})[:32]}"


def _job_identity_payload(
    *,
    source_sha256: str,
    manifest_id: str,
    manifest_sha256: str,
    occurrence_ids: Sequence[str],
    requested_artifacts: Sequence[Mapping[str, Any]],
    application_plan_sha256: str | None,
    extensions: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "manifest_id": manifest_id,
        "manifest_sha256": manifest_sha256,
        "occurrence_ids": list(occurrence_ids),
        "requested_artifacts": [_copy(item) for item in requested_artifacts],
        "application_plan_sha256": application_plan_sha256,
        "extensions": _copy(dict(extensions)),
    }


def _new_artifact(job_id: str, requested: Mapping[str, Any]) -> dict[str, Any]:
    artifact_id = _artifact_id(job_id, requested["id"])
    return {
        "id": artifact_id,
        "logical_id": requested["id"],
        "kind": requested["kind"],
        "requested": True,
        "state": ArtifactState.NOT_CREATED.value,
        "content_sha256": None,
        "required_gates": list(requested["required_gates"]),
        "gates": {
            gate: {"state": GateState.NOT_RUN.value}
            for gate in requested["required_gates"]
        },
    }


def _validate_evidence(
    evidence: Any,
    *,
    path: str,
    job_id: str,
    source_sha256: str,
    manifest_id: str,
    manifest_sha256: str,
    artifact_id: str,
    artifact_sha256: str | None,
    allow_stale_hash: bool,
) -> dict[str, Any]:
    value = dict(_require_mapping(evidence, path))
    _reject_unknown(value, EVIDENCE_FIELDS, path)
    for field in ("evidence_id", "tool", "tool_version"):
        _require_string(value.get(field), f"{path}.{field}")
    _require_sha256(value.get("source_sha256"), f"{path}.source_sha256")
    _require_sha256(value.get("manifest_sha256"), f"{path}.manifest_sha256")
    _require_string(value.get("manifest_id"), f"{path}.manifest_id")
    _require_string(value.get("job_id"), f"{path}.job_id")
    _require_string(value.get("artifact_id"), f"{path}.artifact_id")
    if value["source_sha256"] != source_sha256:
        raise ContractError(f"{path}.source_sha256 does not match the frozen job")
    if value["manifest_id"] != manifest_id or value["manifest_sha256"] != manifest_sha256:
        raise ContractError(f"{path} manifest identity does not match the frozen job")
    if value["job_id"] != job_id:
        raise ContractError(f"{path}.job_id does not match the frozen job")
    if value["artifact_id"] != artifact_id:
        raise ContractError(f"{path}.artifact_id does not match its artifact")
    evidence_hash = value.get("artifact_sha256")
    if evidence_hash is not None:
        _require_sha256(evidence_hash, f"{path}.artifact_sha256")
    if not allow_stale_hash and evidence_hash != artifact_sha256:
        raise ContractError(f"{path}.artifact_sha256 does not match the candidate artifact")
    _optional_string(value.get("recorded_at"), f"{path}.recorded_at")
    if value.get("environment") is not None:
        _require_mapping(value["environment"], f"{path}.environment")
    if value.get("details") is not None:
        _canonical_json(value["details"])
    if value.get("extensions") is not None:
        _require_extensions(value["extensions"], f"{path}.extensions")
    return value


def _validate_gate(
    gate: Any,
    *,
    path: str,
    job_id: str,
    source_sha256: str,
    manifest_id: str,
    manifest_sha256: str,
    artifact_id: str,
    artifact_sha256: str | None,
) -> dict[str, Any]:
    value = dict(_require_mapping(gate, path))
    _reject_unknown(value, GATE_FIELDS, path)
    state = _require_enum(value.get("state"), KNOWN_GATE_STATES, f"{path}.state")
    if state == GateState.PASS.value:
        if artifact_sha256 is None:
            raise ContractError(f"{path} cannot PASS before the candidate has a content_sha256")
        value["evidence"] = _validate_evidence(
            value.get("evidence"),
            path=f"{path}.evidence",
            job_id=job_id,
            source_sha256=source_sha256,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
            allow_stale_hash=False,
        )
    elif state == GateState.STALE.value and value.get("evidence") is not None:
        value["evidence"] = _validate_evidence(
            value["evidence"],
            path=f"{path}.evidence",
            job_id=job_id,
            source_sha256=source_sha256,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
            allow_stale_hash=True,
        )
    elif state == GateState.FAIL.value and value.get("evidence") is not None:
        value["evidence"] = _validate_evidence(
            value["evidence"],
            path=f"{path}.evidence",
            job_id=job_id,
            source_sha256=source_sha256,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
            allow_stale_hash=False,
        )
    elif state == GateState.NOT_RUN.value and value.get("evidence") is not None:
        raise ContractError(f"{path} cannot carry evidence while NOT_RUN")
    _optional_string(value.get("reason"), f"{path}.reason")
    return value


def _validate_artifact(
    artifact: Any,
    *,
    position: int,
    job_id: str,
    source_sha256: str,
    manifest_id: str,
    manifest_sha256: str,
    requested_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    value = dict(_require_mapping(artifact, f"artifacts[{position}]"))
    path = f"artifacts[{position}]"
    _reject_unknown(value, ARTIFACT_FIELDS, path)
    artifact_id = _require_string(value.get("id"), f"{path}.id")
    logical_id = _require_string(value.get("logical_id"), f"{path}.logical_id")
    requested = requested_by_id.get(logical_id)
    if requested is None:
        raise ContractError(f"{path}.logical_id {logical_id!r} is not in the frozen requested set")
    expected_id = _artifact_id(job_id, logical_id)
    if artifact_id != expected_id:
        raise ContractError(f"{path}.id does not match the frozen logical artifact identity")
    if value.get("kind") != requested["kind"]:
        raise ContractError(f"{path}.kind does not match the frozen requested artifact")
    if value.get("requested") is not True:
        raise ContractError(f"{path}.requested must be true for a frozen requested artifact")
    state = _require_enum(value.get("state"), KNOWN_ARTIFACT_STATES, f"{path}.state")
    _optional_sha256(value.get("content_sha256"), f"{path}.content_sha256")
    required_gates = value.get("required_gates")
    if required_gates != requested["required_gates"]:
        raise ContractError(f"{path}.required_gates changed after the requested set was frozen")
    gates = _require_mapping(value.get("gates"), f"{path}.gates")
    if set(gates) != set(required_gates):
        raise ContractError(f"{path}.gates must contain exactly every required gate")
    value["gates"] = {
        gate: _validate_gate(
            gates[gate],
            path=f"{path}.gates.{gate}",
            job_id=job_id,
            source_sha256=source_sha256,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
            artifact_id=artifact_id,
            artifact_sha256=value.get("content_sha256"),
        )
        for gate in required_gates
    }
    if value.get("extensions") is not None:
        _require_extensions(value["extensions"], f"{path}.extensions")
    return value


def _derive_job_status_from_parts(
    occurrences: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
) -> str:
    occurrence_statuses = [item.get("status") for item in occurrences]
    if any(status == OccurrenceStatus.FAILED.value for status in occurrence_statuses):
        return DeliveryStatus.FAILED.value
    for artifact in artifacts:
        for gate in artifact["gates"].values():
            if gate["state"] == GateState.FAIL.value:
                return DeliveryStatus.FAILED.value
    all_occurrences_successful = bool(occurrences) and all(
        status in SUCCESSFUL_OCCURRENCE_STATUSES for status in occurrence_statuses
    )
    all_artifacts_pass = bool(artifacts) and all(
        artifact.get("content_sha256") is not None
        and all(gate["state"] == GateState.PASS.value for gate in artifact["gates"].values())
        for artifact in artifacts
    )
    if all_occurrences_successful and all_artifacts_pass:
        return DeliveryStatus.COMPLETE.value
    return DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value


@dataclasses.dataclass(frozen=True)
class FrozenJob:
    """A source- and deliverable-set-bound job with derived completion."""

    schema_version: int
    job_id: str
    source_sha256: str
    manifest_id: str
    manifest_sha256: str
    occurrence_ids: tuple[str, ...]
    requested_artifacts: tuple[dict[str, Any], ...]
    occurrences: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    application_plan_sha256: str | None = None
    extensions: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def requested_artifacts_sha256(self) -> str:
        return _requested_artifacts_sha256(self.requested_artifacts)

    @property
    def status(self) -> str:
        _validate_job(self)
        return _derive_job_status_from_parts(self.occurrences, self.artifacts)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_sha256": self.source_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "occurrence_ids": list(self.occurrence_ids),
            "requested_artifacts": [_copy(item) for item in self.requested_artifacts],
            "requested_artifacts_sha256": self.requested_artifacts_sha256,
            "occurrences": [_copy(item) for item in self.occurrences],
            "artifacts": [_copy(item) for item in self.artifacts],
            "status": self.status,
        }
        if self.application_plan_sha256 is not None:
            result["application_plan_sha256"] = self.application_plan_sha256
        if self.extensions:
            result["extensions"] = _copy(dict(self.extensions))
        return result


def _validate_job(job: FrozenJob) -> FrozenJob:
    if job.schema_version != SCHEMA_VERSION:
        raise ContractError(f"unsupported job schema_version {job.schema_version}")
    _require_sha256(job.source_sha256, "job.source_sha256")
    _require_string(job.manifest_id, "job.manifest_id")
    _require_sha256(job.manifest_sha256, "job.manifest_sha256")
    _optional_sha256(job.application_plan_sha256, "job.application_plan_sha256")
    _require_extensions(job.extensions, "job.extensions")
    requested = _normalize_requested_artifacts(list(job.requested_artifacts))
    if requested != job.requested_artifacts:
        raise ContractError("job requested_artifacts are not in canonical form")
    expected_ids = tuple(
        _require_string(
            _require_mapping(item, f"job.occurrences[{index}]").get("id"),
            f"job.occurrences[{index}].id",
        )
        for index, item in enumerate(job.occurrences, 1)
    )
    if expected_ids != job.occurrence_ids:
        raise ContractError("job.occurrence_ids do not match occurrence records")
    if len(set(job.occurrence_ids)) != len(job.occurrence_ids):
        raise ContractError("job.occurrence_ids contain duplicates")
    requested_by_id = {item["id"]: item for item in requested}
    if len(job.artifacts) != len(requested):
        raise ContractError("job must contain exactly one artifact record per requested artifact")
    artifacts = tuple(
        _validate_artifact(
            item,
            position=index,
            job_id=job.job_id,
            source_sha256=job.source_sha256,
            manifest_id=job.manifest_id,
            manifest_sha256=job.manifest_sha256,
            requested_by_id=requested_by_id,
        )
        for index, item in enumerate(job.artifacts, 1)
    )
    if {item["logical_id"] for item in artifacts} != set(requested_by_id):
        raise ContractError("job artifacts do not cover exactly the requested set")
    statuses = []
    for index, item in enumerate(job.occurrences, 1):
        row = _validate_occurrence(item, index)
        statuses.append(row)
    identity = _job_identity_payload(
        source_sha256=job.source_sha256,
        manifest_id=job.manifest_id,
        manifest_sha256=job.manifest_sha256,
        occurrence_ids=job.occurrence_ids,
        requested_artifacts=requested,
        application_plan_sha256=job.application_plan_sha256,
        extensions=job.extensions,
    )
    expected_job_id = f"job-{_digest(identity)[:32]}"
    if job.job_id != expected_job_id:
        raise ContractError("job.job_id does not match its frozen identity")
    if _requested_artifacts_sha256(requested) != _requested_artifacts_sha256(job.requested_artifacts):
        raise ContractError("job requested artifact set hash mismatch")
    return dataclasses.replace(job, requested_artifacts=requested, occurrences=tuple(statuses), artifacts=artifacts)


def freeze_job(
    manifest: Manifest,
    source_sha256: str,
    requested_artifacts: Sequence[str | Mapping[str, Any]],
    *,
    application_plan_sha256: str | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> FrozenJob:
    """Freeze a manifest against a source and exact requested artifact set."""

    if not isinstance(manifest, Manifest):
        raise ContractError("freeze_job requires a Manifest")
    manifest = load_manifest(manifest.to_dict())
    source_sha256 = _require_sha256(source_sha256, "source_sha256")
    if manifest.source_sha256 is not None and manifest.source_sha256 != source_sha256:
        raise ContractError("manifest.source_sha256 does not match the source being frozen")
    _optional_sha256(application_plan_sha256, "application_plan_sha256")
    extensions = {} if extensions is None else dict(_require_mapping(extensions, "extensions"))
    requested = _normalize_requested_artifacts(requested_artifacts)
    occurrences = [_copy(row) for row in manifest.formulas]
    forbidden_frozen_statuses = {
        OccurrenceStatus.STAGED.value,
        OccurrenceStatus.AUDITED.value,
        OccurrenceStatus.APPLIED.value,
        OccurrenceStatus.FAILED.value,
    }
    for row in occurrences:
        if row["status"] in forbidden_frozen_statuses:
            raise ContractError(
                f"occurrence {row['id']} has output lifecycle status {row['status']}; rebuild from an input manifest"
            )
    identity = _job_identity_payload(
        source_sha256=source_sha256,
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        occurrence_ids=[row["id"] for row in manifest.formulas],
        requested_artifacts=requested,
        application_plan_sha256=application_plan_sha256,
        extensions=extensions,
    )
    job_id = f"job-{_digest(identity)[:32]}"
    artifacts = [_new_artifact(job_id, item) for item in requested]
    job = FrozenJob(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        source_sha256=source_sha256,
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        occurrence_ids=tuple(row["id"] for row in manifest.formulas),
        requested_artifacts=tuple(_copy(item) for item in requested),
        occurrences=tuple(occurrences),
        artifacts=tuple(artifacts),
        application_plan_sha256=application_plan_sha256,
        extensions=extensions,
    )
    return _validate_job(job)


def derive_job_status(job: FrozenJob | Mapping[str, Any]) -> str:
    """Return the only supported delivery status derivation."""

    if isinstance(job, Mapping):
        job = load_job(job)
    if not isinstance(job, FrozenJob):
        raise ContractError("derive_job_status requires a FrozenJob or job object")
    job = _validate_job(job)
    return job.status


def load_job(data_or_path: Any) -> FrozenJob:
    if isinstance(data_or_path, (str, Path)):
        path = Path(data_or_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(f"cannot load job {path}: {error}") from error
    else:
        data = data_or_path
    mapping = _require_mapping(data, "job")
    allowed = {
        "schema_version",
        "job_id",
        "source_sha256",
        "manifest_id",
        "manifest_sha256",
        "occurrence_ids",
        "requested_artifacts",
        "requested_artifacts_sha256",
        "occurrences",
        "artifacts",
        "status",
        "application_plan_sha256",
        "extensions",
    }
    _reject_unknown(mapping, allowed, "job")
    version = mapping.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ContractError(f"unsupported job.schema_version {version!r}")
    occurrence_ids = mapping.get("occurrence_ids")
    if not isinstance(occurrence_ids, list) or not all(isinstance(item, str) for item in occurrence_ids):
        raise ContractError("job.occurrence_ids must be an array of strings")
    requested = _normalize_requested_artifacts(mapping.get("requested_artifacts"))
    supplied_requested_hash = mapping.get("requested_artifacts_sha256")
    _require_sha256(supplied_requested_hash, "job.requested_artifacts_sha256")
    if supplied_requested_hash != _requested_artifacts_sha256(requested):
        raise ContractError("job.requested_artifacts_sha256 does not match requested_artifacts")
    occurrences = mapping.get("occurrences")
    artifacts = mapping.get("artifacts")
    if not isinstance(occurrences, list) or not isinstance(artifacts, list):
        raise ContractError("job.occurrences and job.artifacts must be arrays")
    if any(not isinstance(item, Mapping) or "status" not in item for item in occurrences):
        raise ContractError("job.occurrences must carry an explicit lifecycle status")
    raw_extensions = mapping.get("extensions", {})
    _require_extensions(raw_extensions, "job.extensions")
    job = FrozenJob(
        schema_version=version,
        job_id=_require_string(mapping.get("job_id"), "job.job_id"),
        source_sha256=_require_sha256(mapping.get("source_sha256"), "job.source_sha256"),
        manifest_id=_require_string(mapping.get("manifest_id"), "job.manifest_id"),
        manifest_sha256=_require_sha256(mapping.get("manifest_sha256"), "job.manifest_sha256"),
        occurrence_ids=tuple(occurrence_ids),
        requested_artifacts=tuple(requested),
        occurrences=tuple(_copy(item) for item in occurrences),
        artifacts=tuple(_copy(item) for item in artifacts),
        application_plan_sha256=mapping.get("application_plan_sha256"),
        extensions=_copy(dict(raw_extensions)),
    )
    validated = _validate_job(job)
    supplied_status = mapping.get("status")
    if supplied_status is not None:
        _require_enum(supplied_status, frozenset(item.value for item in DeliveryStatus), "job.status")
        if supplied_status != validated.status:
            raise ContractError("job.status is caller-supplied but does not match derived completion")
    return validated


def dump_job(job: FrozenJob, path: str | Path) -> None:
    """Write a validated job with deterministic JSON formatting."""

    if not isinstance(job, FrozenJob):
        raise ContractError("dump_job requires a FrozenJob")
    validated = _validate_job(job)
    Path(path).write_text(_canonical_json(validated.to_dict()) + "\n", encoding="utf-8")


def _replace_job(job: FrozenJob, **changes: Any) -> FrozenJob:
    return _validate_job(dataclasses.replace(job, **changes))


def set_occurrence_status(job: FrozenJob, occurrence_id: str, status: str) -> FrozenJob:
    """Return a job with one explicit occurrence lifecycle status."""

    if not isinstance(job, FrozenJob):
        raise ContractError("set_occurrence_status requires a FrozenJob")
    _require_enum(status, KNOWN_OCCURRENCE_STATUSES, "status")
    if occurrence_id not in job.occurrence_ids:
        raise ContractError(f"unknown occurrence id {occurrence_id!r}")
    occurrences = []
    for row in job.occurrences:
        updated = _copy(row)
        if row["id"] == occurrence_id:
            updated["status"] = status
        occurrences.append(updated)
    return _replace_job(job, occurrences=tuple(occurrences))


def set_artifact_content(job: FrozenJob, artifact_id: str, content_sha256: str) -> FrozenJob:
    """Bind a candidate hash and stale any prior gate evidence for that artifact."""

    _require_sha256(content_sha256, "content_sha256")
    artifacts = []
    found = False
    for artifact in job.artifacts:
        updated = _copy(artifact)
        if artifact["id"] == artifact_id or artifact["logical_id"] == artifact_id:
            found = True
            updated["content_sha256"] = content_sha256
            updated["state"] = ArtifactState.STAGING.value
            for gate_name, gate in updated["gates"].items():
                if gate["state"] in {GateState.PASS.value, GateState.FAIL.value}:
                    gate["state"] = GateState.STALE.value
                    gate["reason"] = "candidate content changed; evidence must be regenerated"
            artifacts.append(updated)
        else:
            artifacts.append(updated)
    if not found:
        raise ContractError(f"unknown artifact id {artifact_id!r}")
    return _replace_job(job, artifacts=tuple(artifacts))


def bind_gate_evidence(
    job: FrozenJob,
    artifact_id: str,
    gate: str,
    state: str,
    *,
    evidence_id: str | None = None,
    tool: str = "unknown",
    tool_version: str = "unknown",
    environment: Mapping[str, Any] | None = None,
    details: Any = None,
    recorded_at: str | None = None,
    reason: str | None = None,
) -> FrozenJob:
    """Attach candidate-bound gate evidence to exactly one requested artifact."""

    _require_enum(gate, KNOWN_GATES, "gate")
    _require_enum(state, KNOWN_GATE_STATES, "state")
    _require_string(tool, "tool")
    _require_string(tool_version, "tool_version")
    artifacts = []
    found = False
    for artifact in job.artifacts:
        updated = _copy(artifact)
        if artifact["id"] == artifact_id or artifact["logical_id"] == artifact_id:
            found = True
            if gate not in updated["gates"]:
                raise ContractError(f"gate {gate!r} is not required for artifact {artifact['logical_id']!r}")
            if state == GateState.PASS.value and updated["content_sha256"] is None:
                raise ContractError("a gate cannot PASS before artifact content is hash-bound")
            gate_record: dict[str, Any] = {"state": state}
            if reason is not None:
                gate_record["reason"] = _require_string(reason, "reason")
            if state in {GateState.PASS.value, GateState.FAIL.value, GateState.STALE.value}:
                candidate_hash = updated["content_sha256"]
                if candidate_hash is None and state != GateState.STALE.value:
                    raise ContractError("gate evidence requires an artifact content hash")
                bound_hash = candidate_hash
                if state == GateState.STALE.value:
                    bound_hash = candidate_hash
                evidence = {
                    "evidence_id": evidence_id or f"evidence-{_digest({'job': job.job_id, 'artifact': updated['id'], 'gate': gate, 'state': state})[:32]}",
                    "source_sha256": job.source_sha256,
                    "manifest_id": job.manifest_id,
                    "manifest_sha256": job.manifest_sha256,
                    "job_id": job.job_id,
                    "artifact_id": updated["id"],
                    "artifact_sha256": bound_hash,
                    "tool": tool,
                    "tool_version": tool_version,
                }
                if environment is not None:
                    evidence["environment"] = _copy(dict(_require_mapping(environment, "environment")))
                if details is not None:
                    _canonical_json(details)
                    evidence["details"] = _copy(details)
                if recorded_at is not None:
                    evidence["recorded_at"] = _require_string(recorded_at, "recorded_at")
                gate_record["evidence"] = evidence
            updated["gates"][gate] = gate_record
        artifacts.append(updated)
    if not found:
        raise ContractError(f"unknown artifact id {artifact_id!r}")
    return _replace_job(job, artifacts=tuple(artifacts))


def verify_frozen_job(
    job: FrozenJob,
    manifest: Manifest,
    current_source_sha256: str,
    requested_artifacts: Sequence[str | Mapping[str, Any]] | None = None,
) -> None:
    """Verify that a frozen job can still consume the supplied inputs.

    A changed source, manifest identity, or requested deliverable set requires
    an explicit new freeze/review cycle.
    """

    if not isinstance(job, FrozenJob) or not isinstance(manifest, Manifest):
        raise ContractError("verify_frozen_job requires a FrozenJob and Manifest")
    _validate_job(job)
    manifest = load_manifest(manifest.to_dict())
    current_source_sha256 = _require_sha256(current_source_sha256, "current_source_sha256")
    if current_source_sha256 != job.source_sha256:
        raise ContractError("frozen job source_sha256 does not match the current source; rebuild is required")
    if manifest.manifest_id != job.manifest_id or manifest.manifest_sha256 != job.manifest_sha256:
        raise ContractError("frozen job manifest identity is stale; rebuild/re-approve the manifest")
    if tuple(row["id"] for row in manifest.formulas) != job.occurrence_ids:
        raise ContractError("frozen job occurrence IDs no longer match the manifest")
    if requested_artifacts is not None:
        requested = _normalize_requested_artifacts(requested_artifacts)
        if _requested_artifacts_sha256(requested) != job.requested_artifacts_sha256:
            raise ContractError("frozen requested artifact set changed; rebuild is required")


__all__ = [
    "ArtifactState",
    "Confidence",
    "ContractError",
    "DEFAULT_REQUIRED_GATES",
    "DeliveryStatus",
    "Gate",
    "GateState",
    "Manifest",
    "OccurrenceStatus",
    "SourceType",
    "FrozenJob",
    "bind_gate_evidence",
    "dump_manifest",
    "dump_job",
    "derive_job_status",
    "deterministic_occurrence_id",
    "freeze_job",
    "load_job",
    "load_manifest",
    "set_artifact_content",
    "set_occurrence_status",
    "verify_frozen_job",
]
