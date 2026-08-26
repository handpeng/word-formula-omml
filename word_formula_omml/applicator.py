"""Fail-closed application of reviewed formula templates to DOCX packages.

The applicator deliberately has a small automatic surface: an occurrence must
be approved, semantically backed by a W3B template, style-resolved by W4, and
contained by one ordinary text run.  Everything else is recorded as an
explicit refusal in the frozen application plan.

Application produces staging candidates only.  Promotion is a separate
transaction which accepts only a #2 job whose derived status is ``COMPLETE``
and whose every requested artifact has candidate-bound passing evidence.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from word_formula_omml.contract import (
    ArtifactState,
    ContractError,
    DeliveryStatus,
    FrozenJob,
    GateState,
    Manifest,
    OccurrenceStatus,
    SUCCESSFUL_OCCURRENCE_STATUSES,
    SourceType,
    TERMINAL_OCCURRENCE_STATUSES,
    freeze_job,
    load_job,
    load_manifest,
    set_artifact_content,
    verify_frozen_job,
)
from word_formula_omml.style import HIGHLIGHTS, MATH_FONT, UNDERLINES


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
XML = "http://www.w3.org/XML/1998/namespace"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

ET.register_namespace("w", W)
ET.register_namespace("m", M)
ET.register_namespace("r", R)

SESSION_AUTHOR = "Codex Formula Remediation"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX_COLOR = re.compile(r"^(?:[0-9a-fA-F]{6}|auto)$", re.IGNORECASE)
_WORD_SIZE = re.compile(r"^[1-9][0-9]{0,4}$")
_PROTECTED_FIELDS = (
    "table",
    "bookmark",
    "comment_range",
    "hyperlink",
    "field",
    "drawing",
    "content_control",
    "embedded_object",
)
_ADJACENT_FIELDS = ("adjacent_bookmark", "adjacent_field", "adjacent_hyperlink", "adjacent_drawing")
_TRACKED_REVISION_LOCALS = frozenset({"ins", "del", "pPrChange", "rPrChange"})
_SESSION_REVISION_LOCALS = frozenset({"ins", "del"})
_SPECIAL_SOURCE_TYPES = {
    SourceType.EXISTING_OMML.value,
    SourceType.EQ_FIELD.value,
    SourceType.EMBEDDED_EQUATION_OBJECT.value,
    SourceType.UNKNOWN_FORMULA.value,
}
_UNSAFE_ANCESTORS = {
    "tbl",
    "hyperlink",
    "fldSimple",
    "fldChar",
    "sdt",
    "object",
    "drawing",
    "ins",
    "del",
}


class ApplicationError(ContractError):
    """Raised when an application or promotion safety condition fails."""


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _w(local: str) -> str:
    return _q(W, local)


def _m(local: str) -> str:
    return _q(M, local)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else str(tag)


def _namespace(tag: str) -> str | None:
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def _attribute(node: ET.Element, namespace: str, local: str) -> str | None:
    return node.get(_q(namespace, local)) or node.get(local)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ApplicationError(f"value is not deterministic JSON: {error}") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ApplicationError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


def _safe_path(value: str | Path, field_name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = path.resolve()
    else:
        path = path.resolve()
    if os.path.islink(path):
        raise ApplicationError(f"{field_name} must not be a symbolic link")
    return path


@dataclass
class _Package:
    source_bytes: bytes
    names: tuple[str, ...]
    infos: dict[str, zipfile.ZipInfo]
    parts: dict[str, bytes]
    roots: dict[str, ET.Element]


def _package_from_bytes(source_bytes: bytes, label: str) -> _Package:
    if not isinstance(source_bytes, bytes):
        raise ApplicationError(f"{label} must be bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ApplicationError("DOCX contains duplicate package part names")
            for name in names:
                components = name.split("/")
                if not name or name.startswith("/") or any(component in {"", ".", ".."} for component in components):
                    raise ApplicationError(f"unsafe package part name: {name!r}")
            bad_name = archive.testzip()
            if bad_name is not None:
                raise ApplicationError(f"DOCX package CRC check failed for {bad_name}")
            parts = {name: archive.read(name) for name in names}
            info_map = {info.filename: copy.copy(info) for info in infos}
    except ApplicationError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise ApplicationError(f"{label} is not a readable DOCX ZIP package: {error}") from error

    roots: dict[str, ET.Element] = {}
    for name, data in parts.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            roots[name] = ET.fromstring(data)
        except (ET.ParseError, ValueError) as error:
            raise ApplicationError(f"cannot parse XML package part {name} in {label}: {error}") from error
    return _Package(source_bytes, tuple(names), info_map, parts, roots)


def _read_package(path: str | Path) -> _Package:
    source_path = _safe_path(path, "source")
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise ApplicationError(f"cannot read source DOCX {source_path}: {error}") from error
    return _package_from_bytes(source_bytes, f"source {source_path}")


def _serialize_package(package: _Package) -> bytes:
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w") as archive:
            for name in package.names:
                info = copy.copy(package.infos[name])
                archive.writestr(info, package.parts[name])
    except (OSError, RuntimeError, ValueError) as error:
        raise ApplicationError(f"cannot serialize DOCX staging package: {error}") from error
    return output.getvalue()


def _block_elements(root: ET.Element) -> list[ET.Element]:
    return [node for node in root.iter() if node.tag in {_w("p"), _m("oMathPara")}]


def _parent_map(root: ET.Element) -> dict[int, ET.Element]:
    return {id(child): parent for parent in root.iter() for child in list(parent)}


def _root_story(root: ET.Element) -> str:
    return {
        "document": "main",
        "hdr": "header",
        "ftr": "footer",
        "footnotes": "footnote",
        "endnotes": "endnote",
        "comments": "comment",
    }.get(_local(root.tag), "unknown")


def _revision_id_values(root: ET.Element) -> list[int]:
    values = []
    for node in root.iter():
        value = _attribute(node, W, "id")
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            values.append(parsed)
    return values


def _max_revision_id(package: _Package) -> int:
    return max((value for root in package.roots.values() for value in _revision_id_values(root)), default=0)


def _visible_text_and_target(
    paragraph: ET.Element,
    target_run: ET.Element,
    target_text: ET.Element,
    local_start: int,
    local_end: int,
) -> tuple[str, int, int]:
    text_parts: list[str] = []
    offset = 0
    target_start: int | None = None
    target_end: int | None = None

    def visit(node: ET.Element, deleted: bool = False) -> None:
        nonlocal offset, target_start, target_end
        is_deleted = deleted or node.tag == _w("del")
        if node.tag == _w("del") and not deleted:
            return
        if node.tag == _w("delText"):
            return
        if node.tag == _w("t"):
            value = node.text or ""
            if not is_deleted:
                if node is target_text and target_start is None:
                    target_start = offset + local_start
                    target_end = offset + local_end
                text_parts.append(value)
                offset += len(value)
            return
        for child in list(node):
            visit(child, is_deleted)

    visit(paragraph)
    if target_start is None or target_end is None:
        raise ApplicationError("target run is not in the current accepted text surface")
    return "".join(text_parts), target_start, target_end


def _row_location(row: Mapping[str, Any], package: _Package) -> tuple[ET.Element, ET.Element, ET.Element, ET.Element, int, int, int]:
    package_part = row.get("package_part", "word/document.xml")
    if not isinstance(package_part, str) or package_part not in package.roots:
        raise _EligibilityRefusal("source_package_part_missing", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    root = package.roots[package_part]
    expected_story = row.get("story")
    if expected_story is not None and expected_story != _root_story(root):
        raise _EligibilityRefusal("source_story_mismatch", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    paragraph_number = row.get("paragraph")
    if isinstance(paragraph_number, bool) or not isinstance(paragraph_number, int) or paragraph_number < 1:
        raise _EligibilityRefusal("paragraph_anchor_missing", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    blocks = _block_elements(root)
    if paragraph_number > len(blocks) or blocks[paragraph_number - 1].tag != _w("p"):
        raise _EligibilityRefusal("paragraph_anchor_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    paragraph = blocks[paragraph_number - 1]
    parents = _parent_map(root)
    runs = paragraph.findall(".//w:r", {"w": W})
    run_index = row.get("run_index")
    if isinstance(run_index, bool) or not isinstance(run_index, int) or run_index < 1 or run_index > len(runs):
        raise _EligibilityRefusal("run_anchor_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    run = runs[run_index - 1]
    if parents.get(id(run)) is not paragraph:
        raise _EligibilityRefusal("run_is_not_an_ordinary_paragraph_child", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    for ancestor in _ancestors(run, parents):
        if _local(ancestor.tag) in _UNSAFE_ANCESTORS:
            raise _EligibilityRefusal("protected_or_revision_intersection", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)

    direct_children = list(run)
    text_nodes = [child for child in direct_children if child.tag == _w("t")]
    if len(text_nodes) != 1 or any(child.tag not in {_w("rPr"), _w("t")} for child in direct_children):
        raise _EligibilityRefusal("run_contains_non_text_structure", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    text_node = text_nodes[0]
    run_value = text_node.text or ""
    run_start = row.get("run_start")
    run_end = row.get("run_end")
    if (
        isinstance(run_start, bool)
        or isinstance(run_end, bool)
        or not isinstance(run_start, int)
        or not isinstance(run_end, int)
        or run_start < 0
        or run_end <= run_start
        or run_end > len(run_value)
    ):
        raise _EligibilityRefusal("run_offset_missing_or_invalid", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    source = row.get("raw_source", row.get("source"))
    if not isinstance(source, str) or not source:
        raise _EligibilityRefusal("raw_source_missing", OccurrenceStatus.NEEDS_REVIEW.value)
    if run_value[run_start:run_end] != source:
        raise _EligibilityRefusal("source_run_text_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)

    boundaries = row.get("run_boundaries")
    if not isinstance(boundaries, Mapping) or boundaries.get("run_count") != 1:
        raise _EligibilityRefusal("multi_run_occurrence", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    boundary_runs = boundaries.get("runs")
    if not isinstance(boundary_runs, list) or len(boundary_runs) != 1 or not isinstance(boundary_runs[0], Mapping):
        raise _EligibilityRefusal("run_boundary_evidence_missing", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    boundary = boundary_runs[0]
    if boundary.get("index") != run_index or boundary.get("start") != run_start or boundary.get("end") != run_end:
        raise _EligibilityRefusal("run_boundary_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)

    visible_text, global_start, global_end = _visible_text_and_target(
        paragraph, run, text_node, run_start, run_end
    )
    if visible_text[global_start:global_end] != source:
        raise _EligibilityRefusal("accepted_text_anchor_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    expected_matches = row.get("expected_matches")
    if expected_matches != 1 or visible_text.count(source) != expected_matches:
        raise _EligibilityRefusal("expected_match_count_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    anchor_before = row.get("anchor_before")
    anchor_after = row.get("anchor_after")
    if anchor_before is not None and (not isinstance(anchor_before, str) or not visible_text[:global_start].endswith(anchor_before)):
        raise _EligibilityRefusal("before_anchor_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    if anchor_after is not None and (not isinstance(anchor_after, str) or not visible_text[global_end:].startswith(anchor_after)):
        raise _EligibilityRefusal("after_anchor_drift", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)

    protected_locals = {"bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "fldSimple", "drawing"}
    if any(_local(node.tag) in protected_locals for node in paragraph.iter()):
        raise _EligibilityRefusal("protected_or_adjacent_structure", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    if any(_local(node.tag) in {"ins", "del"} for node in paragraph.iter()):
        # A formula outside a revision may still be safe, but the fast path
        # refuses a paragraph with revision structure to avoid flattening views.
        raise _EligibilityRefusal("revision_structure_in_paragraph", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    return root, paragraph, run, text_node, global_start, global_end, run_index


def _ancestors(node: ET.Element, parents: Mapping[int, ET.Element]) -> list[ET.Element]:
    result = []
    current = parents.get(id(node))
    while current is not None:
        result.append(current)
        current = parents.get(id(current))
    return result


class _EligibilityRefusal(Exception):
    def __init__(self, reason: str, terminal_status: str):
        super().__init__(reason)
        self.reason = reason
        self.terminal_status = terminal_status


@dataclass(frozen=True)
class ApplicationAction:
    """One frozen occurrence decision and its evidence-bound action."""

    occurrence_id: str
    decision: str
    terminal_status: str
    reason: str
    package_part: str | None = None
    story: str | None = None
    paragraph: int | None = None
    run_index: int | None = None
    run_start: int | None = None
    run_end: int | None = None
    source: str | None = None
    layout: str | None = None
    template_sha256: str | None = None
    styled_template_sha256: str | None = None
    style: Mapping[str, Any] = field(default_factory=dict)
    deletion_revision_id: int | None = None
    insertion_revision_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "occurrence_id": self.occurrence_id,
            "decision": self.decision,
            "terminal_status": self.terminal_status,
            "reason": self.reason,
            "package_part": self.package_part,
            "story": self.story,
            "paragraph": self.paragraph,
            "run_index": self.run_index,
            "run_start": self.run_start,
            "run_end": self.run_end,
            "source": self.source,
            "layout": self.layout,
            "template_sha256": self.template_sha256,
            "styled_template_sha256": self.styled_template_sha256,
            "style": copy.deepcopy(dict(self.style)),
            "deletion_revision_id": self.deletion_revision_id,
            "insertion_revision_id": self.insertion_revision_id,
        }
        return result


@dataclass(frozen=True)
class ApplicationPlan:
    """Hash-bound dry-run plan used by the applicator and clean handler."""

    schema_version: int
    source_sha256: str
    manifest_id: str
    manifest_sha256: str
    revision_author: str
    max_existing_revision_id: int
    actions: tuple[ApplicationAction, ...]

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "revision_author": self.revision_author,
            "max_existing_revision_id": self.max_existing_revision_id,
            "actions": [action.to_dict() for action in self.actions],
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256(_canonical_json(self._identity_payload()).encode("utf-8"))

    @property
    def session_revision_ids(self) -> tuple[int, ...]:
        return tuple(
            revision_id
            for action in self.actions
            if action.decision == "APPLY"
            for revision_id in (action.deletion_revision_id, action.insertion_revision_id)
            if revision_id is not None
        )

    @property
    def applied_count(self) -> int:
        return sum(action.decision == "APPLY" for action in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), "plan_sha256": self.plan_sha256}


def _validate_plan(plan: ApplicationPlan) -> ApplicationPlan:
    if isinstance(plan.schema_version, bool) or plan.schema_version != 1:
        raise ApplicationError(f"unsupported application plan schema_version {plan.schema_version!r}")
    _require_sha256(plan.source_sha256, "plan.source_sha256")
    _require_sha256(plan.manifest_sha256, "plan.manifest_sha256")
    if not isinstance(plan.manifest_id, str) or not plan.manifest_id:
        raise ApplicationError("plan.manifest_id must be a non-empty string")
    if not isinstance(plan.revision_author, str) or not plan.revision_author.strip():
        raise ApplicationError("plan.revision_author must be a non-empty string")
    if isinstance(plan.max_existing_revision_id, bool) or not isinstance(plan.max_existing_revision_id, int) or plan.max_existing_revision_id < 0:
        raise ApplicationError("plan.max_existing_revision_id must be a non-negative integer")
    ids = [action.occurrence_id for action in plan.actions]
    if len(ids) != len(set(ids)):
        raise ApplicationError("application plan contains duplicate occurrence IDs")
    revision_ids = []
    for action in plan.actions:
        if not isinstance(action.occurrence_id, str) or not action.occurrence_id.strip():
            raise ApplicationError("application plan occurrence IDs must be non-empty strings")
        if not isinstance(action.terminal_status, str) or action.terminal_status not in TERMINAL_OCCURRENCE_STATUSES:
            raise ApplicationError(
                f"application plan action {action.occurrence_id} has a non-terminal status "
                f"{action.terminal_status!r}"
            )
        if not isinstance(action.reason, str) or not action.reason.strip():
            raise ApplicationError(f"application plan action {action.occurrence_id} has no refusal/action reason")
        if action.decision not in {"APPLY", "PRESERVE", "REFUSE"}:
            raise ApplicationError(f"unsupported application decision {action.decision!r}")
        if action.decision == "APPLY":
            if action.terminal_status != OccurrenceStatus.APPLIED.value:
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} must terminate as APPLIED")
            if action.deletion_revision_id is None or action.insertion_revision_id is None:
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} has no frozen revision IDs")
            _require_sha256(action.template_sha256, f"application plan template hash for {action.occurrence_id}")
            _require_sha256(
                action.styled_template_sha256,
                f"application plan styled template hash for {action.occurrence_id}",
            )
            if not isinstance(action.source, str) or not action.source:
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} has no frozen source text")
            if not isinstance(action.package_part, str) or not action.package_part:
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} has no frozen package part")
            for field_name in ("paragraph", "run_index", "run_start", "run_end"):
                value = getattr(action, field_name)
                minimum = 1 if field_name in {"paragraph", "run_index"} else 0
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                    raise ApplicationError(
                        f"eligible occurrence {action.occurrence_id} has invalid frozen {field_name}"
                    )
            if action.run_end <= action.run_start:
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} has an empty frozen range")
            if action.layout != "inline":
                raise ApplicationError(f"eligible occurrence {action.occurrence_id} has unsupported layout")
            revision_ids.extend((action.deletion_revision_id, action.insertion_revision_id))
        else:
            if action.decision == "PRESERVE" and action.terminal_status not in SUCCESSFUL_OCCURRENCE_STATUSES:
                raise ApplicationError(f"preserved occurrence {action.occurrence_id} has an unsafe terminal status")
            if action.decision == "REFUSE" and action.terminal_status in SUCCESSFUL_OCCURRENCE_STATUSES:
                raise ApplicationError(f"refused occurrence {action.occurrence_id} has a successful terminal status")
            if action.deletion_revision_id is not None or action.insertion_revision_id is not None:
                raise ApplicationError(f"refused occurrence {action.occurrence_id} carries revision IDs")
            if action.template_sha256 is not None or action.styled_template_sha256 is not None:
                raise ApplicationError(f"non-applying occurrence {action.occurrence_id} carries template identity")
        for revision_id in (action.deletion_revision_id, action.insertion_revision_id):
            if revision_id is not None and (
                isinstance(revision_id, bool) or not isinstance(revision_id, int) or revision_id < 0
            ):
                raise ApplicationError(f"application plan revision ID for {action.occurrence_id} is invalid")
    if len(revision_ids) != len(set(revision_ids)) or any(value <= plan.max_existing_revision_id for value in revision_ids):
        raise ApplicationError("application plan revision IDs are not unique and above the source baseline")
    return plan


def load_application_plan(data_or_path: Mapping[str, Any] | str | Path) -> ApplicationPlan:
    if isinstance(data_or_path, (str, Path)):
        try:
            data = json.loads(Path(data_or_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApplicationError(f"cannot load application plan: {error}") from error
    else:
        data = data_or_path
    if not isinstance(data, Mapping):
        raise ApplicationError("application plan must be an object")
    actions_value = data.get("actions")
    if not isinstance(actions_value, list):
        raise ApplicationError("application plan actions must be an array")
    actions = []
    for position, raw in enumerate(actions_value, 1):
        if not isinstance(raw, Mapping):
            raise ApplicationError(f"application plan actions[{position}] must be an object")
        style = raw.get("style", {})
        if not isinstance(style, Mapping):
            raise ApplicationError(f"application plan actions[{position}].style must be an object")
        try:
            occurrence_id = raw["occurrence_id"]
            decision = raw["decision"]
            terminal_status = raw["terminal_status"]
            reason = raw["reason"]
            if not all(isinstance(value, str) for value in (occurrence_id, decision, terminal_status, reason)):
                raise TypeError("occurrence_id, decision, terminal_status, and reason must be strings")
            actions.append(
                ApplicationAction(
                    occurrence_id=occurrence_id,
                    decision=decision,
                    terminal_status=terminal_status,
                    reason=reason,
                    package_part=raw.get("package_part"),
                    story=raw.get("story"),
                    paragraph=raw.get("paragraph"),
                    run_index=raw.get("run_index"),
                    run_start=raw.get("run_start"),
                    run_end=raw.get("run_end"),
                    source=raw.get("source"),
                    layout=raw.get("layout"),
                    template_sha256=raw.get("template_sha256"),
                    styled_template_sha256=raw.get("styled_template_sha256"),
                    style=copy.deepcopy(dict(style)),
                    deletion_revision_id=raw.get("deletion_revision_id"),
                    insertion_revision_id=raw.get("insertion_revision_id"),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApplicationError(f"invalid application plan action {position}: {error}") from error
    plan = ApplicationPlan(
        schema_version=data.get("schema_version"),
        source_sha256=data.get("source_sha256"),
        manifest_id=data.get("manifest_id"),
        manifest_sha256=data.get("manifest_sha256"),
        revision_author=data.get("revision_author"),
        max_existing_revision_id=data.get("max_existing_revision_id"),
        actions=tuple(actions),
    )
    _validate_plan(plan)
    supplied_hash = data.get("plan_sha256")
    if supplied_hash != plan.plan_sha256:
        raise ApplicationError("application plan hash does not match its contents")
    return plan


def dump_application_plan(plan: ApplicationPlan, path: str | Path) -> None:
    _validate_plan(plan)
    Path(path).write_text(_canonical_json(plan.to_dict()) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class _Template:
    node: ET.Element
    sha256: str


def _template(value: Any, occurrence_id: str) -> _Template:
    if isinstance(value, ET.Element):
        raise _EligibilityRefusal("template_semantic_evidence_missing", OccurrenceStatus.NEEDS_REVIEW.value)
    if not isinstance(value, Mapping):
        raise _EligibilityRefusal("template_missing_or_unapproved", OccurrenceStatus.NEEDS_REVIEW.value)
    node = value.get("node")
    if node is None:
        node = value.get("element")
    if node is None and isinstance(value.get("omml"), ET.Element):
        node = value.get("omml")
    if not isinstance(node, ET.Element) or node.tag != _m("oMath"):
        raise _EligibilityRefusal("template_not_single_native_omath", OccurrenceStatus.NEEDS_REVIEW.value)
    semantic = value.get("semantic")
    if not isinstance(semantic, Mapping) or semantic.get("status") != "PASS" or value.get("auto_eligible") is not True:
        raise _EligibilityRefusal("template_not_semantically_approved", OccurrenceStatus.NEEDS_REVIEW.value)
    computed_hash = _sha256(ET.tostring(node, encoding="utf-8"))
    declared_hash = value.get("omml_sha256")
    if declared_hash is not None and declared_hash != computed_hash:
        raise _EligibilityRefusal("template_hash_mismatch", OccurrenceStatus.NEEDS_REVIEW.value)
    return _Template(copy.deepcopy(node), computed_hash)


def _resolved_style(row: Mapping[str, Any]) -> dict[str, Any]:
    resolved = row.get("resolved_style")
    if not isinstance(resolved, Mapping) or resolved.get("status") != "RESOLVED" or resolved.get("auto_eligible") is not True:
        raise _EligibilityRefusal("style_not_resolved", OccurrenceStatus.NEEDS_REVIEW.value)
    style = resolved.get("style")
    if not isinstance(style, Mapping):
        raise _EligibilityRefusal("style_result_missing", OccurrenceStatus.NEEDS_REVIEW.value)
    allowed = {"math_font", "math_font_policy", "color", "size", "highlight", "underline", "math_style"}
    unknown = sorted(set(style) - allowed)
    if unknown:
        raise _EligibilityRefusal("style_result_contains_unsupported_fields", OccurrenceStatus.NEEDS_REVIEW.value)
    if style.get("math_font") != MATH_FONT or style.get("math_font_policy") != "CAMBRIA_MATH":
        raise _EligibilityRefusal("style_math_font_policy_not_word_compatible", OccurrenceStatus.NEEDS_REVIEW.value)
    if "color" in style and (not isinstance(style["color"], str) or not _HEX_COLOR.fullmatch(style["color"].strip())):
        raise _EligibilityRefusal("style_color_invalid", OccurrenceStatus.NEEDS_REVIEW.value)
    if "size" in style and (not isinstance(style["size"], str) or not _WORD_SIZE.fullmatch(style["size"].strip()) or int(style["size"]) > 32767):
        raise _EligibilityRefusal("style_size_invalid", OccurrenceStatus.NEEDS_REVIEW.value)
    if "highlight" in style and style["highlight"] not in HIGHLIGHTS:
        raise _EligibilityRefusal("style_highlight_invalid", OccurrenceStatus.NEEDS_REVIEW.value)
    if "underline" in style and style["underline"] not in UNDERLINES:
        raise _EligibilityRefusal("style_underline_invalid", OccurrenceStatus.NEEDS_REVIEW.value)
    if "math_style" in style and style["math_style"] not in {"none", "bold", "italic"}:
        raise _EligibilityRefusal("style_math_emphasis_requires_special_handler", OccurrenceStatus.NEEDS_REVIEW.value)
    return copy.deepcopy(dict(style))


def _validate_action_style(style: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the style frozen into a plan without consulting mutable input."""

    return _resolved_style(
        {
            "resolved_style": {
                "status": "RESOLVED",
                "auto_eligible": True,
                "style": style,
            }
        }
    )


