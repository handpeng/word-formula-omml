"""Evidence-backed formula recovery and canonical IR construction.

Recovery is intentionally independent from Word XML and OMML.  It converts a
candidate text span into a traceable normalized LaTeX representation and a
canonical semantic value, or returns an explicit review/refusal result.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from word_formula_omml.canonical import CanonicalError, canonicalize_formula
from word_formula_omml.contract import (
    Confidence,
    ContractError,
    Manifest,
    OccurrenceStatus,
    SourceType,
    load_manifest,
)


class RecoveryError(ContractError):
    """Raised for malformed recovery input rather than uncertain mathematics."""


EVIDENCE_RANKS = {
    "author_approved": 1,
    "manifest": 1,
    "user_instruction": 1,
    "original_latex": 2,
    "original_tex": 2,
    "latex": 2,
    "authoritative_pdf": 3,
    "pdf": 3,
    "render": 3,
    "trusted_manuscript": 4,
    "manuscript": 4,
    "word_context": 5,
    "context": 5,
    "heuristic": 6,
    "normalization": 6,
}

_SOURCE_TO_EVIDENCE = {
    SourceType.RAW_LATEX.value: "original_latex",
    SourceType.PLAIN_MATH.value: "word_context",
    SourceType.UNICODE_MATH.value: "word_context",
}

_LOST_ESCAPE_RE = re.compile(r"(?<!\\)\b(?:frac|sqrt|mathcal|mathrm|mathbf|boldsymbol|text)\s*\{")
_CORRUPTION_REPLACEMENTS = {
    "â‰¤": "≤",
    "â‰¥": "≥",
    "âˆ’": "−",
    "Â±": "±",
    "âˆž": "∞",
    "Ã—": "×",
    "Ã·": "÷",
}
_DELIMITERS = (("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)"))
_UNICODE_LATEX = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "θ": r"\theta",
    "λ": r"\lambda",
    "μ": r"\mu",
    "π": r"\pi",
    "σ": r"\sigma",
    "φ": r"\phi",
    "ω": r"\omega",
    "≤": r"\le",
    "≥": r"\ge",
    "≠": r"\ne",
    "≈": r"\approx",
    "±": r"\pm",
    "∓": r"\mp",
    "×": r"\times",
    "·": r"\cdot",
    "−": "-",
    "∈": r"\in",
}


@dataclass(frozen=True)
class Transformation:
    """One raw-to-normalized change with its rule and evidence basis."""

    rule: str
    before: str
    after: str
    reason: str
    evidence_source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "evidence_source": self.evidence_source,
        }


@dataclass(frozen=True)
class _EvidenceCandidate:
    source: str
    rank: int
    text: str
    approved: bool
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryResult:
    """The complete, serializable outcome for one occurrence."""

    raw_source: str
    source_type: str
    normalized_latex: str | None
    canonical: dict | None
    evidence: tuple[dict[str, Any], ...]
    transformations: tuple[dict[str, str], ...]
    ambiguity: tuple[str, ...]
    confidence: str
    status: str
    reason: str
    auto_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "raw_source": self.raw_source,
            "source_type": self.source_type,
            "normalized_latex": self.normalized_latex,
            "canonical": copy.deepcopy(self.canonical),
            "evidence": copy.deepcopy(list(self.evidence)),
            "transformations": copy.deepcopy(list(self.transformations)),
            "ambiguity": list(self.ambiguity),
            "confidence": self.confidence,
            "status": self.status,
            "reason": self.reason,
            "auto_eligible": self.auto_eligible,
        }
        return value


def _infer_source_type(text: str) -> str:
    if any(value in text for value in _CORRUPTION_REPLACEMENTS):
        return SourceType.CORRUPTED_TEXT.value
    if _LOST_ESCAPE_RE.search(text):
        return SourceType.PARTIAL_LATEX.value
    if "\\" in text:
        return SourceType.RAW_LATEX.value if _balanced_braces(text) else SourceType.PARTIAL_LATEX.value
    if any(character in text for character in _UNICODE_LATEX):
        return SourceType.UNICODE_MATH.value
    if re.search(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+|\^\{?[^\s}]+\}?)+", text):
        return SourceType.PLAIN_MATH.value
    if re.search(r"(?:>=|<=|\+/-|\^)", text):
        return SourceType.PLAIN_MATH.value
    if re.fullmatch(r"\s*[\(\[]\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\)\]]\s*", text):
        return SourceType.PLAIN_MATH.value
    return SourceType.UNKNOWN_FORMULA.value


def _balanced_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for character in text:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _has_math_signal(text: str) -> bool:
    """Require visible syntax before treating a bare word as Word math."""

    return bool(
        re.search(
            r"(?:\\[A-Za-z]+|[\^_]|\+/-|<=|>=|!=|[≤≥≠±∓×·−∈]|[+*/=<>-]|[\(\[\{].*[\)\]}])",
            text,
        )
    )


def _normalize_evidence(value: Any) -> list[_EvidenceCandidate]:
    if value is None:
        return []
    items: Iterable[Any]
    if isinstance(value, Mapping):
        items = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        raise RecoveryError("evidence must be an object or array of objects")
    result: list[_EvidenceCandidate] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, Mapping):
            raise RecoveryError(f"evidence[{index}] must be an object")
        source = item.get("source", item.get("kind", item.get("type")))
        if not isinstance(source, str) or source not in EVIDENCE_RANKS:
            raise RecoveryError(f"evidence[{index}].source must name a supported evidence source")
        text = item.get("latex", item.get("normalized_latex", item.get("text", item.get("value"))))
        if not isinstance(text, str) or not text.strip():
            raise RecoveryError(f"evidence[{index}] must contain non-empty latex/text")
        approved = item.get("approved", source in {"author_approved", "manifest", "user_instruction"}) is True
        if source in {"author_approved", "manifest"} and not approved:
            raise RecoveryError(f"evidence[{index}] author-approved evidence must set approved=true")
        result.append(_EvidenceCandidate(source, EVIDENCE_RANKS[source], text, approved, dict(item)))
    return result


def _strip_math_delimiters(value: str, *, evidence_source: str, transformations: list[Transformation]) -> str:
    result = value.strip()
    for left, right in _DELIMITERS:
        starts = result.startswith(left)
        ends = result.endswith(right)
        if starts or ends:
            if not (starts and ends) or len(result) <= len(left) + len(right):
                raise RecoveryError("unbalanced math delimiters")
            before = result
            result = result[len(left) : -len(right)].strip()
            transformations.append(Transformation("strip_math_delimiters", before, result, "balanced delimiters identify the formula span", evidence_source))
            return result
    return result


def _normalize_text(
    raw: str,
    *,
    source_type: str,
    evidence_source: str,
    context: Mapping[str, Any],
) -> tuple[str, list[Transformation]]:
    transformations: list[Transformation] = []
    value = _strip_math_delimiters(raw, evidence_source=evidence_source, transformations=transformations)

    if any(item in value for item in _CORRUPTION_REPLACEMENTS):
        if not (context.get("math_intent") is True and context.get("allow_corruption_repair") is True):
            raise RecoveryError("corruption requires explicit math context and repair authorization")
        for source, replacement in _CORRUPTION_REPLACEMENTS.items():
            if source in value:
                before = value
                value = value.replace(source, replacement)
                transformations.append(Transformation("repair_mojibake", before, value, "context-gated known mathematical encoding repair", evidence_source))

    if _LOST_ESCAPE_RE.search(value):
        if not (context.get("math_intent") is True and context.get("allow_lost_escape") is True):
            raise RecoveryError("lost LaTeX escape requires explicit mathematical context")
        before = value
        value = re.sub(r"(?<!\\)\b(frac|sqrt|mathcal|mathrm|mathbf|boldsymbol|text)(?=\s*\{)", r"\\\1", value)
        transformations.append(Transformation("restore_lost_escape", before, value, "context-gated command repair", evidence_source))

    for source, replacement in (
        ("+/-", r"\pm"),
        (">=", r"\ge"),
        ("<=", r"\le"),
        ("!=", r"\ne"),
    ):
        if source in value:
            before = value
            value = value.replace(source, replacement)
            transformations.append(Transformation("normalize_operator", before, value, f"normalize operator spelling {source!r}", evidence_source))

    for source, replacement in _UNICODE_LATEX.items():
        if source in value:
            before = value
            value = value.replace(source, replacement)
            transformations.append(Transformation("normalize_unicode_symbol", before, value, f"normalize Unicode mathematical symbol {source}", evidence_source))

    scientific = re.compile(r"(?P<base>\b\d+(?:\.\d+)?)\s*\^\s*(?P<exponent>-?\d+)\b")
    match = scientific.search(value)
    while match:
        before = value
        replacement = f"{match.group('base')}^{{{match.group('exponent')}}}"
        value = value[: match.start()] + replacement + value[match.end() :]
        transformations.append(Transformation("group_scientific_exponent", before, value, "group exponent so its sign and digits remain one operand", evidence_source))
        match = scientific.search(value, match.start() + len(replacement))

    value = re.sub(r"\s+", " ", value).strip()
    return value, transformations


def _evidence_record(candidate: _EvidenceCandidate, *, selected: bool, normalized: str | None, error: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": candidate.source,
        "rank": candidate.rank,
        "approved": candidate.approved,
        "selected": selected,
        "input": candidate.text,
    }
    if normalized is not None:
        record["normalized_latex"] = normalized
    if error is not None:
        record["outcome"] = "REJECTED"
        record["reason"] = error
    else:
        record["outcome"] = "ACCEPTED"
    extra = {key: copy.deepcopy(value) for key, value in candidate.details.items() if key not in {"source", "kind", "type", "latex", "normalized_latex", "text", "value", "approved"}}
    if extra:
        record["details"] = extra
    return record


def _result(
    *,
    raw_source: str,
    source_type: str,
    normalized: str | None,
    canonical: dict | None,
    evidence: list[dict[str, Any]],
    transformations: list[Transformation],
    ambiguity: list[str],
    confidence: str,
    status: str,
    reason: str,
    auto_eligible: bool,
) -> RecoveryResult:
    return RecoveryResult(
        raw_source=raw_source,
        source_type=source_type,
        normalized_latex=normalized,
        canonical=canonical,
        evidence=tuple(evidence),
        transformations=tuple(item.to_dict() for item in transformations),
        ambiguity=tuple(dict.fromkeys(ambiguity)),
        confidence=confidence,
        status=status,
        reason=reason,
        auto_eligible=auto_eligible,
    )


def recover_formula(
    raw_source: str,
    *,
    source_type: str | None = None,
    evidence: Any = None,
    context: Mapping[str, Any] | None = None,
    layout: str = "inline",
) -> RecoveryResult:
    """Recover one formula or return a review/refusal result.

    ``context`` must explicitly opt into heuristic repairs that could affect
    prose or corrupted source.  The function never silently promotes an
    uncertain result to an approved one.
    """

    if not isinstance(raw_source, str) or not raw_source.strip():
        raise RecoveryError("raw_source must be a non-empty string")
    if layout not in {"inline", "display"}:
        raise RecoveryError("layout must be 'inline' or 'display'")
    context = dict(context or {})
    detected = source_type or _infer_source_type(raw_source)
    if detected not in {item.value for item in SourceType}:
        raise RecoveryError(f"unsupported source_type {detected!r}")
    explicit = _normalize_evidence(evidence)

    if detected == SourceType.EXISTING_OMML.value:
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=None,
            canonical=None,
            evidence=[],
            transformations=[],
            ambiguity=["existing_native_equation_is_out_of_scope_for_recovery"],
            confidence=Confidence.REVIEW_REQUIRED.value,
            status=OccurrenceStatus.PRESERVED.value,
            reason="existing_native_equation_preserved",
            auto_eligible=False,
        )
    if context.get("structural_refusal") is True:
        reason = str(context.get("structural_reason") or "structural_handler_required")
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=None,
            canonical=None,
            evidence=[],
            transformations=[],
            ambiguity=[reason],
            confidence=Confidence.REVIEW_REQUIRED.value,
            status=OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
            reason=reason,
            auto_eligible=False,
        )
    if detected in {SourceType.EQ_FIELD.value, SourceType.EMBEDDED_EQUATION_OBJECT.value}:
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=None,
            canonical=None,
            evidence=[],
            transformations=[],
            ambiguity=["legacy_equation_requires_special_handler"],
            confidence=Confidence.REVIEW_REQUIRED.value,
            status=OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
            reason="legacy_equation_requires_special_handler",
            auto_eligible=False,
        )
    if detected == SourceType.UNKNOWN_FORMULA.value and not explicit:
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=None,
            canonical=None,
            evidence=[],
            transformations=[],
            ambiguity=["unsupported_or_non_formula_text"],
            confidence=Confidence.UNRECOVERABLE.value,
            status=OccurrenceStatus.REFUSED.value,
            reason="unsupported_or_non_formula_text",
            auto_eligible=False,
        )

    candidates: list[tuple[_EvidenceCandidate, str, list[Transformation], dict]] = []
    evidence_records: list[dict[str, Any]] = []
    built_in: _EvidenceCandidate | None = None
    if detected != SourceType.UNKNOWN_FORMULA.value:
        source_evidence_name = _SOURCE_TO_EVIDENCE.get(detected, "heuristic")
        built_in = _EvidenceCandidate(source_evidence_name, EVIDENCE_RANKS[source_evidence_name], raw_source, False, {"basis": "inventory_source"})
    all_candidates = [*explicit, *([built_in] if built_in is not None else [])]
    errors: dict[int, str] = {}
    for index, candidate in enumerate(all_candidates):
        candidate_context = dict(context)
        # External evidence is already a statement of mathematical intent;
        # its own command syntax is safe to normalize without treating the
        # damaged Word spelling as proof.
        if candidate is not built_in:
            candidate_context.setdefault("math_intent", True)
            candidate_context.setdefault("allow_lost_escape", True)
            candidate_context.setdefault("allow_corruption_repair", True)
        try:
            if candidate is built_in and not _has_math_signal(raw_source) and context.get("math_intent") is not True:
                raise RecoveryError("bare prose token is not sufficient evidence of mathematical intent")
            normalized, transformations = _normalize_text(
                candidate.text,
                source_type=detected,
                evidence_source=candidate.source,
                context=candidate_context,
            )
            canonical = canonicalize_formula(normalized, source_type=detected)
            candidates.append((candidate, normalized, transformations, canonical))
            evidence_records.append(_evidence_record(candidate, selected=False, normalized=normalized))
        except (CanonicalError, RecoveryError) as error:
            errors[index] = str(error)
            evidence_records.append(_evidence_record(candidate, selected=False, normalized=None, error=str(error)))

    if not candidates:
        if detected == SourceType.PARTIAL_LATEX.value and _LOST_ESCAPE_RE.search(raw_source):
            ambiguity = ["repair_requires_authoritative_evidence"]
        elif detected in {SourceType.PARTIAL_LATEX.value, SourceType.CORRUPTED_TEXT.value}:
            ambiguity = ["malformed_or_corrupted_source_requires_authoritative_evidence"]
        else:
            ambiguity = ["no_supported_recovery"]
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=None,
            canonical=None,
            evidence=evidence_records,
            transformations=[],
            ambiguity=ambiguity,
            confidence=Confidence.REVIEW_REQUIRED.value if detected != SourceType.UNKNOWN_FORMULA.value else Confidence.UNRECOVERABLE.value,
            status=OccurrenceStatus.NEEDS_REVIEW.value,
            reason=ambiguity[0],
            auto_eligible=False,
        )

    candidates.sort(key=lambda item: (item[0].rank, item[0].source, item[1]))
    selected_candidate, normalized, transformations, canonical = candidates[0]
    if selected_candidate is not built_in and selected_candidate.text != raw_source:
        transformations = [
            Transformation(
                "adopt_evidence_representation",
                raw_source,
                selected_candidate.text,
                "higher-ranked evidence supplies the mathematical source representation",
                selected_candidate.source,
            ),
            *transformations,
        ]
    distinct = {(item[1], repr(item[3])) for item in candidates}
    ambiguity: list[str] = []
    if len(distinct) > 1:
        ambiguity.append("evidence_conflict")
        # A conflict is review-required even when the selected source ranks
        # above the alternative.  Lower-ranked evidence is never an override.
    failed_explicit = [
        (all_candidates[index], message)
        for index, message in errors.items()
        if index < len(explicit)
    ]
    if failed_explicit:
        # An unusable higher-ranked source cannot be hidden by a lower-ranked
        # fallback.  The selected value remains inspectable, but is review-only.
        highest_failed_rank = min(candidate.rank for candidate, _message in failed_explicit)
        if highest_failed_rank <= selected_candidate.rank:
            ambiguity.append("higher_ranked_evidence_unusable")
    if errors and selected_candidate is built_in and detected in {SourceType.PARTIAL_LATEX.value, SourceType.CORRUPTED_TEXT.value}:
        ambiguity.append("lower_ranked_repair_requires_explicit_evidence")

    for index, record in enumerate(evidence_records):
        if index < len(all_candidates):
            record["selected"] = all_candidates[index] is selected_candidate

    if ambiguity:
        return _result(
            raw_source=raw_source,
            source_type=detected,
            normalized=normalized,
            canonical=canonical,
            evidence=evidence_records,
            transformations=transformations,
            ambiguity=ambiguity,
            confidence=Confidence.REVIEW_REQUIRED.value,
            status=OccurrenceStatus.NEEDS_REVIEW.value,
            reason=ambiguity[0],
            auto_eligible=False,
        )

    explicit_selected = selected_candidate is not built_in
    confidence = Confidence.AUTHORITATIVE.value if explicit_selected and selected_candidate.rank == 1 and selected_candidate.approved else Confidence.HIGH.value
    auto_eligible = confidence in {Confidence.AUTHORITATIVE.value, Confidence.HIGH.value}
    if detected in {SourceType.PARTIAL_LATEX.value, SourceType.CORRUPTED_TEXT.value} and not explicit_selected:
        explicitly_gated = context.get("math_intent") is True and (
            context.get("allow_lost_escape") is True or context.get("allow_corruption_repair") is True
        )
        if not explicitly_gated:
            return _result(
                raw_source=raw_source,
                source_type=detected,
                normalized=normalized,
                canonical=canonical,
                evidence=evidence_records,
                transformations=transformations,
                ambiguity=["repair_requires_authoritative_evidence"],
                confidence=Confidence.REVIEW_REQUIRED.value,
                status=OccurrenceStatus.NEEDS_REVIEW.value,
                reason="repair_requires_authoritative_evidence",
                auto_eligible=False,
            )

    status = OccurrenceStatus.APPROVED.value if confidence == Confidence.AUTHORITATIVE.value else OccurrenceStatus.RECOVERED.value
    return _result(
        raw_source=raw_source,
        source_type=detected,
        normalized=normalized,
        canonical=canonical,
        evidence=evidence_records,
        transformations=transformations,
        ambiguity=[],
        confidence=confidence,
        status=status,
        reason="authoritative_recovery" if confidence == Confidence.AUTHORITATIVE.value else "deterministic_supported_recovery",
        auto_eligible=auto_eligible,
    )


def _manifest_evidence(evidence: Any, occurrence_id: str) -> Any:
    if evidence is None:
        return None
    if isinstance(evidence, Mapping) and occurrence_id in evidence:
        return evidence[occurrence_id]
    return evidence


def _manifest_context(contexts: Any, occurrence_id: str) -> Mapping[str, Any] | None:
    if contexts is None:
        return None
    if isinstance(contexts, Mapping) and occurrence_id in contexts:
        value = contexts[occurrence_id]
        if value is None or isinstance(value, Mapping):
            return value
        raise RecoveryError(f"context for {occurrence_id} must be an object")
    if isinstance(contexts, Mapping):
        return contexts
    raise RecoveryError("contexts must be an object")


def recover_manifest(
    manifest: Manifest | Mapping[str, Any] | str,
    *,
    evidence: Mapping[str, Any] | Any = None,
    contexts: Mapping[str, Any] | None = None,
) -> Manifest:
    """Recover all eligible inventory rows while preserving occurrence IDs.

    Structural/native rows remain explicitly preserved or special-handler
    rows.  No row is silently dropped, and the shared W0 loader validates the
    resulting manifest before it is returned.
    """

    current = manifest if isinstance(manifest, Manifest) else load_manifest(manifest)
    rows: list[dict[str, Any]] = []
    for original in current.formulas:
        row = copy.deepcopy(original)
        raw = row.get("raw_source", row.get("source", row.get("latex", "")))
        existing_status = row.get("status")
        recovery_context = _manifest_context(contexts, row["id"])
        if existing_status in {
            OccurrenceStatus.PRESERVED.value,
            OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        } or row.get("source_type") in {
            SourceType.EXISTING_OMML.value,
            SourceType.EQ_FIELD.value,
            SourceType.EMBEDDED_EQUATION_OBJECT.value,
        }:
            structural_context = dict(recovery_context or {})
            if existing_status == OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value and row.get("source_type") not in {
                SourceType.EXISTING_OMML.value,
                SourceType.EQ_FIELD.value,
                SourceType.EMBEDDED_EQUATION_OBJECT.value,
            }:
                structural_context.update(
                    {
                        "structural_refusal": True,
                        "structural_reason": row.get("extensions", {})
                        .get("inventory", {})
                        .get("status_reason", "structural_handler_required"),
                    }
                )
            result = recover_formula(raw, source_type=row.get("source_type"), context=structural_context)
        else:
            result = recover_formula(
                raw,
                source_type=row.get("source_type"),
                evidence=_manifest_evidence(evidence, row["id"]),
                context=recovery_context,
                layout=row.get("layout", "inline"),
            )
        if result.normalized_latex is not None:
            row["normalized_latex"] = result.normalized_latex
        else:
            row.pop("normalized_latex", None)
        if result.canonical is not None:
            row["canonical"] = copy.deepcopy(result.canonical)
        else:
            row.pop("canonical", None)
        row["evidence"] = copy.deepcopy(list(result.evidence))
        row["ambiguity"] = list(result.ambiguity)
        row["confidence"] = result.confidence
        row["target_layout"] = row.get("target_layout", row.get("layout", "inline"))
        row["extensions"] = copy.deepcopy(row.get("extensions", {}))
        row["extensions"]["recovery"] = {
            "reason": result.reason,
            "transformations": list(result.transformations),
            "auto_eligible": result.auto_eligible,
            "source_sha256": current.source_sha256,
        }
        if existing_status not in {
            OccurrenceStatus.PRESERVED.value,
            OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        }:
            row["status"] = result.status
        rows.append(row)
    data: dict[str, Any] = {
        "schema_version": current.schema_version,
        "source_sha256": current.source_sha256,
        "revision_author": current.revision_author,
        "formulas": rows,
        "extensions": copy.deepcopy(dict(current.extensions)),
    }
    data = {key: value for key, value in data.items() if value is not None}
    return load_manifest(data)


def recovery_fingerprint(result: RecoveryResult) -> str:
    """Return a stable digest for a result, useful for frozen retry records."""

    payload = result.to_dict()
    import json

    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "EVIDENCE_RANKS",
    "RecoveryError",
    "RecoveryResult",
    "Transformation",
    "recover_formula",
    "recover_manifest",
    "recovery_fingerprint",
]