def _direct_child(node: ET.Element, tag: str) -> ET.Element | None:
    matches = [child for child in node if child.tag == tag]
    if len(matches) > 1:
        raise ApplicationError(f"XML node contains duplicate {_local(tag)} children")
    return matches[0] if matches else None


def _set_word_value(parent: ET.Element, local: str, value: str) -> None:
    child = _direct_child(parent, _w(local))
    if child is None:
        child = ET.SubElement(parent, _w(local))
    child.set(_q(W, "val"), value)
    for attribute in list(child.attrib):
        if attribute not in {_q(W, "val")}:  # Keep no unbound aliases in generated properties.
            del child.attrib[attribute]


def _apply_math_style(node: ET.Element, style: Mapping[str, Any]) -> None:
    """Apply occurrence style only to the new OMML control properties."""

    math_style = style.get("math_style")
    style_value = {"none": "p", "bold": "b", "italic": "i"}.get(math_style)
    for math_run in (item for item in node.iter() if item.tag == _m("r")):
        math_properties = _direct_child(math_run, _m("rPr"))
        if math_properties is None:
            math_properties = ET.Element(_m("rPr"))
            math_run.insert(0, math_properties)
        if style_value is not None:
            math_style_node = _direct_child(math_properties, _m("sty"))
            if math_style_node is None:
                math_style_node = ET.Element(_m("sty"))
                math_properties.insert(0, math_style_node)
            math_style_node.set(_q(M, "val"), style_value)

        control_properties = _direct_child(math_properties, _m("ctrlPr"))
        if control_properties is None:
            control_properties = ET.SubElement(math_properties, _m("ctrlPr"))
        word_properties = _direct_child(control_properties, _w("rPr"))
        if word_properties is None:
            word_properties = ET.SubElement(control_properties, _w("rPr"))

        fonts = _direct_child(word_properties, _w("rFonts"))
        if fonts is None:
            fonts = ET.SubElement(word_properties, _w("rFonts"))
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(_q(W, attribute), MATH_FONT)
        for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "csTheme"):
            fonts.attrib.pop(_q(W, attribute), None)

        if "color" in style:
            _set_word_value(word_properties, "color", style["color"])
        if "size" in style:
            _set_word_value(word_properties, "sz", style["size"])
            _set_word_value(word_properties, "szCs", style["size"])
        if "highlight" in style:
            _set_word_value(word_properties, "highlight", style["highlight"])
        if "underline" in style:
            _set_word_value(word_properties, "u", style["underline"])


def _styled_template(template: _Template, style: Mapping[str, Any]) -> ET.Element:
    node = copy.deepcopy(template.node)
    _apply_math_style(node, style)
    return node


def _set_text_value(node: ET.Element, local: str, value: str) -> None:
    node.tag = _w(local)
    node.attrib.clear()
    if value.startswith((" ", "\t")) or value.endswith((" ", "\t")):
        node.set(_q(XML, "space"), "preserve")
    node.text = value


def _run_fragment(run: ET.Element, value: str, *, deleted: bool) -> ET.Element:
    fragment = copy.deepcopy(run)
    direct_text = [child for child in fragment if child.tag in {_w("t"), _w("delText")}]
    if len(direct_text) != 1 or any(child.tag not in {_w("rPr"), _w("t"), _w("delText")} for child in fragment):
        raise ApplicationError("source run changed while building a revision fragment")
    _set_text_value(direct_text[0], "delText" if deleted else "t", value)
    return fragment


def _revision_wrapper(local: str, revision_id: int, author: str, child: ET.Element) -> ET.Element:
    wrapper = ET.Element(
        _w(local),
        {
            _q(W, "id"): str(revision_id),
            _q(W, "author"): author,
        },
    )
    wrapper.append(child)
    return wrapper


def _replace_run(
    root: ET.Element,
    run: ET.Element,
    source: str,
    template: _Template,
    style: Mapping[str, Any],
    action: ApplicationAction,
    revision_author: str,
) -> None:
    parents = _parent_map(root)
    parent = parents.get(id(run))
    if parent is None or run not in list(parent):
        raise ApplicationError(f"application target {action.occurrence_id} is no longer attached to its paragraph")
    text_nodes = [child for child in run if child.tag == _w("t")]
    if len(text_nodes) != 1:
        raise ApplicationError(f"application target {action.occurrence_id} is no longer a plain text run")
    original_text = text_nodes[0].text or ""
    start = action.run_start
    end = action.run_end
    if start is None or end is None or original_text[start:end] != source:
        raise ApplicationError(f"application target {action.occurrence_id} changed after the plan was frozen")

    styled_template = _styled_template(template, style)
    prefix = _run_fragment(run, original_text[:start], deleted=False) if start else None
    suffix = _run_fragment(run, original_text[end:], deleted=False) if end < len(original_text) else None
    deleted = _revision_wrapper(
        "del",
        action.deletion_revision_id,
        revision_author,
        _run_fragment(run, source, deleted=True),
    )
    inserted = _revision_wrapper("ins", action.insertion_revision_id, revision_author, styled_template)

    index = list(parent).index(run)
    parent.remove(run)
    replacement = [item for item in (prefix, deleted, inserted, suffix) if item is not None]
    for offset, item in enumerate(replacement):
        parent.insert(index + offset, item)


def _check_row_protection(row: Mapping[str, Any]) -> None:
    if row.get("inside_existing_revision") is not False or row.get("revision_ancestry") != []:
        raise _EligibilityRefusal("existing_revision_intersection", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    protected = row.get("protected_containers")
    if not isinstance(protected, Mapping) or any(protected.get(field) is not False for field in _PROTECTED_FIELDS):
        raise _EligibilityRefusal("protected_container_intersection", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    if any(row.get(field) is not False for field in _ADJACENT_FIELDS):
        raise _EligibilityRefusal("protected_adjacent_structure", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)


def _declared_template_hash(row: Mapping[str, Any]) -> str | None:
    for container in (row.get("omml"), row.get("extensions")):
        if not isinstance(container, Mapping):
            continue
        for key in ("omml_sha256", "template_sha256", "sha256"):
            value = container.get(key)
            if value is not None:
                return value
        nested = container.get("omml")
        if isinstance(nested, Mapping):
            for key in ("omml_sha256", "template_sha256", "sha256"):
                if nested.get(key) is not None:
                    return nested[key]
    return None


def _refusal_action(row: Mapping[str, Any], reason: str, terminal_status: str) -> ApplicationAction:
    return ApplicationAction(
        occurrence_id=row["id"],
        decision="REFUSE",
        terminal_status=terminal_status,
        reason=reason,
        package_part=row.get("package_part"),
        story=row.get("story"),
        paragraph=row.get("paragraph"),
        run_index=row.get("run_index"),
        run_start=row.get("run_start"),
        run_end=row.get("run_end"),
        source=row.get("raw_source", row.get("source")),
        layout=row.get("target_layout", row.get("layout")),
    )


def _plan_row(row: Mapping[str, Any], package: _Package, templates: Mapping[str, Any]) -> ApplicationAction:
    occurrence_id = row["id"]
    status = row.get("status")
    source_type = row.get("source_type")
    if status == OccurrenceStatus.EXCLUDED.value:
        return ApplicationAction(occurrence_id, "PRESERVE", status, "approved_exclusion", source=row.get("raw_source", row.get("source")))
    if status == OccurrenceStatus.PRESERVED.value or source_type == SourceType.EXISTING_OMML.value:
        return ApplicationAction(occurrence_id, "PRESERVE", OccurrenceStatus.PRESERVED.value, "existing_native_omml", source=row.get("raw_source", row.get("source")))
    if status in {
        OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        OccurrenceStatus.NEEDS_REVIEW.value,
        OccurrenceStatus.REFUSED.value,
        OccurrenceStatus.FAILED.value,
    }:
        return _refusal_action(row, "occurrence_already_requires_review", status)
    if status != OccurrenceStatus.APPROVED.value:
        return _refusal_action(row, "occurrence_not_approved", OccurrenceStatus.NEEDS_REVIEW.value)
    if source_type in _SPECIAL_SOURCE_TYPES:
        return _refusal_action(row, "source_type_requires_special_handler", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    layout = row.get("target_layout", row.get("layout"))
    if layout != "inline":
        return _refusal_action(row, "display_requires_paragraph_handler", OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value)
    try:
        _check_row_protection(row)
        _row_location(row, package)
        style = _resolved_style(row)
        template = _template(templates.get(occurrence_id), occurrence_id)
        declared_hash = _declared_template_hash(row)
        if declared_hash is not None and declared_hash != template.sha256:
            raise _EligibilityRefusal("template_hash_does_not_match_manifest", OccurrenceStatus.NEEDS_REVIEW.value)
        styled_template_hash = _sha256(ET.tostring(_styled_template(template, style), encoding="utf-8"))
    except _EligibilityRefusal as refusal:
        return _refusal_action(row, refusal.reason, refusal.terminal_status)
    return ApplicationAction(
        occurrence_id=occurrence_id,
        decision="APPLY",
        terminal_status=OccurrenceStatus.APPLIED.value,
        reason="safe_single_run_occurrence",
        package_part=row.get("package_part", "word/document.xml"),
        story=row.get("story"),
        paragraph=row.get("paragraph"),
        run_index=row.get("run_index"),
        run_start=row.get("run_start"),
        run_end=row.get("run_end"),
        source=row.get("raw_source", row.get("source")),
        layout=layout,
        template_sha256=template.sha256,
        styled_template_sha256=styled_template_hash,
        style=style,
    )


def build_application_plan(
    source: str | Path,
    manifest: Manifest | Mapping[str, Any] | str | Path,
    templates: Mapping[str, Any],
    *,
    revision_author: str = SESSION_AUTHOR,
) -> ApplicationPlan:
    """Build a hash-bound dry-run plan without changing the source package."""

    current = manifest if isinstance(manifest, Manifest) else load_manifest(manifest)
    package = _read_package(source)
    source_sha256 = _sha256(package.source_bytes)
    if current.source_sha256 is not None and current.source_sha256 != source_sha256:
        raise ApplicationError("manifest source_sha256 does not match the source DOCX")
    if not isinstance(templates, Mapping):
        raise ApplicationError("templates must be a mapping keyed by occurrence ID")
    if not isinstance(revision_author, str) or not revision_author.strip():
        raise ApplicationError("revision_author must be a non-empty string")

    actions = [_plan_row(row, package, templates) for row in current.formulas]
    seen_targets: dict[tuple[Any, ...], int] = {}
    for index, action in enumerate(actions):
        if action.decision != "APPLY":
            continue
        key = (action.package_part, action.paragraph, action.run_index)
        if key in seen_targets:
            prior = seen_targets[key]
            for position in (prior, index):
                actions[position] = _refusal_action(
                    current.formulas[position],
                    "overlapping_or_same_run_occurrence",
                    OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
                )
        else:
            seen_targets[key] = index

    next_revision_id = _max_revision_id(package) + 1
    allocated: list[ApplicationAction] = []
    for action in actions:
        if action.decision == "APPLY":
            allocated.append(
                replace(
                    action,
                    deletion_revision_id=next_revision_id,
                    insertion_revision_id=next_revision_id + 1,
                )
            )
            next_revision_id += 2
        else:
            allocated.append(action)
    plan = ApplicationPlan(
        schema_version=1,
        source_sha256=source_sha256,
        manifest_id=current.manifest_id,
        manifest_sha256=current.manifest_sha256,
        revision_author=revision_author,
        max_existing_revision_id=next_revision_id - 1 if allocated and any(action.decision == "APPLY" for action in allocated) else _max_revision_id(package),
        actions=tuple(allocated),
    )
    # The baseline field must remain the source maximum, not the last allocated ID.
    plan = replace(plan, max_existing_revision_id=_max_revision_id(package))
    return _validate_plan(plan)


def prepare_application(
    source: str | Path,
    manifest: Manifest | Mapping[str, Any] | str | Path,
    requested_artifacts: Sequence[str | Mapping[str, Any]],
    templates: Mapping[str, Any],
    *,
    revision_author: str = SESSION_AUTHOR,
    extensions: Mapping[str, Any] | None = None,
) -> tuple[FrozenJob, ApplicationPlan]:
    """Build the plan first, then freeze the #2 job against its plan hash."""

    current = manifest if isinstance(manifest, Manifest) else load_manifest(manifest)
    plan = build_application_plan(source, current, templates, revision_author=revision_author)
    job = freeze_job(
        current,
        plan.source_sha256,
        requested_artifacts,
        application_plan_sha256=plan.plan_sha256,
        extensions=extensions,
    )
    return job, plan


@dataclass(frozen=True)
class StagedApplication:
    """Candidate paths and the job state produced by a staging run."""

    job: FrozenJob
    plan: ApplicationPlan
    source_sha256: str
    artifact_paths: Mapping[str, Path]
    artifact_hashes: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "plan": self.plan.to_dict(),
            "source_sha256": self.source_sha256,
            "artifact_paths": {key: str(value) for key, value in sorted(self.artifact_paths.items())},
            "artifact_hashes": dict(sorted(self.artifact_hashes.items())),
        }


def _coerce_plan(value: ApplicationPlan | Mapping[str, Any] | str | Path) -> ApplicationPlan:
    if isinstance(value, ApplicationPlan):
        return _validate_plan(value)
    return load_application_plan(value)


def _coerce_job(value: FrozenJob | Mapping[str, Any] | str | Path) -> FrozenJob:
    if isinstance(value, FrozenJob):
        return load_job(value.to_dict())
    return load_job(value)


def _manifest_rows(manifest: Manifest, plan: ApplicationPlan) -> dict[str, dict[str, Any]]:
    rows = {row["id"]: row for row in manifest.formulas}
    action_ids = tuple(action.occurrence_id for action in plan.actions)
    manifest_ids = tuple(row["id"] for row in manifest.formulas)
    if action_ids != manifest_ids:
        raise ApplicationError("application plan does not account for every manifest occurrence in order")
    return rows


def _verify_plan_inputs(
    source: str | Path,
    manifest: Manifest | Mapping[str, Any] | str | Path,
    plan: ApplicationPlan | Mapping[str, Any] | str | Path,
) -> tuple[Manifest, ApplicationPlan, _Package]:
    current = manifest if isinstance(manifest, Manifest) else load_manifest(manifest)
    frozen = _coerce_plan(plan)
    package = _read_package(source)
    source_sha256 = _sha256(package.source_bytes)
    if frozen.source_sha256 != source_sha256:
        raise ApplicationError("application plan source_sha256 does not match the source DOCX")
    if frozen.manifest_id != current.manifest_id or frozen.manifest_sha256 != current.manifest_sha256:
        raise ApplicationError("application plan manifest identity is stale; rebuild/re-approve the manifest")
    _manifest_rows(current, frozen)
    return current, frozen, package


def _verify_job_plan(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    manifest: Manifest,
    plan: ApplicationPlan,
    package: _Package,
    requested_artifacts: Sequence[str | Mapping[str, Any]] | None = None,
) -> FrozenJob:
    current_job = _coerce_job(job)
    verify_frozen_job(current_job, manifest, _sha256(package.source_bytes), requested_artifacts)
    if current_job.application_plan_sha256 != plan.plan_sha256:
        raise ApplicationError("frozen job is not bound to this application plan")
    if tuple(current_job.occurrence_ids) != tuple(action.occurrence_id for action in plan.actions):
        raise ApplicationError("frozen job occurrence accounting does not match the application plan")
    return current_job


def _action_matches_row(action: ApplicationAction, row: Mapping[str, Any]) -> None:
    expected = {
        "package_part": row.get("package_part", "word/document.xml"),
        "story": row.get("story"),
        "paragraph": row.get("paragraph"),
        "run_index": row.get("run_index"),
        "run_start": row.get("run_start"),
        "run_end": row.get("run_end"),
        "source": row.get("raw_source", row.get("source")),
        "layout": row.get("target_layout", row.get("layout")),
    }
    actual = {
        "package_part": action.package_part,
        "story": action.story,
        "paragraph": action.paragraph,
        "run_index": action.run_index,
        "run_start": action.run_start,
        "run_end": action.run_end,
        "source": action.source,
        "layout": action.layout,
    }
    if actual != expected:
        raise ApplicationError(f"application plan action {action.occurrence_id} no longer matches its manifest row")


def _revision_nodes(
    package: _Package,
    author: str | None = None,
    *,
    locals_: frozenset[str] = _SESSION_REVISION_LOCALS,
) -> list[tuple[str, ET.Element]]:
    result: list[tuple[str, ET.Element]] = []
    for name, root in package.roots.items():
        if name.endswith(".rels"):
            continue
        for node in root.iter():
            if _namespace(node.tag) != W or _local(node.tag) not in locals_:
                continue
            if author is None or _attribute(node, W, "author") == author:
                result.append((name, node))
    return result


def _ensure_session_author_is_unused(package: _Package, author: str) -> None:
    if _revision_nodes(package, author, locals_=_TRACKED_REVISION_LOCALS):
        raise ApplicationError(
            "source already contains revisions owned by the remediation session author; use a fresh author identity"
        )


def _serialize_root(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True, short_empty_elements=True)


def _enable_revision_tracking(package: _Package) -> None:
    settings_name = "word/settings.xml"
    root = package.roots.get(settings_name)
    if root is None or root.tag != _w("settings"):
        raise ApplicationError("word/settings.xml is required to record tracked formula replacements")
    tracking_nodes = [child for child in root if child.tag == _w("trackRevisions")]
    if len(tracking_nodes) > 1:
        raise ApplicationError("word/settings.xml contains duplicate w:trackRevisions elements")
    if tracking_nodes and (_attribute(tracking_nodes[0], W, "val") or "true").strip().lower() in {
        "0",
        "false",
        "nil",
        "off",
        "none",
    }:
        raise ApplicationError("word/settings.xml explicitly disables tracked revisions")
    if not tracking_nodes:
        root.append(ET.Element(_w("trackRevisions")))
        package.parts[settings_name] = _serialize_root(root)


def apply_application_plan(
    source: str | Path,
    manifest: Manifest | Mapping[str, Any] | str | Path,
    plan: ApplicationPlan | Mapping[str, Any] | str | Path,
    templates: Mapping[str, Any],
) -> bytes:
    """Apply only the frozen ``APPLY`` actions and return a redlined candidate.

    The source path is read-only.  All eligibility and target checks happen on
    the unmodified package before any element is replaced.
    """

    current, frozen, package = _verify_plan_inputs(source, manifest, plan)
    if not isinstance(templates, Mapping):
        raise ApplicationError("templates must be a mapping keyed by occurrence ID")
    rows = _manifest_rows(current, frozen)
    prepared: list[tuple[str, int, int, int, ET.Element, ET.Element, _Template, dict[str, Any], ApplicationAction]] = []
    for action in frozen.actions:
        if action.decision != "APPLY":
            continue
        row = rows[action.occurrence_id]
        _action_matches_row(action, row)
        try:
            root, _paragraph, run, _text_node, _global_start, _global_end, _run_index = _row_location(row, package)
            template = _template(templates.get(action.occurrence_id), action.occurrence_id)
        except _EligibilityRefusal as refusal:
            raise ApplicationError(
                f"frozen application target {action.occurrence_id} is no longer eligible: {refusal.reason}"
            ) from refusal
        declared_hash = _declared_template_hash(row)
        if declared_hash is not None and declared_hash != template.sha256:
            raise ApplicationError(f"template hash changed for frozen occurrence {action.occurrence_id}")
        if action.template_sha256 != template.sha256:
            raise ApplicationError(f"template hash does not match the frozen action {action.occurrence_id}")
        style = _validate_action_style(action.style)
        styled_template_hash = _sha256(ET.tostring(_styled_template(template, style), encoding="utf-8"))
        if action.styled_template_sha256 != styled_template_hash:
            raise ApplicationError(f"styled template hash does not match the frozen action {action.occurrence_id}")
        prepared.append(
            (
                action.package_part or "word/document.xml",
                action.paragraph or 0,
                action.run_index or 0,
                action.run_start or 0,
                root,
                run,
                template,
                style,
                action,
            )
        )

    if not prepared:
        return package.source_bytes

    _ensure_session_author_is_unused(package, frozen.revision_author)
    _enable_revision_tracking(package)
    for part, _paragraph, _run_index, _run_start, root, run, template, style, action in sorted(
        prepared,
        key=lambda item: (item[0], item[1], item[2], item[3]),
        reverse=True,
    ):
        _replace_run(root, run, action.source or "", template, style, action, frozen.revision_author)

    changed_parts = {item[0] for item in prepared}
    for part in changed_parts:
        package.parts[part] = _serialize_root(package.roots[part])
    candidate = _serialize_package(package)
    _package_from_bytes(candidate, "redlined staging candidate")
    source_path = _safe_path(source, "source")
    try:
        if _sha256(source_path.read_bytes()) != frozen.source_sha256:
            raise ApplicationError("source DOCX changed during application; candidate is discarded")
    except OSError as error:
        raise ApplicationError(f"cannot recheck source immutability: {error}") from error
    return candidate


def _candidate_package(candidate: bytes | str | Path) -> _Package:
    if isinstance(candidate, bytes):
        return _package_from_bytes(candidate, "DOCX candidate")
    path = _safe_path(candidate, "candidate")
    try:
        return _package_from_bytes(path.read_bytes(), f"candidate {path}")
    except OSError as error:
        raise ApplicationError(f"cannot read candidate DOCX {path}: {error}") from error


def _expected_session_revisions(plan: ApplicationPlan) -> tuple[set[int], set[int]]:
    deletion_ids = {action.deletion_revision_id for action in plan.actions if action.decision == "APPLY"}
    insertion_ids = {action.insertion_revision_id for action in plan.actions if action.decision == "APPLY"}
    if None in deletion_ids or None in insertion_ids:
        raise ApplicationError("application plan has incomplete session revision identity")
    return {int(value) for value in deletion_ids}, {int(value) for value in insertion_ids}


def _revision_id(node: ET.Element, label: str) -> int:
    value = _attribute(node, W, "id")
    try:
        parsed = int(value) if value is not None else -1
    except (TypeError, ValueError):
        parsed = -1
    if parsed < 0:
        raise ApplicationError(f"{label} has no valid revision ID")
    return parsed


def _other_revision_fingerprint(package: _Package, author: str) -> tuple[tuple[str, str, bytes], ...]:
    return tuple(
        sorted(
            (
                part,
                _local(node.tag),
                ET.tostring(node, encoding="utf-8"),
            )
            for part, node in _revision_nodes(package, locals_=_TRACKED_REVISION_LOCALS)
            if _attribute(node, W, "author") != author
        )
    )


def _assert_session_action_binding(
    package: _Package,
    action: ApplicationAction,
    session_nodes: Mapping[tuple[str, int], tuple[str, ET.Element]],
) -> None:
    part = action.package_part or "word/document.xml"
    root = package.roots.get(part)
    if root is None:
        raise ApplicationError(f"session revisions for {action.occurrence_id} are in a missing package part")
    if action.paragraph is None or action.paragraph < 1:
        raise ApplicationError(f"session revisions for {action.occurrence_id} have no paragraph binding")
    blocks = _block_elements(root)
    if action.paragraph > len(blocks) or blocks[action.paragraph - 1].tag != _w("p"):
        raise ApplicationError(f"session revisions for {action.occurrence_id} moved from their frozen paragraph")
    paragraph = blocks[action.paragraph - 1]
    parents = _parent_map(root)
    deletion_entry = session_nodes.get(("del", int(action.deletion_revision_id)))
    insertion_entry = session_nodes.get(("ins", int(action.insertion_revision_id)))
    if deletion_entry is None or insertion_entry is None:
        raise ApplicationError(f"session revisions for {action.occurrence_id} are incomplete")
    deletion_part, deletion = deletion_entry
    insertion_part, insertion = insertion_entry
    if deletion_part != part or insertion_part != part:
        raise ApplicationError(f"session revisions for {action.occurrence_id} moved to another package part")
    if parents.get(id(deletion)) is not paragraph or parents.get(id(insertion)) is not paragraph:
        raise ApplicationError(f"session revisions for {action.occurrence_id} moved from their frozen paragraph")
    children = list(paragraph)
    try:
        deletion_index = children.index(deletion)
        insertion_index = children.index(insertion)
    except ValueError as error:
        raise ApplicationError(f"session revisions for {action.occurrence_id} are detached") from error
    if insertion_index != deletion_index + 1:
        raise ApplicationError(f"session revisions for {action.occurrence_id} are no longer adjacent")

    deleted_children = list(deletion)
    if len(deleted_children) != 1 or deleted_children[0].tag != _w("r"):
        raise ApplicationError(f"session deletion for {action.occurrence_id} has an unsupported source run")
    deleted_run = deleted_children[0]
    deleted_text = [child for child in deleted_run if child.tag == _w("delText")]
    if (
        len(deleted_text) != 1
        or any(child.tag not in {_w("rPr"), _w("delText")} for child in deleted_run)
        or (deleted_text[0].text or "") != action.source
    ):
        raise ApplicationError(f"session deletion for {action.occurrence_id} does not match frozen source text")

    inserted_children = list(insertion)
    if len(inserted_children) != 1 or inserted_children[0].tag != _m("oMath"):
        raise ApplicationError(f"session insertion for {action.occurrence_id} has an unsupported OMML node")
    actual_hash = _sha256(ET.tostring(inserted_children[0], encoding="utf-8"))
    if actual_hash != action.styled_template_sha256:
        raise ApplicationError(f"session insertion for {action.occurrence_id} does not match the frozen OMML")


def _assert_session_revision_set(package: _Package, plan: ApplicationPlan) -> None:
    expected_deletions, expected_insertions = _expected_session_revisions(plan)
    found_deletions: set[int] = set()
    found_insertions: set[int] = set()
    session_nodes: dict[tuple[str, int], tuple[str, ET.Element]] = {}
    parents_by_root = {name: _parent_map(root) for name, root in package.roots.items()}
    for part, node in _revision_nodes(package, plan.revision_author):
        revision_id = _revision_id(node, f"session revision in {part}")
        if node.tag == _w("del"):
            if revision_id not in expected_deletions or revision_id in found_deletions:
                raise ApplicationError("candidate contains an unexpected or duplicate session deletion revision")
            found_deletions.add(revision_id)
            session_nodes[("del", revision_id)] = (part, node)
        else:
            if revision_id not in expected_insertions or revision_id in found_insertions:
                raise ApplicationError("candidate contains an unexpected or duplicate session insertion revision")
            found_insertions.add(revision_id)
            session_nodes[("ins", revision_id)] = (part, node)
        parent_map = parents_by_root[part]
        for ancestor in _ancestors(node, parent_map):
            if _local(ancestor.tag) in {"ins", "del"}:
                raise ApplicationError("session revision is nested in another revision and cannot be safely accepted")
    if found_deletions != expected_deletions or found_insertions != expected_insertions:
        raise ApplicationError("candidate is missing one or more frozen session revisions")
    for action in plan.actions:
        if action.decision == "APPLY":
            _assert_session_action_binding(package, action, session_nodes)


def _restore_deleted_run(wrapper: ET.Element) -> ET.Element:
    children = list(wrapper)
    if len(children) != 1 or children[0].tag != _w("r"):
        raise ApplicationError("session deletion does not contain one ordinary source run")
    run = copy.deepcopy(children[0])
    text_nodes = [child for child in run if child.tag == _w("delText")]
    if len(text_nodes) != 1 or any(child.tag not in {_w("rPr"), _w("delText")} for child in run):
        raise ApplicationError("session deletion contains unsupported source-run structure")
    _set_text_value(text_nodes[0], "t", text_nodes[0].text or "")
    return run


def _session_replacement(wrapper: ET.Element, *, accept: bool) -> list[ET.Element]:
    if wrapper.tag == _w("del"):
        if accept:
            return []
        return [_restore_deleted_run(wrapper)]
    children = list(wrapper)
    if len(children) != 1 or children[0].tag != _m("oMath"):
        raise ApplicationError("session insertion does not contain one native OMML equation")
    if accept:
        return [copy.deepcopy(children[0])]
    return []


def _transform_session_revisions(candidate: bytes | str | Path, plan: ApplicationPlan, *, accept: bool) -> bytes:
    frozen = _coerce_plan(plan)
    package = _candidate_package(candidate)
    _assert_session_revision_set(package, frozen)
    before_other = _other_revision_fingerprint(package, frozen.revision_author)

    def visit(parent: ET.Element) -> None:
        for child in list(parent):
            if child.tag in {_w("ins"), _w("del")} and _attribute(child, W, "author") == frozen.revision_author:
                replacements = _session_replacement(child, accept=accept)
                index = list(parent).index(child)
                parent.remove(child)
                for offset, replacement in enumerate(replacements):
                    parent.insert(index + offset, replacement)
            else:
                visit(child)

    changed_parts: set[str] = set()
    for name, root in package.roots.items():
        if name.endswith(".rels"):
            continue
        had_session_revision = any(part == name for part, _node in _revision_nodes(package, frozen.revision_author))
        before = _other_revision_fingerprint(package, frozen.revision_author)
        visit(root)
        after = _other_revision_fingerprint(package, frozen.revision_author)
        if before != after:
            raise ApplicationError("session revision handling changed a pre-existing other-author revision")
        if had_session_revision:
            changed_parts.add(name)

    if _revision_nodes(package, frozen.revision_author):
        raise ApplicationError("session revisions remain after candidate transformation")
    if before_other != _other_revision_fingerprint(package, frozen.revision_author):
        raise ApplicationError("pre-existing revision fingerprint changed during session revision handling")
    for name in changed_parts:
        package.parts[name] = _serialize_root(package.roots[name])
    transformed = _serialize_package(package)
    _package_from_bytes(transformed, "transformed DOCX candidate")
    return transformed


def accept_session_revisions(candidate: bytes | str | Path, plan: ApplicationPlan) -> bytes:
    """Create a clean candidate by accepting only this plan's insertions."""

    return _transform_session_revisions(candidate, plan, accept=True)


def reject_session_revisions(candidate: bytes | str | Path, plan: ApplicationPlan) -> bytes:
    """Reject this plan's edits and reconstruct the original source runs."""

    return _transform_session_revisions(candidate, plan, accept=False)


def _safe_read_file(path: Path, field_name: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ApplicationError(f"cannot read {field_name} {path}: {error}") from error
    if not path.is_file():
        raise ApplicationError(f"{field_name} must be a regular file: {path}")
    return data


def _atomic_write_bytes(path: str | Path, data: bytes, field_name: str = "staging artifact") -> Path:
    target = _safe_path(path, field_name)
    if target.exists() and not target.is_file():
        raise ApplicationError(f"{field_name} must be a regular file path: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".word-formula-staging-", dir=str(target.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except OSError as error:
        raise ApplicationError(f"cannot atomically write {field_name} {target}: {error}") from error
    return target


def _artifact_paths(
    job: FrozenJob,
    paths: Mapping[str, str | Path],
    field_name: str,
) -> dict[str, tuple[dict[str, Any], Path]]:
    if not isinstance(paths, Mapping):
        raise ApplicationError(f"{field_name} must map every logical artifact ID to a path")
    by_key: dict[str, dict[str, Any]] = {}
    for artifact in job.artifacts:
        by_key[artifact["logical_id"]] = artifact
        by_key[artifact["id"]] = artifact
    result: dict[str, tuple[dict[str, Any], Path]] = {}
    for key, value in paths.items():
        if not isinstance(key, str) or key not in by_key:
            raise ApplicationError(f"{field_name} contains an unknown artifact ID {key!r}")
        logical_id = by_key[key]["logical_id"]
        if logical_id in result:
            raise ApplicationError(f"{field_name} contains duplicate paths for artifact {logical_id!r}")
        result[logical_id] = (by_key[key], _safe_path(value, f"{field_name}.{logical_id}"))
    expected = {artifact["logical_id"] for artifact in job.artifacts}
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ApplicationError(f"{field_name} must cover exactly the frozen artifact set; missing={missing}, extra={extra}")
    paths_only = [path for _artifact, path in result.values()]
    if len(set(paths_only)) != len(paths_only):
        raise ApplicationError(f"{field_name} paths must be unique for every requested artifact")
    return result


def _set_job_occurrence_statuses(job: FrozenJob, plan: ApplicationPlan) -> FrozenJob:
    updated = _coerce_job(job)
    for action in plan.actions:
        updated_data = updated.to_dict()
        for row in updated_data["occurrences"]:
            if row["id"] == action.occurrence_id:
                row["status"] = action.terminal_status
                break
        updated = load_job(updated_data)
    return updated


def _set_artifact_states(job: FrozenJob, state: str) -> FrozenJob:
    data = job.to_dict()
    for artifact in data["artifacts"]:
        artifact["state"] = state
    return load_job(data)


def _bind_staged_hashes(job: FrozenJob, artifact_paths: Mapping[str, tuple[dict[str, Any], Path]]) -> tuple[FrozenJob, dict[str, str]]:
    updated = job
    hashes: dict[str, str] = {}
    for logical_id in sorted(artifact_paths):
        artifact, path = artifact_paths[logical_id]
        data = _safe_read_file(path, "staging artifact")
        content_sha256 = _sha256(data)
        updated = set_artifact_content(updated, artifact["id"], content_sha256)
        hashes[logical_id] = content_sha256
    return updated, hashes


def stage_application(
    source: str | Path,
    manifest: Manifest | Mapping[str, Any] | str | Path,
    job: FrozenJob | Mapping[str, Any] | str | Path,
    plan: ApplicationPlan | Mapping[str, Any] | str | Path,
    templates: Mapping[str, Any],
    artifact_paths: Mapping[str, str | Path],
) -> StagedApplication:
    """Write redlined/clean candidates and bind their exact hashes to the job."""

    current, frozen, package = _verify_plan_inputs(source, manifest, plan)
    current_job = _verify_job_plan(job, current, frozen, package)
    paths = _artifact_paths(current_job, artifact_paths, "artifact_paths")
    source_path = _safe_path(source, "source")
    if any(path == source_path for _artifact, path in paths.values()):
        raise ApplicationError("staging artifact path must not overwrite the source DOCX")

    redlined = apply_application_plan(source, current, frozen, templates)
    clean: bytes | None = None
    candidates: dict[str, bytes] = {}
    for logical_id, (artifact, _path) in paths.items():
        kind = str(artifact["kind"]).strip().lower()
        if kind == "redlined" or logical_id.lower() == "redlined":
            candidates[logical_id] = redlined
        elif kind == "clean" or logical_id.lower() == "clean":
            if clean is None:
                clean = accept_session_revisions(redlined, frozen)
            candidates[logical_id] = clean
        else:
            raise ApplicationError(
                f"artifact {logical_id!r} has unsupported W5 candidate kind {artifact['kind']!r}; "
                "use an explicit task-specific handler"
            )

    for logical_id in sorted(candidates):
        _atomic_write_bytes(paths[logical_id][1], candidates[logical_id], f"staging artifact {logical_id}")
    try:
        if _sha256(_safe_read_file(source_path, "source DOCX")) != frozen.source_sha256:
            raise ApplicationError("source DOCX changed during staging")
    except ApplicationError:
        raise

    updated_job = _set_job_occurrence_statuses(current_job, frozen)
    updated_job, hashes = _bind_staged_hashes(updated_job, paths)
    return StagedApplication(
        job=updated_job,
        plan=frozen,
        source_sha256=frozen.source_sha256,
        artifact_paths={key: value[1] for key, value in paths.items()},
        artifact_hashes=hashes,
    )


def bind_staged_artifacts(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    artifact_paths: Mapping[str, str | Path],
) -> FrozenJob:
    """Bind candidate hashes to a frozen job without changing gate evidence."""

    current = _coerce_job(job)
    paths = _artifact_paths(current, artifact_paths, "artifact_paths")
    updated, _hashes = _bind_staged_hashes(current, paths)
    return updated


def validate_staged_artifacts(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    artifact_paths: Mapping[str, str | Path],
) -> FrozenJob:
    """Verify candidate hashes and all required PASS gates, then mark VALIDATED."""

    current = _coerce_job(job)
    paths = _artifact_paths(current, artifact_paths, "artifact_paths")
    for logical_id, (artifact, path) in paths.items():
        data = _safe_read_file(path, "staging artifact")
        candidate_hash = _sha256(data)
        if artifact.get("content_sha256") != candidate_hash:
            raise ApplicationError(f"staging artifact {logical_id} hash does not match the frozen job")
        if artifact.get("state") not in {ArtifactState.STAGING.value, ArtifactState.VALIDATED.value}:
            raise ApplicationError(f"staging artifact {logical_id} is not in a validateable state")
        for gate_name in artifact["required_gates"]:
            if artifact["gates"][gate_name]["state"] != GateState.PASS.value:
                raise ApplicationError(
                    f"staging artifact {logical_id} cannot be VALIDATED before gate {gate_name} passes"
                )
    return _set_artifact_states(current, ArtifactState.VALIDATED.value)


def _temporary_backup_path(parent: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".word-formula-backup-", dir=str(parent))
    os.close(descriptor)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path


def finalize_artifact_set(
    job: FrozenJob | Mapping[str, Any] | str | Path,
    staging_paths: Mapping[str, str | Path],
    final_paths: Mapping[str, str | Path],
    *,
    source: str | Path,
) -> FrozenJob:
    """Promote the complete requested artifact set with rollback on any failure."""

    current = _coerce_job(job)
    source_path = _safe_path(source, "source")
    source_bytes = _safe_read_file(source_path, "source DOCX")
    if _sha256(source_bytes) != current.source_sha256:
        raise ApplicationError("source DOCX no longer matches the frozen job")
    if current.status != DeliveryStatus.COMPLETE.value:
        raise ApplicationError("artifact set is not COMPLETE; finalization is refused")
    if any(artifact.get("state") != ArtifactState.VALIDATED.value for artifact in current.artifacts):
        raise ApplicationError("every requested artifact must be VALIDATED before finalization")
    staging = _artifact_paths(current, staging_paths, "staging_paths")
    finals = _artifact_paths(current, final_paths, "final_paths")
    staged_paths = [path for _artifact, path in staging.values()]
    final_path_values = [path for _artifact, path in finals.values()]
    if len(set(staged_paths)) != len(staged_paths) or len(set(final_path_values)) != len(final_path_values):
        raise ApplicationError("staging and final artifact paths must be unique")
    if set(staged_paths) & set(final_path_values):
        raise ApplicationError("staging and final artifact paths must be distinct")
    if source_path in set(staged_paths) or source_path in set(final_path_values):
        raise ApplicationError("source DOCX path must not be used for staging or final artifacts")
    for path in final_path_values:
        if path.exists():
            try:
                if os.path.samefile(path, source_path):
                    raise ApplicationError("final artifact path aliases the source DOCX")
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ApplicationError(f"cannot verify final artifact path identity for {path}: {error}") from error

    for logical_id in sorted(staging):
        artifact, path = staging[logical_id]
        data = _safe_read_file(path, "staging artifact")
        if _sha256(data) != artifact.get("content_sha256"):
            raise ApplicationError(f"staging artifact {logical_id} changed after validation")
    for _logical_id, (_artifact, path) in finals.items():
        if path.exists() and not path.is_file():
            raise ApplicationError(f"final artifact path is not a regular file: {path}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ApplicationError(f"cannot prepare final artifact directory {path.parent}: {error}") from error

    backups: list[tuple[Path, Path]] = []
    promoted: list[tuple[Path, Path]] = []
    transaction_complete = False
    rollback_complete = False
    try:
        for logical_id in sorted(finals):
            _artifact, target = finals[logical_id]
            if target.exists():
                backup = _temporary_backup_path(target.parent)
                os.replace(target, backup)
                backups.append((backup, target))
        for logical_id in sorted(staging):
            _artifact, candidate = staging[logical_id]
            target = finals[logical_id][1]
            os.replace(candidate, target)
            promoted.append((candidate, target))
        if _sha256(_safe_read_file(source_path, "source DOCX")) != current.source_sha256:
            raise ApplicationError("source DOCX changed during finalization")
        transaction_complete = True
    except BaseException as error:
        rollback_errors: list[str] = []
        for candidate, target in reversed(promoted):
            try:
                if target.is_symlink():
                    rollback_errors.append(f"refusing to replace replacement symlink {target}")
                elif target.exists():
                    os.replace(target, candidate)
            except OSError as rollback_error:
                rollback_errors.append(f"restore staging artifact {candidate}: {rollback_error}")
        for backup, target in reversed(backups):
            try:
                if backup.exists():
                    os.replace(backup, target)
            except OSError as rollback_error:
                rollback_errors.append(f"restore {target}: {rollback_error}")
        rollback_complete = not rollback_errors
        detail = f"; rollback errors: {rollback_errors}" if rollback_errors else ""
        raise ApplicationError(f"atomic artifact-set finalization failed; prior final set was restored{detail}") from error
    finally:
        if transaction_complete or rollback_complete:
            for backup, _target in backups:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass

    return _set_artifact_states(current, ArtifactState.FINALIZED.value)


# Short aliases keep the low-level primitives discoverable without creating a
# second implementation path.
apply_plan = apply_application_plan
make_clean_candidate = accept_session_revisions
make_rejected_candidate = reject_session_revisions
stage_artifacts = stage_application
finalize_deliverable_set = finalize_artifact_set


__all__ = [
    "ApplicationAction",
    "ApplicationError",
    "ApplicationPlan",
    "SESSION_AUTHOR",
    "StagedApplication",
    "accept_session_revisions",
    "apply_application_plan",
    "apply_plan",
    "bind_staged_artifacts",
    "build_application_plan",
    "dump_application_plan",
    "finalize_artifact_set",
    "finalize_deliverable_set",
    "load_application_plan",
    "make_clean_candidate",
    "make_rejected_candidate",
    "prepare_application",
    "reject_session_revisions",
    "stage_application",
    "stage_artifacts",
    "validate_staged_artifacts",
]
