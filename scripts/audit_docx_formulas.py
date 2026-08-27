#!/usr/bin/env python3
"""Read-only structural and evidence-bound audit for DOCX formula artifacts.

The audit has two deliberately separate modes.  The historical command-line
flags continue to provide a useful structural report.  Supplying a manifest
and frozen application plan enables occurrence-level, revision, semantic, and
source-reconstruction checks; supplying the frozen job additionally produces
evidence that can be consumed as the #2 ``STRUCTURAL_AUDIT`` gate.

This module never derives or emits a delivery ``COMPLETE`` result.  That state
belongs to the shared lifecycle contract after every required gate has passed.
"""

from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.applicator import (  # noqa: E402
    ApplicationError,
    ApplicationPlan,
    load_application_plan,
    reject_session_revisions,
)
from word_formula_omml.canonical import CanonicalError, canonicalize_formula  # noqa: E402
from word_formula_omml.contract import (  # noqa: E402
    ContractError,
    FrozenJob,
    Manifest,
    load_job,
    load_manifest,
    verify_frozen_job,
)
from word_formula_omml.semantic import compare_omml_to_canonical  # noqa: E402


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"
XML = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W, "m": M, "r": R, "pr": PKG_R}
QW = lambda name: f"{{{W}}}{name}"
QM = lambda name: f"{{{M}}}{name}"

DEFAULT_ALLOWED = {"word/document.xml", "word/styles.xml", "word/settings.xml"}
TRACKED_REVISION_LOCALS = frozenset({"ins", "del", "pPrChange", "rPrChange"})
STORY_ROOTS = frozenset({"document", "hdr", "ftr", "footnotes", "endnotes", "comments"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AUDIT_EVIDENCE_ID_RE = re.compile(r"^audit-[0-9a-f]{32}$")
AUDIT_SCHEMA_VERSION = 1


class AuditError(RuntimeError):
    """Raised when an audit input or evidence contract cannot be consumed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--expected-formulas", type=int)
    parser.add_argument("--expected-author-insertions", type=int)
    parser.add_argument("--expected-author-deletions", type=int)
    parser.add_argument("--author")
    parser.add_argument("--residual", action="append", default=[], help="Regex forbidden in current text")
    parser.add_argument("--allow-part", action="append", default=[], help="Part allowed to differ from baseline")
    parser.add_argument("--require-cambria-math", action="store_true")
    parser.add_argument(
        "--semantic-index",
        type=Path,
        help="Generated-library index whose expected canonical semantics must be rechecked",
    )
    parser.add_argument("--manifest", type=Path, help="Frozen occurrence manifest for bound auditing")
    parser.add_argument(
        "--application-plan",
        "--plan",
        dest="application_plan",
        type=Path,
        help="Frozen W5 application plan for bound auditing",
    )
    parser.add_argument("--job", type=Path, help="Frozen #2 job for per-artifact evidence")
    parser.add_argument("--artifact-id", "--artifact", dest="artifact_id", help="Requested artifact ID or logical ID")
    parser.add_argument("--artifact-kind", help="Expected artifact kind (redlined or clean for W5 candidates)")
    parser.add_argument("--json", type=Path, help="Write the report to this JSON file")
    return parser.parse_args()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AuditError(f"value is not deterministic JSON: {error}") from error


def _read_zip_parts(data: bytes, label: str) -> dict[str, bytes]:
    if not isinstance(data, bytes):
        raise AuditError(f"{label} must be bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise AuditError(f"{label} contains duplicate package part names")
            for name in names:
                validation_name = name[:-1] if name.endswith("/") else name
                components = validation_name.split("/")
                if (
                    not validation_name
                    or "\\" in name
                    or name.startswith("/")
                    or any(component in {"", ".", ".."} for component in components)
                ):
                    raise AuditError(f"unsafe package part name in {label}: {name!r}")
            bad = archive.testzip()
            if bad:
                raise AuditError(f"ZIP CRC failure in {label}: {bad}")
            return {name: archive.read(name) for name in names if not name.endswith("/")}
    except AuditError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise AuditError(f"cannot read {label} as a DOCX ZIP package: {error}") from error


def read_package_bytes(data: bytes, label: str = "DOCX package") -> dict[str, bytes]:
    """Read and minimally validate package bytes without modifying them."""

    return _read_zip_parts(data, label)


def read_package(path: Path) -> dict[str, bytes]:
    try:
        return read_package_bytes(path.read_bytes(), f"DOCX {path}")
    except OSError as error:
        raise AuditError(f"cannot read DOCX {path}: {error}") from error


def xml(package: Mapping[str, bytes], part: str) -> ET.Element:
    if part not in package:
        raise AuditError(f"missing required part: {part}")
    try:
        return ET.fromstring(package[part])
    except ET.ParseError as error:
        raise AuditError(f"cannot parse XML package part {part}: {error}") from error


def _xml_roots(package: Mapping[str, bytes]) -> dict[str, ET.Element]:
    roots: dict[str, ET.Element] = {}
    for name, data in sorted(package.items()):
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            roots[name] = ET.fromstring(data)
        except ET.ParseError as error:
            raise AuditError(f"cannot parse XML package part {name}: {error}") from error
    return roots


def _story_roots(
    package: Mapping[str, bytes],
    roots: Mapping[str, ET.Element] | None = None,
) -> dict[str, ET.Element]:
    parsed = roots if roots is not None else _xml_roots(package)
    return {
        name: root
        for name, root in parsed.items()
        if root.tag.startswith("{" + W + "}")
        and root.tag.rsplit("}", 1)[-1] in STORY_ROOTS
        and not name.endswith(".rels")
    }


def _blocks(root: ET.Element) -> list[ET.Element]:
    parents = _parent_map(root)
    blocks: list[ET.Element] = []
    for node in root.iter():
        if node.tag == QW("p"):
            blocks.append(node)
            continue
        if node.tag != QM("oMathPara"):
            continue
        current = parents.get(id(node))
        nested = False
        while current is not None:
            if current.tag == QW("p"):
                nested = True
                break
            current = parents.get(id(current))
        if not nested:
            blocks.append(node)
    return blocks


def _visible_paragraph_text(paragraph: ET.Element, replacements: Mapping[int, str] | None = None) -> str:
    """Return accepted Word text, optionally replacing generated OMML nodes."""

    parts: list[str] = []
    replacements = replacements or {}

    def visit(node: ET.Element) -> None:
        if id(node) in replacements:
            parts.append(replacements[id(node)])
            return
        if node.tag == QW("del") or node.tag == QW("delText"):
            return
        if node.tag == QW("t"):
            parts.append(node.text or "")
            return
        for child in list(node):
            visit(child)

    visit(paragraph)
    return "".join(parts)


def current_text(root: ET.Element) -> str:
    return "\n".join(_visible_paragraph_text(paragraph) for paragraph in root.findall(".//w:p", NS))


def media_hashes(package: Mapping[str, bytes]) -> dict[str, str]:
    return {
        name: sha256(data)
        for name, data in sorted(package.items())
        if name.startswith("word/media/")
    }


def relationship_targets(package: Mapping[str, bytes]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for name, data in sorted(package.items()):
        if not name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise AuditError(f"cannot parse relationship part {name}: {error}") from error
        result[name] = sorted(
            ({key: value for key, value in relation.attrib.items()} for relation in root),
            key=lambda item: (item.get("Id", ""), item.get("Target", ""), item.get("Type", "")),
        )
    return result


def comparable_part(name: str, data: bytes | None) -> bytes | str | None:
    if data is None:
        return None
    if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise AuditError(f"cannot parse comparable XML part {name}: {error}") from error
        for node in root.iter():
            if node.text is not None and not node.text.strip():
                node.text = None
            if node.tail is not None and not node.tail.strip():
                node.tail = None
        return ET.canonicalize(
            xml_data=ET.tostring(root, encoding="unicode"),
            rewrite_prefixes=True,
        )
    return data


def revision_authors(root: ET.Element, tag: str) -> dict[str, int]:
    return dict(collections.Counter(node.get(QW("author"), "") for node in root.findall(f".//w:{tag}", NS)))


def _math_runs_without_cambria(root: ET.Element) -> int:
    missing = 0
    for equation in root.findall(".//m:oMath", NS):
        for run in equation.findall(".//m:r", NS):
            fonts = run.findall(".//w:rFonts", NS)
            if not any(
                font.get(QW("ascii")) == "Cambria Math" and font.get(QW("hAnsi")) == "Cambria Math"
                for font in fonts
            ):
                missing += 1
    return missing


def inspect(path: Path, package: Mapping[str, bytes], *, raw_bytes: bytes | None = None) -> tuple[dict, ET.Element]:
    document = xml(package, "word/document.xml")
    styles = xml(package, "word/styles.xml") if "word/styles.xml" in package else None
    equations = document.findall(".//m:oMath", NS)
    story_counts = {
        part: len(root.findall(".//m:oMath", NS))
        for part, root in _story_roots(package).items()
    }
    report = {
        "path": str(path),
        "sha256": sha256(raw_bytes) if raw_bytes is not None else (sha256(path.read_bytes()) if path.is_file() else None),
        "parts": len(package),
        "paragraphs": len(document.findall(".//w:p", NS)),
        "omath": len(equations),
        "omath_para": len(document.findall(".//m:oMathPara", NS)),
        "omath_by_part": story_counts,
        "drawings": len(document.findall(".//w:drawing", NS)),
        "sections": len(document.findall(".//w:sectPr", NS)),
        "comments": len(document.findall(".//w:commentRangeStart", NS)),
        "bookmarks": len(document.findall(".//w:bookmarkStart", NS)),
        "fields": len(document.findall(".//w:fldChar", NS)),
        "hyperlinks": len(document.findall(".//w:hyperlink", NS)),
        "content_controls": len(document.findall(".//w:sdt", NS)),
        "insertions_by_author": revision_authors(document, "ins"),
        "deletions_by_author": revision_authors(document, "del"),
        "paragraph_property_changes": len(document.findall(".//w:pPrChange", NS)),
        "run_property_changes": len(document.findall(".//w:rPrChange", NS)),
        "styles": len(styles.findall(".//w:style", NS)) if styles is not None else 0,
        "media": media_hashes(package),
        "relationships": relationship_targets(package),
        "math_runs_without_explicit_cambria": _math_runs_without_cambria(document),
    }
    return report, document


def audit_semantic_index(document: ET.Element, index_path: Path) -> tuple[list[dict], list[str]]:
    """Recompute semantic results with the shared W3B bridge.

    The index supplies identity and expected canonical values only.  Its prior
    semantic result is deliberately ignored so a changed candidate cannot
    reuse stale PASS evidence.
    """

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot load semantic index {index_path}: {error}") from error
    if not isinstance(index, dict) or not isinstance(index.get("formulas"), list):
        raise AuditError("semantic index must contain a formulas array")
    schema_version = index.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise AuditError(f"unsupported semantic index schema_version {schema_version!r}")
    if not index["formulas"]:
        raise AuditError("semantic index formulas array must not be empty")
    paragraphs = document.findall(".//w:body//w:p", NS)
    results: list[dict] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for position, entry in enumerate(index["formulas"], 1):
        if not isinstance(entry, dict):
            errors.append(f"semantic index formulas[{position}] is not an object")
            continue
        formula_id = entry.get("id")
        if not isinstance(formula_id, str) or not formula_id.strip():
            errors.append(f"semantic index formulas[{position}] has an invalid id")
            formula_id = f"index-{position}"
        elif formula_id in seen_ids:
            errors.append(f"{formula_id}: duplicate semantic index id")
        seen_ids.add(formula_id)
        expected = entry.get("canonical")
        paragraph_number = entry.get("equation_paragraph")
        if expected is None:
            errors.append(f"{formula_id}: semantic index has no expected canonical value")
            continue
        if (
            not isinstance(paragraph_number, int)
            or isinstance(paragraph_number, bool)
            or paragraph_number < 1
            or paragraph_number > len(paragraphs)
        ):
            errors.append(f"{formula_id}: equation paragraph {paragraph_number!r} is out of range")
            continue
        marker_number = entry.get("marker_paragraph")
        if (
            not isinstance(marker_number, int)
            or isinstance(marker_number, bool)
            or marker_number < 1
            or marker_number > len(paragraphs)
        ):
            errors.append(f"{formula_id}: marker paragraph {marker_number!r} is out of range")
        else:
            marker_text = "".join(paragraphs[marker_number - 1].itertext()).strip()
            if marker_text != f"OMML_ID:{formula_id}":
                errors.append(f"{formula_id}: marker paragraph does not identify this formula")
            if marker_number + 1 != paragraph_number:
                errors.append(f"{formula_id}: marker and equation paragraphs are not adjacent")
        equations = paragraphs[paragraph_number - 1].findall(".//m:oMath", NS)
        if len(equations) != 1:
            errors.append(f"{formula_id}: equation paragraph has {len(equations)} m:oMath nodes, expected 1")
            continue
        actual_xml = ET.tostring(equations[0], encoding="utf-8")
        expected_hash = entry.get("omml_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            errors.append(f"{formula_id}: semantic index has an invalid or missing OMML candidate hash")
        elif expected_hash != hashlib.sha256(actual_xml).hexdigest():
            errors.append(f"{formula_id}: OMML candidate hash does not match semantic index")
        source_latex = entry.get("latex")
        if not isinstance(source_latex, str) or not source_latex.strip():
            errors.append(f"{formula_id}: semantic index has no approved LaTeX source")
            source_latex = None
        result = compare_omml_to_canonical(equations[0], expected, source_latex=source_latex)
        serialized = {"id": formula_id, "equation_paragraph": paragraph_number, **result.to_dict()}
        results.append(serialized)
        if not result.passed:
            errors.append(f"{formula_id}: semantic {result.status}: {result.reason}")
    if len(results) != len(index["formulas"]):
        errors.append("semantic index entries could not all be rechecked")
    return results, errors


def _direct_child(node: ET.Element | None, tag: str) -> ET.Element | None:
    if node is None:
        return None
    matches = [child for child in node if child.tag == tag]
    if len(matches) > 1:
        raise AuditError(f"XML node contains duplicate {_local(tag)} children")
    return matches[0] if matches else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else str(tag)


def _attribute(node: ET.Element | None, namespace: str, local: str) -> str | None:
    if node is None:
        return None
    return node.get(f"{{{namespace}}}{local}") or node.get(local)


def _parent_map(root: ET.Element) -> dict[int, ET.Element]:
    return {id(child): parent for parent in root.iter() for child in list(parent)}


def _is_session_revision(node: ET.Element, author: str | None) -> bool:
    return (
        author is not None
        and node.tag in {QW("ins"), QW("del")}
        and _attribute(node, W, "author") == author
    )


def _element_location(
    root: ET.Element,
    node: ET.Element,
    parents: Mapping[int, ET.Element],
    *,
    ignored_ids: set[int] | None = None,
    ignored_revision_author: str | None = None,
) -> str:
    """Build a stable path while ignoring expected remediation insertions."""

    ignored_ids = ignored_ids or set()
    segments: list[str] = []
    current: ET.Element | None = node
    while current is not None:
        parent = parents.get(id(current))
        if parent is None:
            segments.append(f"{_local(current.tag)}[1]")
            break
        siblings = [
            child
            for child in list(parent)
            if id(child) not in ignored_ids and not _is_session_revision(child, ignored_revision_author)
        ]
        same_tag = [child for child in siblings if child.tag == current.tag]
        try:
            ordinal = same_tag.index(current) + 1
        except ValueError:
            ordinal = 1
        segments.append(f"{_local(current.tag)}[{ordinal}]")
        current = parent
    return "/".join(reversed(segments))


def _canonical_xml(node: ET.Element) -> str:
    return ET.canonicalize(
        xml_data=ET.tostring(node, encoding="unicode"),
        rewrite_prefixes=True,
    )


def _revision_records(
    package: Mapping[str, bytes],
    *,
    ignored_author: str | None = None,
    ignored_ids: set[int] | None = None,
    roots: Mapping[str, ET.Element] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for part, root in _story_roots(package, roots).items():
        if part.endswith(".rels"):
            continue
        parents = _parent_map(root)
        seen: set[str] = set()
        for node in root.iter():
            if _local(node.tag) not in TRACKED_REVISION_LOCALS or not node.tag.startswith("{" + W + "}"):
                continue
            author = _attribute(node, W, "author") or ""
            if ignored_author is not None and author == ignored_author and _local(node.tag) in {"ins", "del"}:
                continue
            identity = {
                "part": part,
                "kind": _local(node.tag),
                "author": author,
                "id": _attribute(node, W, "id") or "",
                "location": _element_location(
                    root,
                    node,
                    parents,
                    ignored_ids=ignored_ids,
                    ignored_revision_author=ignored_author,
                ),
            }
            key = _canonical_json(identity)
            if key in seen:
                errors.append(f"duplicate pre-existing revision identity: {key}")
            seen.add(key)
            records.append(
                {
                    "identity": identity,
                    "fingerprint": sha256(_canonical_xml(node).encode("utf-8")),
                }
            )
    return records, errors


def _omml_records(
    package: Mapping[str, bytes],
    *,
    ignored_ids: set[int] | None = None,
    ignored_revision_author: str | None = None,
    roots: Mapping[str, ET.Element] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    ignored_ids = ignored_ids or set()
    for part, root in _story_roots(package, roots).items():
        if part.endswith(".rels"):
            continue
        parents = _parent_map(root)
        for node in root.iter():
            if node.tag != QM("oMath") or id(node) in ignored_ids:
                continue
            if any(_is_session_revision(ancestor, ignored_revision_author) for ancestor in _ancestors(node, parents)):
                continue
            identity = {
                "part": part,
                "location": _element_location(
                    root,
                    node,
                    parents,
                    ignored_ids=ignored_ids,
                    ignored_revision_author=ignored_revision_author,
                ),
            }
            records.append(
                {
                    "identity": identity,
                    "fingerprint": sha256(_canonical_xml(node).encode("utf-8")),
                }
            )
    return records


def _omml_nodes_by_location(
    package: Mapping[str, bytes],
    *,
    ignored_ids: set[int] | None = None,
    ignored_revision_author: str | None = None,
    roots: Mapping[str, ET.Element] | None = None,
) -> dict[tuple[str, str], list[ET.Element]]:
    """Index surviving ``m:oMath`` nodes by the stable inventory location."""

    nodes: dict[tuple[str, str], list[ET.Element]] = collections.defaultdict(list)
    ignored_ids = ignored_ids or set()
    for part, root in _story_roots(package, roots).items():
        parents = _parent_map(root)
        for node in root.iter():
            if node.tag != QM("oMath") or id(node) in ignored_ids:
                continue
            if any(_is_session_revision(ancestor, ignored_revision_author) for ancestor in _ancestors(node, parents)):
                continue
            location = "/" + _element_location(
                root,
                node,
                parents,
                ignored_ids=ignored_ids,
                ignored_revision_author=ignored_revision_author,
            )
            nodes[(part, location)].append(node)
    return dict(nodes)


def _inventory_node_path(row: Mapping[str, Any]) -> str | None:
    extensions = row.get("extensions")
    inventory = extensions.get("inventory") if isinstance(extensions, Mapping) else None
    path = inventory.get("node_path") if isinstance(inventory, Mapping) else None
    return path if isinstance(path, str) and path.startswith("/") else None


def _ancestors(node: ET.Element, parents: Mapping[int, ET.Element]) -> list[ET.Element]:
    result: list[ET.Element] = []
    current = parents.get(id(node))
    while current is not None:
        result.append(current)
        current = parents.get(id(current))
    return result


def _record_map(records: Sequence[Mapping[str, Any]], label: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for record in records:
        key = _canonical_json(record["identity"])
        if key in result:
            errors.append(f"duplicate {label} identity: {key}")
        result[key] = dict(record)
    return result, errors


def _compare_records(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    label: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    base_map, errors = _record_map(baseline, label)
    candidate_map, candidate_errors = _record_map(candidate, label)
    errors.extend(candidate_errors)
    added = [candidate_map[key] for key in sorted(set(candidate_map) - set(base_map))]
    removed = [base_map[key] for key in sorted(set(base_map) - set(candidate_map))]
    changed = [
        {
            "identity": candidate_map[key]["identity"],
            "baseline_fingerprint": base_map[key]["fingerprint"],
            "candidate_fingerprint": candidate_map[key]["fingerprint"],
        }
        for key in sorted(set(base_map) & set(candidate_map))
        if base_map[key]["fingerprint"] != candidate_map[key]["fingerprint"]
    ]
    return {"added": added, "removed": removed, "changed": changed}, errors


def _package_diff(
    baseline: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
    allowed: set[str],
) -> tuple[dict[str, Any], list[str]]:
    added = sorted(set(candidate) - set(baseline))
    removed = sorted(set(baseline) - set(candidate))
    changed = sorted(
        part
        for part in set(candidate) & set(baseline)
        if comparable_part(part, candidate[part]) != comparable_part(part, baseline[part])
    )
    changes: list[dict[str, str]] = []
    for kind, parts in (("added", added), ("removed", removed), ("changed", changed)):
        for part in parts:
            if part in allowed:
                reason = "explicitly allowed audit/application part"
                scope = "allowed"
            else:
                reason = "part is outside the explicit audit allowlist"
                scope = "protected"
            changes.append({"part": part, "kind": kind, "scope": scope, "reason": reason})
    protected = [item for item in changes if item["scope"] == "protected"]
    errors = []
    if protected:
        errors.append("protected package parts changed: " + str([item["part"] for item in protected]))
    return {
        "allowed_parts": sorted(allowed),
        "added": added,
        "removed": removed,
        "changed": changed,
        "changes": changes,
        "protected_changes": protected,
    }, errors


def _run_fragment(run: ET.Element, value: str) -> ET.Element:
    """Build the exact ordinary-run fragment emitted by the applicator."""

    fragment = copy.deepcopy(run)
    text_nodes = [child for child in fragment if child.tag == QW("t")]
    if len(text_nodes) != 1 or any(child.tag not in {QW("rPr"), QW("t")} for child in fragment):
        raise AuditError("planned clean replacement source run is not a plain text run")
    text_node = text_nodes[0]
    text_node.attrib.clear()
    if value.startswith((" ", "\t")) or value.endswith((" ", "\t")):
        text_node.set(f"{{{XML}}}space", "preserve")
    text_node.text = value
    return fragment


def _child_path(root: ET.Element, node: ET.Element) -> tuple[int, ...]:
    """Return a path based on child indexes for locating a node in a clone."""

    parents = _parent_map(root)
    indexes: list[int] = []
    current = node
    while current is not root:
        parent = parents.get(id(current))
        if parent is None:
            raise AuditError("candidate mapping node is detached from its story root")
        try:
            indexes.append(list(parent).index(current))
        except ValueError as error:
            raise AuditError("candidate mapping node is detached from its parent") from error
        current = parent
    return tuple(reversed(indexes))


def _node_at_path(root: ET.Element, path: Sequence[int]) -> ET.Element:
    current = root
    for index in path:
        children = list(current)
        if index < 0 or index >= len(children):
            raise AuditError("candidate mapping path is no longer valid")
        current = children[index]
    return current


def _baseline_run_binding(
    baseline_root: ET.Element,
    paragraph_number: int,
    action: Any,
) -> tuple[ET.Element, int, str] | None:
    baseline_blocks = _blocks(baseline_root)
    if (
        paragraph_number < 1
        or paragraph_number > len(baseline_blocks)
        or baseline_blocks[paragraph_number - 1].tag != QW("p")
    ):
        return None
    paragraph = baseline_blocks[paragraph_number - 1]
    runs = paragraph.findall(".//w:r", NS)
    run_index = action.run_index
    if (
        run_index is None
        or run_index < 1
        or run_index > len(runs)
        or _parent_map(baseline_root).get(id(runs[run_index - 1])) is not paragraph
    ):
        return None
    run = runs[run_index - 1]
    text_nodes = [child for child in run if child.tag == QW("t")]
    if len(text_nodes) != 1 or any(child.tag not in {QW("rPr"), QW("t")} for child in run):
        return None
    return run, list(paragraph).index(run), text_nodes[0].text or ""


def _redlined_story_projection(
    baseline_root: ET.Element,
    rejected_root: ET.Element,
    actions: Sequence[Any],
) -> tuple[ET.Element, list[str]]:
    """Restore planned rejected replacement fragments to the source run."""

    errors: list[str] = []
    rejected_blocks = _blocks(rejected_root)
    placements: list[tuple[tuple[int, ...], int, int, str, ET.Element]] = []
    by_paragraph: dict[int, list[tuple[int, int, int, Any, ET.Element, str]]] = collections.defaultdict(list)
    for action in actions:
        occurrence_id = action.occurrence_id
        paragraph_number = action.paragraph or 0
        binding = _baseline_run_binding(baseline_root, paragraph_number, action)
        if binding is None:
            errors.append(f"{occurrence_id}: redlined story projection source run binding is missing")
            continue
        baseline_run, base_index, original_text = binding
        start = action.run_start
        end = action.run_end
        if start is None or end is None or start < 0 or end <= start or end > len(original_text):
            errors.append(f"{occurrence_id}: redlined story projection source range is invalid")
            continue
        by_paragraph[paragraph_number].append(
            (base_index, int(start > 0), int(end < len(original_text)), action, baseline_run, original_text)
        )

    for paragraph_number, paragraph_actions in by_paragraph.items():
        if (
            paragraph_number < 1
            or paragraph_number > len(rejected_blocks)
            or rejected_blocks[paragraph_number - 1].tag != QW("p")
        ):
            for _base_index, _prefix, _suffix, action, _run, _text in paragraph_actions:
                errors.append(f"{action.occurrence_id}: redlined story projection paragraph binding is missing")
            continue
        rejected_paragraph = rejected_blocks[paragraph_number - 1]
        rejected_children = list(rejected_paragraph)
        prior_delta = 0
        for base_index, prefix, suffix, action, baseline_run, original_text in sorted(
            paragraph_actions,
            key=lambda item: (item[0], item[3].occurrence_id),
        ):
            source_index = base_index + prior_delta + prefix
            start = source_index - prefix
            end = source_index + suffix + 1
            if start < 0 or end > len(rejected_children):
                errors.append(f"{action.occurrence_id}: redlined story projection run fragment binding is missing")
                prior_delta += prefix + suffix
                continue
            expected_fragments = []
            if prefix:
                expected_fragments.append(
                    (source_index - 1, _run_fragment(baseline_run, original_text[: action.run_start]), "prefix")
                )
            expected_fragments.append(
                (source_index, _run_fragment(baseline_run, original_text[action.run_start : action.run_end]), "source")
            )
            if suffix:
                expected_fragments.append(
                    (source_index + 1, _run_fragment(baseline_run, original_text[action.run_end :]), "suffix")
                )
            fragment_error = False
            for index, expected, label in expected_fragments:
                actual = rejected_children[index]
                if _canonical_xml(actual) != _canonical_xml(expected):
                    errors.append(f"{action.occurrence_id}: rejected candidate {label} run fragment changed")
                    fragment_error = True
            if not fragment_error:
                source_path = _child_path(rejected_root, rejected_children[source_index])
                placements.append((source_path, prefix, suffix, action.occurrence_id, baseline_run))
            prior_delta += prefix + suffix

    ranges: dict[tuple[int, ...], list[tuple[int, int, str]]] = collections.defaultdict(list)
    for path, prefix, suffix, occurrence_id, _baseline_run in placements:
        parent_path = path[:-1]
        source_index = path[-1]
        ranges[parent_path].append((source_index - prefix, source_index + suffix + 1, occurrence_id))
    for parent_path, parent_ranges in ranges.items():
        ordered = sorted(parent_ranges)
        for left, right in zip(ordered, ordered[1:]):
            if left[1] > right[0]:
                errors.append(f"redlined story projection has overlapping planned replacements: {left[2]}, {right[2]}")

    projected = copy.deepcopy(rejected_root)
    for path, prefix, suffix, occurrence_id, baseline_run in sorted(placements, key=lambda item: item[0], reverse=True):
        try:
            parent = _node_at_path(projected, path[:-1])
            source_index = path[-1]
            start = source_index - prefix
            end = source_index + suffix + 1
            children = list(parent)
            if start < 0 or end > len(children) or children[source_index].tag != QW("r"):
                raise AuditError("redlined candidate replacement path is no longer valid")
            for child in children[start:end]:
                parent.remove(child)
            parent.insert(start, copy.deepcopy(baseline_run))
        except AuditError as error:
            errors.append(f"{occurrence_id}: {error}")
    return projected, errors


def _clean_story_projection(
    baseline_root: ET.Element,
    candidate_root: ET.Element,
    actions: Sequence[Any],
    mapping: Mapping[str, ET.Element],
) -> tuple[ET.Element, list[str]]:
    """Restore only the planned clean replacements for a full story compare.

    The candidate must retain the exact prefix/suffix run fragments that W5
    derives from the source run.  Replacing the generated equation and those
    checked fragments with the original source run then makes an exact XML
    comparison possible without masking unrelated changes in the paragraph.
    """

    errors: list[str] = []
    candidate_parents = _parent_map(candidate_root)
    candidate_blocks = _blocks(candidate_root)
    baseline_blocks = _blocks(baseline_root)
    descriptors: list[tuple[tuple[int, ...], int, int, str, ET.Element]] = []
    ranges: dict[tuple[int, ...], list[tuple[int, int, str]]] = collections.defaultdict(list)

    for action in actions:
        occurrence_id = action.occurrence_id
        equation = mapping.get(occurrence_id)
        if equation is None:
            errors.append(f"{occurrence_id}: clean story projection has no mapped generated OMML")
            continue
        paragraph_number = action.paragraph or 0
        if (
            paragraph_number < 1
            or paragraph_number > len(candidate_blocks)
            or candidate_blocks[paragraph_number - 1].tag != QW("p")
            or paragraph_number > len(baseline_blocks)
            or baseline_blocks[paragraph_number - 1].tag != QW("p")
        ):
            errors.append(f"{occurrence_id}: clean story projection paragraph binding is missing")
            continue
        candidate_paragraph = candidate_blocks[paragraph_number - 1]
        baseline_paragraph = baseline_blocks[paragraph_number - 1]
        if candidate_parents.get(id(equation)) is not candidate_paragraph:
            errors.append(f"{occurrence_id}: generated OMML is not a direct child of its planned paragraph")
            continue
        try:
            equation_index = list(candidate_paragraph).index(equation)
        except ValueError:
            errors.append(f"{occurrence_id}: generated OMML is detached from its planned paragraph")
            continue
        run_index = action.run_index
        baseline_runs = baseline_paragraph.findall(".//w:r", NS)
        if (
            run_index is None
            or run_index < 1
            or run_index > len(baseline_runs)
            or _parent_map(baseline_root).get(id(baseline_runs[run_index - 1])) is not baseline_paragraph
        ):
            errors.append(f"{occurrence_id}: clean story projection source run binding is missing")
            continue
        baseline_run = baseline_runs[run_index - 1]
        source_nodes = [child for child in baseline_run if child.tag == QW("t")]
        if len(source_nodes) != 1 or any(child.tag not in {QW("rPr"), QW("t")} for child in baseline_run):
            errors.append(f"{occurrence_id}: clean story projection source run is not plain text")
            continue
        original_text = source_nodes[0].text or ""
        start = action.run_start
        end = action.run_end
        if (
            start is None
            or end is None
            or start < 0
            or end <= start
            or end > len(original_text)
        ):
            errors.append(f"{occurrence_id}: clean story projection source range is invalid")
            continue
        prefix = int(start > 0)
        suffix = int(end < len(original_text))
        candidate_children = list(candidate_paragraph)
        if equation_index < prefix or equation_index + suffix >= len(candidate_children):
            errors.append(f"{occurrence_id}: clean story projection run fragment binding is missing")
            continue
        expected_fragments = []
        if prefix:
            expected_fragments.append((equation_index - 1, _run_fragment(baseline_run, original_text[:start]), "prefix"))
        if suffix:
            expected_fragments.append((equation_index + 1, _run_fragment(baseline_run, original_text[end:]), "suffix"))
        fragment_error = False
        for index, expected, label in expected_fragments:
            actual = candidate_children[index]
            if _canonical_xml(actual) != _canonical_xml(expected):
                errors.append(f"{occurrence_id}: clean candidate {label} run fragment changed")
                fragment_error = True
        if fragment_error:
            continue
        path = _child_path(candidate_root, equation)
        parent_path = path[:-1]
        ranges[parent_path].append((equation_index - prefix, equation_index + suffix + 1, occurrence_id))
        descriptors.append((path, prefix, suffix, occurrence_id, baseline_run))

    for parent_path, parent_ranges in ranges.items():
        ordered = sorted(parent_ranges)
        for left, right in zip(ordered, ordered[1:]):
            if left[1] > right[0]:
                errors.append(f"clean story projection has overlapping planned replacements: {left[2]}, {right[2]}")

    projected = copy.deepcopy(candidate_root)
    for path, prefix, suffix, occurrence_id, baseline_run in sorted(
        descriptors,
        key=lambda item: item[0],
        reverse=True,
    ):
        try:
            parent = _node_at_path(projected, path[:-1])
            equation_index = path[-1]
            start = equation_index - prefix
            end = equation_index + suffix + 1
            children = list(parent)
            if start < 0 or end > len(children) or children[equation_index].tag != QM("oMath"):
                raise AuditError("clean candidate replacement path is no longer valid")
            for child in children[start:end]:
                parent.remove(child)
            parent.insert(start, copy.deepcopy(baseline_run))
        except AuditError as error:
            errors.append(f"{occurrence_id}: {error}")
    return projected, errors


def _audit_story_content(
    baseline: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
    plan: ApplicationPlan,
    kind: str,
    mapping: Mapping[str, ET.Element] | None = None,
    baseline_roots: Mapping[str, ET.Element] | None = None,
    candidate_roots: Mapping[str, ET.Element] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Detect drift outside the blocks deliberately changed by the plan."""

    errors: list[str] = []
    normalized_candidate: Mapping[str, bytes] = candidate
    if kind == "redlined" and plan.applied_count:
        try:
            normalized_candidate = read_package_bytes(
                reject_session_revisions(_serialize_package_for_audit(candidate), plan),
                "rejected story-content candidate",
            )
        except (ApplicationError, AuditError, ContractError) as error:
            return {"status": "FAIL", "stories": []}, [f"story-content normalization failed: {error}"]
    elif kind not in {"clean", "redlined", "unchanged"}:
        return {"status": "FAIL", "stories": []}, [f"cannot audit story content for candidate kind {kind!r}"]

    base_stories = _story_roots(baseline, baseline_roots)
    candidate_stories = _story_roots(normalized_candidate, candidate_roots if normalized_candidate is candidate else None)
    records: list[dict[str, Any]] = []
    for part in sorted(set(base_stories) | set(candidate_stories)):
        base_root = base_stories.get(part)
        candidate_root = candidate_stories.get(part)
        if base_root is None or candidate_root is None:
            errors.append(f"{part}: story root is missing from baseline or candidate")
            continue
        base_blocks = _blocks(base_root)
        candidate_blocks = _blocks(candidate_root)
        record: dict[str, Any] = {
            "part": part,
            "baseline_blocks": len(base_blocks),
            "candidate_blocks": len(candidate_blocks),
        }
        if len(base_blocks) != len(candidate_blocks):
            record["status"] = "FAIL"
            errors.append(f"{part}: story block count changed {len(base_blocks)} -> {len(candidate_blocks)}")
        elif kind == "redlined":
            part_actions = [
                action
                for action in plan.actions
                if action.decision == "APPLY" and (action.package_part or "word/document.xml") == part
            ]
            projected, projection_errors = _redlined_story_projection(base_root, candidate_root, part_actions)
            errors.extend(projection_errors)
            record["status"] = "PASS" if not projection_errors and _canonical_xml(base_root) == _canonical_xml(projected) else "FAIL"
            if record["status"] != "PASS" and not projection_errors:
                errors.append(f"{part}: unplanned story content drift detected")
        elif kind == "clean":
            part_actions = [
                action
                for action in plan.actions
                if action.decision == "APPLY" and (action.package_part or "word/document.xml") == part
            ]
            if mapping is None:
                record["status"] = "FAIL"
                errors.append(f"{part}: clean story projection has no generated-OMML mapping")
            else:
                projected, projection_errors = _clean_story_projection(
                    base_root,
                    candidate_root,
                    part_actions,
                    mapping,
                )
                errors.extend(projection_errors)
                record["status"] = "PASS" if not projection_errors and _canonical_xml(base_root) == _canonical_xml(projected) else "FAIL"
                if record["status"] != "PASS" and not projection_errors:
                    errors.append(f"{part}: unplanned story content drift detected")
        else:
            record["status"] = "PASS" if _canonical_xml(base_root) == _canonical_xml(candidate_root) else "FAIL"
            if record["status"] != "PASS":
                errors.append(f"{part}: unplanned story content drift detected")
        record["affected_blocks"] = sorted(
            {
                action.paragraph
                for action in plan.actions
                if action.decision == "APPLY"
                and (action.package_part or "word/document.xml") == part
                and action.paragraph is not None
            }
        )
        records.append(record)
    if set(base_stories) != set(candidate_stories):
        errors.append("story parts changed between baseline and candidate")
    return {"status": "PASS" if not errors else "FAIL", "stories": records}, errors


def _without_track_revisions(root: ET.Element) -> ET.Element:
    result = copy.deepcopy(root)
    parents = _parent_map(result)
    for node in list(result.iter()):
        if node.tag == QW("trackRevisions"):
            parent = parents.get(id(node))
            if parent is not None:
                parent.remove(node)
    return result


def _audit_settings_drift(
    baseline: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
    plan: ApplicationPlan,
) -> tuple[dict[str, Any], list[str]]:
    """Allow only the applicator's explicit tracked-revision setting change."""

    part = "word/settings.xml"
    baseline_data = baseline.get(part)
    candidate_data = candidate.get(part)
    if baseline_data is None or candidate_data is None:
        if baseline_data == candidate_data and not plan.applied_count:
            return {"status": "PASS", "track_revisions": 0}, []
        return {"status": "FAIL", "track_revisions": None}, [f"{part}: settings part is missing from baseline or candidate"]
    try:
        baseline_root = ET.fromstring(baseline_data)
        candidate_root = ET.fromstring(candidate_data)
    except ET.ParseError as error:
        return {"status": "FAIL", "track_revisions": None}, [f"{part}: cannot parse settings XML: {error}"]
    baseline_tracking = baseline_root.findall(".//w:trackRevisions", NS)
    candidate_tracking = candidate_root.findall(".//w:trackRevisions", NS)
    errors: list[str] = []
    if plan.applied_count and not baseline_tracking:
        if len(candidate_tracking) != 1 or list(candidate_tracking[0]) or candidate_tracking[0].attrib:
            errors.append(f"{part}: candidate does not contain exactly the applicator-created trackRevisions setting")
    elif len(baseline_tracking) != len(candidate_tracking) or any(
        _canonical_xml(left) != _canonical_xml(right)
        for left, right in zip(baseline_tracking, candidate_tracking)
    ):
        errors.append(f"{part}: pre-existing trackRevisions settings changed")
    if _canonical_xml(_without_track_revisions(baseline_root)) != _canonical_xml(
        _without_track_revisions(candidate_root)
    ):
        errors.append(f"{part}: unplanned settings content drift detected")
    return {
        "status": "PASS" if not errors else "FAIL",
        "baseline_track_revisions": len(baseline_tracking),
        "candidate_track_revisions": len(candidate_tracking),
    }, errors


def _part_formula_counts(
    package: Mapping[str, bytes],
    roots: Mapping[str, ET.Element] | None = None,
) -> dict[str, int]:
    return {
        part: len(root.findall(".//m:oMath", NS))
        for part, root in _story_roots(package, roots).items()
    }


def _paragraph_for_action(
    package: Mapping[str, bytes],
    action: Any,
    roots: Mapping[str, ET.Element] | None = None,
) -> tuple[str, ET.Element, ET.Element] | None:
    part = action.package_part or "word/document.xml"
    story_roots = _story_roots(package, roots)
    root = story_roots.get(part)
    if root is None or action.paragraph is None or action.paragraph < 1:
        return None
    blocks = _blocks(root)
    if action.paragraph > len(blocks) or blocks[action.paragraph - 1].tag != QW("p"):
        return None
    return part, root, blocks[action.paragraph - 1]


def _omml_hash(node: ET.Element) -> str:
    return sha256(ET.tostring(node, encoding="utf-8"))


def _session_insertion_nodes(
    package: Mapping[str, bytes],
    plan: ApplicationPlan,
    roots: Mapping[str, ET.Element] | None = None,
) -> tuple[dict[str, ET.Element], list[str]]:
    found: dict[str, ET.Element] = {}
    errors: list[str] = []
    expected = {
        int(action.insertion_revision_id): action
        for action in plan.actions
        if action.decision == "APPLY" and action.insertion_revision_id is not None
    }
    for part, root in _story_roots(package, roots).items():
        parents = _parent_map(root)
        blocks = _blocks(root)
        for wrapper in root.iter(QW("ins")):
            if _attribute(wrapper, W, "author") != plan.revision_author:
                continue
            raw_id = _attribute(wrapper, W, "id")
            try:
                revision_id = int(raw_id) if raw_id is not None else -1
            except (TypeError, ValueError):
                revision_id = -1
            action = expected.get(revision_id)
            if action is None:
                continue
            expected_part = action.package_part or "word/document.xml"
            parent = parents.get(id(wrapper))
            actual_paragraph = None
            if parent is not None and parent.tag == QW("p"):
                try:
                    actual_paragraph = blocks.index(parent) + 1
                except ValueError:
                    actual_paragraph = None
            if part != expected_part or actual_paragraph != action.paragraph:
                errors.append(
                    f"{action.occurrence_id}: session insertion is outside its frozen "
                    f"location ({part} paragraph {actual_paragraph})"
                )
                continue
            if parent is None:
                errors.append(f"{action.occurrence_id}: session insertion has no frozen replacement parent")
                continue
            siblings = list(parent)
            wrapper_index = siblings.index(wrapper)
            if (
                wrapper_index == 0
                or siblings[wrapper_index - 1].tag != QW("del")
                or _attribute(siblings[wrapper_index - 1], W, "author") != plan.revision_author
                or _attribute(siblings[wrapper_index - 1], W, "id") != str(action.deletion_revision_id)
            ):
                errors.append(
                    f"{action.occurrence_id}: session insertion is not adjacent to its frozen deletion"
                )
                continue
            equations = wrapper.findall(".//m:oMath", NS)
            if len(equations) != 1:
                errors.append(f"{action.occurrence_id}: session insertion has {len(equations)} equations")
                continue
            if action.occurrence_id in found:
                errors.append(f"{action.occurrence_id}: duplicate session insertion mapping")
                continue
            found[action.occurrence_id] = equations[0]
    return found, errors


def _candidate_kind(
    package: Mapping[str, bytes],
    plan: ApplicationPlan,
    requested: str | None,
    roots: Mapping[str, ET.Element] | None = None,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    session_count = sum(
        1
        for root in _story_roots(package, roots).values()
        for node in root.iter()
        if node.tag in {QW("ins"), QW("del")} and _attribute(node, W, "author") == plan.revision_author
    )
    applied = plan.applied_count
    if applied == 0:
        detected = requested or "unchanged"
        if session_count:
            errors.append("candidate contains session revisions but the frozen plan has no APPLY actions")
    elif session_count == applied * 2:
        detected = "redlined"
    elif session_count == 0:
        detected = "clean"
    else:
        detected = "unknown"
        errors.append(
            f"session revision count {session_count} does not match the frozen plan ({applied * 2} redlined or 0 clean)"
        )
    if requested is not None and detected not in {"unchanged", requested}:
        errors.append(f"candidate kind {detected!r} does not match requested artifact kind {requested!r}")
    if requested is not None and requested not in {"redlined", "clean"}:
        errors.append(f"unsupported W5 artifact kind {requested!r} for occurrence mapping")
    return detected, errors


def _paragraph_equation_deltas(
    baseline: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
    plan: ApplicationPlan,
    baseline_roots: Mapping[str, ET.Element] | None = None,
    candidate_roots: Mapping[str, ET.Element] | None = None,
) -> tuple[dict[tuple[str, int], list[ET.Element]], list[str], dict[str, Any]]:
    """Return candidate equations beyond the unchanged baseline equations."""

    errors: list[str] = []
    extras: dict[tuple[str, int], list[ET.Element]] = {}
    details: dict[str, Any] = {}
    expected_by_location: dict[tuple[str, int], list[Any]] = collections.defaultdict(list)
    for action in plan.actions:
        if action.decision == "APPLY":
            expected_by_location[(action.package_part or "word/document.xml", action.paragraph or 0)].append(action)

    baseline_stories = _story_roots(baseline, baseline_roots)
    candidate_stories = _story_roots(candidate, candidate_roots)
    all_parts = sorted(set(baseline_stories) | set(candidate_stories))
    for part in all_parts:
        base_root = baseline_stories.get(part)
        candidate_root = candidate_stories.get(part)
        if base_root is None or candidate_root is None:
            continue
        base_blocks = _blocks(base_root)
        candidate_blocks = _blocks(candidate_root)
        if len(base_blocks) != len(candidate_blocks):
            errors.append(f"{part}: block count changed {len(base_blocks)} -> {len(candidate_blocks)}")
            continue
        for paragraph_number, (base_block, candidate_block) in enumerate(zip(base_blocks, candidate_blocks), 1):
            if base_block.tag != QW("p") or candidate_block.tag != QW("p"):
                continue
            base_equations = base_block.findall(".//m:oMath", NS)
            candidate_equations = candidate_block.findall(".//m:oMath", NS)
            remaining = list(candidate_equations)
            missing_baseline = 0
            for equation in base_equations:
                expected_hash = _omml_hash(equation)
                match = next((item for item in remaining if _omml_hash(item) == expected_hash), None)
                if match is None:
                    missing_baseline += 1
                else:
                    remaining.remove(match)
            if missing_baseline:
                errors.append(
                    f"{part} paragraph {paragraph_number}: {missing_baseline} pre-existing OMML equation(s) changed or disappeared"
                )
            actions = expected_by_location.get((part, paragraph_number), [])
            extras[(part, paragraph_number)] = remaining
            actual_hashes = collections.Counter(_omml_hash(item) for item in remaining)
            expected_hashes = collections.Counter(
                action.styled_template_sha256 for action in actions if action.styled_template_sha256 is not None
            )
            if actual_hashes != expected_hashes:
                errors.append(
                    f"{part} paragraph {paragraph_number}: generated OMML multiset does not match the frozen plan"
                )
            details[f"{part}:{paragraph_number}"] = {
                "baseline_count": len(base_equations),
                "candidate_count": len(candidate_equations),
                "generated_count": len(remaining),
                "expected_generated_count": len(actions),
            }
    return extras, errors, details


def _map_generated_equations(
    baseline: Mapping[str, bytes] | None,
    candidate: Mapping[str, bytes],
    plan: ApplicationPlan,
    kind: str,
    baseline_roots: Mapping[str, ET.Element] | None = None,
    candidate_roots: Mapping[str, ET.Element] | None = None,
) -> tuple[dict[str, ET.Element], set[int], list[str], dict[str, Any]]:
    mapping: dict[str, ET.Element] = {}
    errors: list[str] = []
    details: dict[str, Any] = {}
    generated_ids: set[int] = set()
    apply_actions = [action for action in plan.actions if action.decision == "APPLY"]
    if kind == "redlined":
        found, found_errors = _session_insertion_nodes(candidate, plan, candidate_roots)
        errors.extend(found_errors)
        for action in apply_actions:
            equation = found.get(action.occurrence_id)
            if equation is None:
                errors.append(f"{action.occurrence_id}: frozen session insertion is missing")
                continue
            if _omml_hash(equation) != action.styled_template_sha256:
                errors.append(f"{action.occurrence_id}: session OMML hash does not match the frozen plan")
                continue
            mapping[action.occurrence_id] = equation
            generated_ids.add(id(equation))
    elif kind == "clean":
        if baseline is not None:
            _extras, delta_errors, details = _paragraph_equation_deltas(
                baseline,
                candidate,
                plan,
                baseline_roots,
                candidate_roots,
            )
            errors.extend(delta_errors)
            # Hashes prove the generated multiset, but they cannot identify a
            # generated node when it is byte-identical to a pre-existing
            # equation in the same paragraph.  Bind clean nodes to the frozen
            # ordinary-run position first, then use the multiset only as an
            # independent count/content check.
            baseline_stories = _story_roots(baseline, baseline_roots)
            candidate_stories = _story_roots(candidate, candidate_roots)
            by_location: dict[tuple[str, int], list[Any]] = collections.defaultdict(list)
            for action in apply_actions:
                by_location[(action.package_part or "word/document.xml", action.paragraph or 0)].append(action)
            for key, actions in by_location.items():
                part, paragraph_number = key
                base_root = baseline_stories.get(part)
                candidate_root = candidate_stories.get(part)
                if base_root is None or candidate_root is None:
                    errors.append(f"{part} paragraph {paragraph_number}: clean candidate paragraph binding is missing")
                    continue
                base_blocks = _blocks(base_root)
                candidate_blocks = _blocks(candidate_root)
                if (
                    paragraph_number < 1
                    or paragraph_number > len(base_blocks)
                    or paragraph_number > len(candidate_blocks)
                    or base_blocks[paragraph_number - 1].tag != QW("p")
                    or candidate_blocks[paragraph_number - 1].tag != QW("p")
                ):
                    errors.append(f"{part} paragraph {paragraph_number}: clean candidate paragraph binding is missing")
                    continue
                base_paragraph = base_blocks[paragraph_number - 1]
                candidate_paragraph = candidate_blocks[paragraph_number - 1]
                base_parents = _parent_map(base_root)
                base_children = list(base_paragraph)
                placements: list[tuple[int, int, bool, Any]] = []
                for action in actions:
                    run_index = action.run_index
                    base_runs = base_paragraph.findall(".//w:r", NS)
                    if run_index is None or run_index < 1 or run_index > len(base_runs):
                        errors.append(f"{action.occurrence_id}: frozen clean mapping run is missing")
                        continue
                    base_run = base_runs[run_index - 1]
                    if base_parents.get(id(base_run)) is not base_paragraph:
                        errors.append(f"{action.occurrence_id}: frozen clean mapping run is not a direct paragraph child")
                        continue
                    source_nodes = [child for child in base_run if child.tag == QW("t")]
                    if len(source_nodes) != 1:
                        errors.append(f"{action.occurrence_id}: frozen clean mapping source run is not plain text")
                        continue
                    try:
                        base_index = base_children.index(base_run)
                    except ValueError:
                        errors.append(f"{action.occurrence_id}: frozen clean mapping run is detached")
                        continue
                    prefix = bool(action.run_start)
                    suffix = action.run_end is not None and action.run_end < len(source_nodes[0].text or "")
                    placements.append((base_index, int(prefix), bool(suffix), action))
                placements.sort(key=lambda item: (item[0], item[3].occurrence_id))
                used: set[int] = set()
                prior_delta = 0
                for base_index, prefix, suffix, action in placements:
                    candidate_index = base_index + prior_delta + prefix
                    candidate_children = list(candidate_paragraph)
                    equation = (
                        candidate_children[candidate_index]
                        if 0 <= candidate_index < len(candidate_children)
                        else None
                    )
                    if equation is None or equation.tag != QM("oMath"):
                        errors.append(f"{action.occurrence_id}: clean candidate has no mapped generated OMML")
                        prior_delta += prefix + int(suffix)
                        continue
                    if _omml_hash(equation) != action.styled_template_sha256:
                        errors.append(f"{action.occurrence_id}: clean candidate OMML does not match the frozen plan")
                        prior_delta += prefix + int(suffix)
                        continue
                    if id(equation) in used:
                        errors.append(f"{action.occurrence_id}: clean candidate OMML is mapped more than once")
                        prior_delta += prefix + int(suffix)
                        continue
                    used.add(id(equation))
                    mapping[action.occurrence_id] = equation
                    generated_ids.add(id(equation))
                    prior_delta += prefix + int(suffix)
        else:
            for action in apply_actions:
                location = _paragraph_for_action(candidate, action, candidate_roots)
                if location is None:
                    errors.append(f"{action.occurrence_id}: clean candidate paragraph binding is missing")
                    continue
                _part, _root, paragraph = location
                choices = [
                    item
                    for item in paragraph.findall(".//m:oMath", NS)
                    if _omml_hash(item) == action.styled_template_sha256 and id(item) not in generated_ids
                ]
                if not choices:
                    errors.append(f"{action.occurrence_id}: clean candidate has no mapped generated OMML")
                    continue
                mapping[action.occurrence_id] = choices[0]
                generated_ids.add(id(choices[0]))
    elif apply_actions:
        errors.append(f"cannot map generated OMML for unsupported candidate kind {kind!r}")
    if len(mapping) != len(apply_actions):
        errors.append("manifest/application-plan generated OMML accounting is incomplete")
    return mapping, generated_ids, errors, details


def _expected_canonical(row: Mapping[str, Any]) -> tuple[Any, str | None]:
    expected = row.get("canonical")
    semantic = row.get("semantic")
    if expected is None and isinstance(semantic, Mapping):
        expected = semantic.get("canonical")
    source = row.get("normalized_latex") or row.get("latex")
    if expected is None:
        if not isinstance(source, str) or not source.strip():
            raise CanonicalError("manifest occurrence has no approved semantic source")
        expected = canonicalize_formula(source, source_type=row.get("source_type"))
    return expected, source if isinstance(source, str) else None


def _style_value(properties: ET.Element | None, local: str) -> str | None:
    child = _direct_child(properties, QW(local))
    return _attribute(child, W, "val")


def _audit_generated_style(node: ET.Element, expected: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    runs = node.findall(".//m:r", NS)
    if not runs:
        return {"status": "FAIL", "runs": 0}, ["generated OMML contains no math runs"]
    expected_math_style = {"none": "p", "bold": "b", "italic": "i"}.get(expected.get("math_style"))
    run_results: list[dict[str, Any]] = []
    for index, run in enumerate(runs, 1):
        math_properties = _direct_child(run, QM("rPr"))
        legacy_control_properties = _direct_child(math_properties, QM("ctrlPr"))
        if legacy_control_properties is not None:
            errors.append(f"math run {index} uses non-native m:ctrlPr placement")
        word_properties = _direct_child(run, QW("rPr"))
        fonts = _direct_child(word_properties, QW("rFonts"))
        if fonts is None or fonts.get(QW("ascii")) != "Cambria Math" or fonts.get(QW("hAnsi")) != "Cambria Math":
            errors.append(f"math run {index} lacks explicit Cambria Math")
        actual = {
            "color": _style_value(word_properties, "color"),
            "size": _style_value(word_properties, "sz"),
            "highlight": _style_value(word_properties, "highlight"),
            "underline": _style_value(word_properties, "u"),
            "math_style": _attribute(_direct_child(math_properties, QM("sty")), M, "val"),
        }
        for field in ("color", "size", "highlight", "underline"):
            if field in expected and actual[field] != expected[field]:
                errors.append(f"math run {index} {field} {actual[field]!r} != expected {expected[field]!r}")
        if expected_math_style is not None and actual["math_style"] != expected_math_style:
            errors.append(
                f"math run {index} math_style {actual['math_style']!r} != expected {expected_math_style!r}"
            )
        run_results.append(actual)
    return {"status": "PASS" if not errors else "FAIL", "runs": run_results}, errors


def _audit_occurrences(
    manifest: Manifest,
    plan: ApplicationPlan,
    candidate: Mapping[str, bytes],
    baseline: Mapping[str, bytes] | None,
    kind: str,
    job: FrozenJob | None,
    baseline_roots: Mapping[str, ET.Element] | None = None,
    candidate_roots: Mapping[str, ET.Element] | None = None,
) -> tuple[list[dict[str, Any]], list[str], set[int], dict[str, Any], dict[str, ET.Element]]:
    errors: list[str] = []
    rows = {row["id"]: row for row in manifest.formulas}
    manifest_ids = tuple(row["id"] for row in manifest.formulas)
    plan_ids = tuple(action.occurrence_id for action in plan.actions)
    if manifest_ids != plan_ids:
        errors.append("application plan does not account for every manifest occurrence in order")
    actions = {action.occurrence_id: action for action in plan.actions}
    if set(actions) != set(rows):
        errors.append("application plan occurrence IDs do not match the manifest occurrence IDs")
    if job is not None:
        job_rows = {row["id"]: row for row in job.occurrences}
        if tuple(job.occurrence_ids) != manifest_ids:
            errors.append("frozen job occurrence IDs do not match the manifest")
    else:
        job_rows = {}
    mapping, generated_ids, mapping_errors, mapping_details = _map_generated_equations(
        baseline,
        candidate,
        plan,
        kind,
        baseline_roots,
        candidate_roots,
    )
    errors.extend(mapping_errors)
    baseline_native_nodes: dict[tuple[str, str], list[ET.Element]] = {}
    candidate_native_nodes: dict[tuple[str, str], list[ET.Element]] = {}
    if baseline is not None:
        baseline_native_nodes = _omml_nodes_by_location(baseline, roots=baseline_roots)
        candidate_native_nodes = _omml_nodes_by_location(
            candidate,
            ignored_ids=generated_ids,
            ignored_revision_author=plan.revision_author,
            roots=candidate_roots,
        )
    preserved_locations: set[tuple[str, str]] = set()
    native_preserve_count = 0
    accounting: list[dict[str, Any]] = []
    for row in manifest.formulas:
        occurrence_id = row["id"]
        action = actions.get(occurrence_id)
        if action is None:
            accounting.append({"id": occurrence_id, "terminal": False, "status": "UNACCOUNTED"})
            continue
        record: dict[str, Any] = {
            "id": occurrence_id,
            "decision": action.decision,
            "status": action.terminal_status,
            "terminal": action.terminal_status in {
                "APPLIED",
                "PRESERVED",
                "EXCLUDED",
                "NEEDS_REVIEW",
                "NEEDS_SPECIAL_HANDLER",
                "REFUSED",
                "FAILED",
            },
        }
        if action.decision == "APPLY":
            equation = mapping.get(occurrence_id)
            if equation is not None:
                record["omml_sha256"] = _omml_hash(equation)
                try:
                    expected, source_latex = _expected_canonical(row)
                    semantic = compare_omml_to_canonical(equation, expected, source_latex=source_latex)
                    record["semantic"] = semantic.to_dict()
                    if not semantic.passed:
                        errors.append(f"{occurrence_id}: semantic {semantic.status}: {semantic.reason}")
                except (CanonicalError, ContractError, TypeError, ValueError) as error:
                    record["semantic"] = {"status": "INVALID", "reason": str(error)}
                    errors.append(f"{occurrence_id}: semantic expectation is unavailable: {error}")
                style = action.style if isinstance(action.style, Mapping) else {}
                style_result, style_errors = _audit_generated_style(equation, style)
                record["style"] = style_result
                errors.extend(f"{occurrence_id}: {error}" for error in style_errors)
        elif action.decision == "PRESERVE":
            record["preserved"] = True
            is_native_omml = row.get("source_type") == "EXISTING_OMML" or action.reason == "existing_native_omml"
            if is_native_omml:
                native_preserve_count += 1
                part = action.package_part or row.get("package_part") or "word/document.xml"
                node_path = _inventory_node_path(row)
                location = (part, node_path) if node_path is not None else None
                if location is None:
                    errors.append(f"{occurrence_id}: preserved native OMML is missing its frozen inventory node_path")
                elif location in preserved_locations:
                    errors.append(f"{occurrence_id}: preserved native OMML location is mapped more than once")
                else:
                    preserved_locations.add(location)
                    baseline_nodes = baseline_native_nodes.get(location, [])
                    candidate_nodes = candidate_native_nodes.get(location, [])
                    if baseline is None:
                        errors.append(f"{occurrence_id}: preserved native OMML mapping requires a baseline source")
                    elif len(baseline_nodes) != 1:
                        errors.append(
                            f"{occurrence_id}: frozen native OMML location resolves to {len(baseline_nodes)} baseline nodes"
                        )
                    elif len(candidate_nodes) != 1:
                        errors.append(
                            f"{occurrence_id}: preserved native OMML location resolves to {len(candidate_nodes)} candidate nodes"
                        )
                    else:
                        baseline_node = baseline_nodes[0]
                        candidate_node = candidate_nodes[0]
                        record["omml"] = {
                            "part": part,
                            "location": node_path,
                            "sha256": _omml_hash(candidate_node),
                        }
                        if _canonical_xml(baseline_node) != _canonical_xml(candidate_node):
                            errors.append(f"{occurrence_id}: preserved native OMML content changed")
        if job_rows:
            job_status = job_rows.get(occurrence_id, {}).get("status")
            record["job_status"] = job_status
            if job_status != action.terminal_status:
                errors.append(
                    f"{occurrence_id}: frozen job status {job_status!r} does not match plan terminal status {action.terminal_status!r}"
                )
        if not record["terminal"]:
            errors.append(f"{occurrence_id}: application plan action is not terminally accounted")
        accounting.append(record)
    if baseline is not None:
        baseline_locations = set(baseline_native_nodes)
        candidate_locations = set(candidate_native_nodes)
        if preserved_locations != baseline_locations:
            missing = sorted(baseline_locations - preserved_locations)
            extra = sorted(preserved_locations - baseline_locations)
            errors.append(
                "manifest native OMML accounting does not cover the baseline one-to-one: "
                f"missing={missing}, extra={extra}"
            )
        if candidate_locations != baseline_locations:
            errors.append(
                "candidate pre-existing OMML locations do not match the baseline: "
                f"candidate={sorted(candidate_locations)}, baseline={sorted(baseline_locations)}"
            )
    elif native_preserve_count:
        errors.append("preserved native OMML accounting requires a baseline source")
    return accounting, errors, generated_ids, mapping_details, mapping


def _paragraph_reconstruction(
    baseline: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
    plan: ApplicationPlan,
    kind: str,
    mapping: Mapping[str, ET.Element],
    baseline_roots: Mapping[str, ET.Element] | None = None,
    candidate_roots: Mapping[str, ET.Element] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    apply_actions = [action for action in plan.actions if action.decision == "APPLY"]
    bound_actions = [action for action in plan.actions if action.package_part and action.paragraph is not None]
    if not bound_actions:
        return {"status": "PASS", "checked": 0, "paragraphs": []}, errors
    rejected_package: Mapping[str, bytes] | None = None
    if kind == "redlined" and apply_actions:
        try:
            rejected = reject_session_revisions(_serialize_package_for_audit(candidate), plan)
            rejected_package = read_package_bytes(rejected, "rejected source-reconstruction candidate")
        except (ApplicationError, AuditError, ContractError) as error:
            errors.append(f"source-visible reconstruction failed: {error}")
            return {"status": "FAIL", "checked": 0, "paragraphs": []}, errors
    elif kind not in {"clean", "redlined", "unchanged"}:
        errors.append(f"source-visible reconstruction cannot classify candidate kind {kind!r}")
        return {"status": "FAIL", "checked": 0, "paragraphs": []}, errors

    baseline_stories = _story_roots(baseline, baseline_roots)
    candidate_stories = _story_roots(candidate, candidate_roots)
    locations = sorted({(action.package_part or "word/document.xml", action.paragraph or 0) for action in bound_actions})
    records: list[dict[str, Any]] = []
    by_location: dict[tuple[str, int], list[Any]] = collections.defaultdict(list)
    for action in apply_actions:
        by_location[(action.package_part or "word/document.xml", action.paragraph or 0)].append(action)
    for part, paragraph_number in locations:
        base_root = baseline_stories.get(part)
        if base_root is None:
            errors.append(f"{part}: baseline source story is missing")
            continue
        base_blocks = _blocks(base_root)
        if paragraph_number < 1 or paragraph_number > len(base_blocks) or base_blocks[paragraph_number - 1].tag != QW("p"):
            errors.append(f"{part} paragraph {paragraph_number}: baseline paragraph binding is missing")
            continue
        expected_text = _visible_paragraph_text(base_blocks[paragraph_number - 1])
        if kind == "redlined" and rejected_package is not None:
            assert rejected_package is not None
            actual_root = _story_roots(rejected_package).get(part)
            if actual_root is None:
                errors.append(f"{part}: rejected source story is missing")
                continue
            actual_blocks = _blocks(actual_root)
            if paragraph_number > len(actual_blocks) or actual_blocks[paragraph_number - 1].tag != QW("p"):
                errors.append(f"{part} paragraph {paragraph_number}: rejected paragraph binding is missing")
                continue
            actual_text = _visible_paragraph_text(actual_blocks[paragraph_number - 1])
        else:
            actual_root = candidate_stories.get(part)
            if actual_root is None:
                errors.append(f"{part}: clean candidate story is missing")
                continue
            actual_blocks = _blocks(actual_root)
            if paragraph_number > len(actual_blocks) or actual_blocks[paragraph_number - 1].tag != QW("p"):
                errors.append(f"{part} paragraph {paragraph_number}: clean paragraph binding is missing")
                continue
            replacements = {
                id(mapping[action.occurrence_id]): action.source or ""
                for action in by_location[(part, paragraph_number)]
                if action.occurrence_id in mapping
            }
            actual_text = _visible_paragraph_text(actual_blocks[paragraph_number - 1], replacements)
        status = "PASS" if actual_text == expected_text else "FAIL"
        records.append(
            {
                "part": part,
                "paragraph": paragraph_number,
                "expected": expected_text,
                "actual": actual_text,
                "status": status,
            }
        )
        if status != "PASS":
            errors.append(f"{part} paragraph {paragraph_number}: source-visible text reconstruction differs from baseline")
    return {"status": "PASS" if not errors else "FAIL", "checked": len(records), "paragraphs": records}, errors


def _serialize_package_for_audit(package: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in package.items():
            archive.writestr(name, data)
    return output.getvalue()


def _coerce_manifest(value: Manifest | Mapping[str, Any] | str | Path) -> Manifest:
    if isinstance(value, Manifest):
        return load_manifest(value.to_dict())
    return load_manifest(value)


def _coerce_plan(value: ApplicationPlan | Mapping[str, Any] | str | Path) -> ApplicationPlan:
    if isinstance(value, ApplicationPlan):
        return load_application_plan(value.to_dict())
    return load_application_plan(value)


def _coerce_job(value: FrozenJob | Mapping[str, Any] | str | Path) -> FrozenJob:
    if isinstance(value, FrozenJob):
        return load_job(value.to_dict())
    return load_job(value)


def _select_artifact(job: FrozenJob, artifact_id: str | None) -> dict[str, Any]:
    if artifact_id is None:
        if len(job.artifacts) != 1:
            raise AuditError("--artifact-id is required when the frozen job has multiple artifacts")
        return dict(job.artifacts[0])
    matches = [
        dict(artifact)
        for artifact in job.artifacts
        if artifact["id"] == artifact_id or artifact["logical_id"] == artifact_id
    ]
    if len(matches) != 1:
        raise AuditError(f"artifact ID {artifact_id!r} does not identify exactly one frozen artifact")
    return matches[0]


def _audit_evidence_payload(
    *,
    source_sha256: str,
    manifest: Manifest,
    plan: ApplicationPlan,
    artifact: Mapping[str, Any],
    candidate_sha256: str,
    job: FrozenJob,
) -> dict[str, Any]:
    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "gate": "STRUCTURAL_AUDIT",
        "state": "PASS",
        "source_sha256": source_sha256,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "application_plan_sha256": plan.plan_sha256,
        "job_id": job.job_id,
        "artifact_id": artifact["id"],
        "artifact_logical_id": artifact["logical_id"],
        "artifact_kind": artifact["kind"],
        "artifact_type": artifact["kind"],
        "artifact_sha256": candidate_sha256,
        "tool": "scripts/audit_docx_formulas.py",
        "tool_version": "1",
    }
    payload["evidence_id"] = _audit_evidence_id(payload)
    return payload


def _audit_evidence_id(evidence: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in evidence.items() if key != "evidence_id"}
    return "audit-" + sha256(_canonical_json(payload).encode("utf-8"))[:32]


def validate_audit_evidence(
    evidence_or_report: Mapping[str, Any] | str | Path,
    candidate: bytes | str | Path,
    *,
    baseline: bytes | str | Path | None = None,
    manifest: Manifest | Mapping[str, Any] | str | Path | None = None,
    application_plan: ApplicationPlan | Mapping[str, Any] | str | Path | None = None,
    job: FrozenJob | Mapping[str, Any] | str | Path | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Reject stale, cross-artifact, or cross-job audit evidence."""

    if isinstance(evidence_or_report, (str, Path)):
        try:
            value = json.loads(Path(evidence_or_report).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AuditError(f"cannot load audit evidence: {error}") from error
    else:
        value = dict(evidence_or_report)
    if isinstance(value.get("evidence"), Mapping):
        report = value
        evidence = dict(value["evidence"])
        if report.get("status") != "PASS":
            raise AuditError("audit report is not a passing structural audit")
        if report.get("evidence_state") != "PASS":
            raise AuditError("audit report has no passing candidate-bound evidence state")
        audit_report = report.get("audit")
        if not isinstance(audit_report, Mapping) or audit_report.get("gate") != "STRUCTURAL_AUDIT":
            raise AuditError("audit report has no valid structural-audit gate metadata")
        if audit_report.get("state") != "PASS":
            raise AuditError("audit report structural-audit state is not PASS")
        if (
            report.get("delivery_status") == "COMPLETE"
            or report.get("status") == "COMPLETE"
            or audit_report.get("delivery_status") == "COMPLETE"
            or audit_report.get("status") == "COMPLETE"
        ):
            raise AuditError("audit evidence must not claim overall delivery completion")
    else:
        evidence = value
    if evidence.get("delivery_status") == "COMPLETE" or evidence.get("status") == "COMPLETE":
        raise AuditError("audit evidence must not claim overall delivery completion")
    if evidence.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise AuditError("unsupported audit evidence schema_version")
    if evidence.get("gate") != "STRUCTURAL_AUDIT" or evidence.get("state") != "PASS":
        raise AuditError("audit evidence is not a passing STRUCTURAL_AUDIT gate")
    for field in ("source_sha256", "manifest_sha256", "application_plan_sha256", "artifact_sha256"):
        if not isinstance(evidence.get(field), str) or not SHA256_RE.fullmatch(evidence[field]):
            raise AuditError(f"audit evidence has an invalid {field}")
    if not isinstance(evidence.get("evidence_id"), str) or not AUDIT_EVIDENCE_ID_RE.fullmatch(evidence["evidence_id"]):
        raise AuditError("audit evidence has an invalid evidence_id")
    if evidence["evidence_id"] != _audit_evidence_id(evidence):
        raise AuditError("audit evidence evidence_id does not match its contents")
    for field in (
        "manifest_id",
        "job_id",
        "artifact_id",
        "artifact_logical_id",
        "artifact_kind",
        "artifact_type",
        "tool",
        "tool_version",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise AuditError(f"audit evidence has an invalid {field}")
    if evidence["artifact_kind"] != evidence["artifact_type"]:
        raise AuditError("audit evidence artifact kind/type do not match")
    if not isinstance(evidence.get("application_plan_sha256"), str) or not SHA256_RE.fullmatch(
        evidence["application_plan_sha256"]
    ):
        raise AuditError("audit evidence has an invalid application_plan_sha256")
    if isinstance(candidate, bytes):
        candidate_bytes = candidate
    else:
        try:
            candidate_bytes = Path(candidate).read_bytes()
        except OSError as error:
            raise AuditError(f"cannot read candidate for evidence validation: {error}") from error
    actual_candidate_sha256 = sha256(candidate_bytes)
    if evidence["artifact_sha256"] != actual_candidate_sha256:
        raise AuditError("audit evidence artifact_sha256 does not match the candidate")

    manifest_obj = _coerce_manifest(manifest) if manifest is not None else None
    plan_obj = _coerce_plan(application_plan) if application_plan is not None else None
    job_obj = _coerce_job(job) if job is not None else None
    if (manifest_obj is None) != (plan_obj is None):
        raise AuditError("manifest and application_plan must be supplied together")
    if job_obj is not None and (manifest_obj is None or plan_obj is None):
        raise AuditError("frozen job validation requires both manifest and application_plan")
    if manifest_obj is not None:
        if evidence.get("manifest_id") != manifest_obj.manifest_id or evidence["manifest_sha256"] != manifest_obj.manifest_sha256:
            raise AuditError("audit evidence manifest identity does not match the supplied manifest")
        if manifest_obj.source_sha256 is not None and evidence["source_sha256"] != manifest_obj.source_sha256:
            raise AuditError("audit evidence source_sha256 does not match the supplied manifest")
    if plan_obj is not None and evidence["application_plan_sha256"] != plan_obj.plan_sha256:
        raise AuditError("audit evidence application-plan identity does not match the supplied plan")
    if plan_obj is not None:
        if plan_obj.source_sha256 != evidence["source_sha256"]:
            raise AuditError("audit evidence source_sha256 does not match the supplied application plan")
        if plan_obj.manifest_id != evidence.get("manifest_id") or plan_obj.manifest_sha256 != evidence["manifest_sha256"]:
            raise AuditError("audit evidence manifest identity does not match the supplied application plan")
    if baseline is not None:
        if isinstance(baseline, bytes):
            baseline_bytes = baseline
        else:
            try:
                baseline_bytes = Path(baseline).read_bytes()
            except OSError as error:
                raise AuditError(f"cannot read baseline for evidence validation: {error}") from error
        if evidence["source_sha256"] != sha256(baseline_bytes):
            raise AuditError("audit evidence source_sha256 does not match the supplied baseline")
    if job_obj is not None:
        if evidence.get("job_id") != job_obj.job_id:
            raise AuditError("audit evidence job identity does not match the supplied job")
        artifact = _select_artifact(job_obj, artifact_id or evidence.get("artifact_id"))
        if evidence.get("artifact_id") != artifact["id"]:
            raise AuditError("audit evidence artifact identity does not match the frozen job")
        if evidence.get("artifact_logical_id") != artifact["logical_id"]:
            raise AuditError("audit evidence logical artifact identity does not match the frozen job")
        if evidence.get("artifact_kind") != artifact["kind"] or evidence.get("artifact_type") != artifact["kind"]:
            raise AuditError("audit evidence artifact type does not match the frozen job")
        if artifact.get("content_sha256") != actual_candidate_sha256:
            raise AuditError("audit evidence candidate does not match the frozen artifact content hash")
        if job_obj.application_plan_sha256 != evidence["application_plan_sha256"]:
            raise AuditError("audit evidence plan identity does not match the frozen job")
        if manifest_obj is not None and plan_obj is not None:
            verify_frozen_job(job_obj, manifest_obj, evidence["source_sha256"])
    elif artifact_id is not None and evidence.get("artifact_id") != artifact_id:
        raise AuditError("audit evidence artifact identity does not match the requested artifact")
    return evidence


def audit_artifact(
    docx: Path,
    *,
    baseline: Path | None = None,
    expected_formulas: int | None = None,
    expected_author_insertions: int | None = None,
    expected_author_deletions: int | None = None,
    author: str | None = None,
    residual: Sequence[str] = (),
    allow_parts: Sequence[str] = (),
    require_cambria_math: bool = False,
    semantic_index: Path | None = None,
    manifest: Manifest | Mapping[str, Any] | str | Path | None = None,
    application_plan: ApplicationPlan | Mapping[str, Any] | str | Path | None = None,
    job: FrozenJob | Mapping[str, Any] | str | Path | None = None,
    artifact_id: str | None = None,
    artifact_kind: str | None = None,
) -> dict[str, Any]:
    """Audit one candidate and return a machine-readable report."""

    try:
        candidate_bytes = docx.read_bytes()
    except OSError as error:
        raise AuditError(f"cannot read candidate DOCX {docx}: {error}") from error
    package = read_package_bytes(candidate_bytes, f"candidate DOCX {docx}")
    report, document = inspect(docx, package, raw_bytes=candidate_bytes)
    candidate_roots = _story_roots(package)
    errors: list[str] = []
    report["audit"] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "gate": "STRUCTURAL_AUDIT",
        "state": "NOT_RUN",
        "delivery_status": "NOT_DERIVED",
    }

    if expected_formulas is not None and report["omath"] != expected_formulas:
        errors.append(f"formula count {report['omath']} != {expected_formulas}")
    if require_cambria_math and report["math_runs_without_explicit_cambria"]:
        errors.append(f"{report['math_runs_without_explicit_cambria']} math runs lack explicit Cambria Math")
    if author:
        inserted = report["insertions_by_author"].get(author, 0)
        deleted = report["deletions_by_author"].get(author, 0)
        if expected_author_insertions is not None and inserted != expected_author_insertions:
            errors.append(f"author insertion count {inserted} != {expected_author_insertions}")
        if expected_author_deletions is not None and deleted != expected_author_deletions:
            errors.append(f"author deletion count {deleted} != {expected_author_deletions}")

    current = current_text(document)
    residuals: dict[str, int] = {}
    for pattern in residual:
        matches = re.findall(pattern, current)
        if matches:
            residuals[pattern] = len(matches)
            errors.append(f"residual pattern {pattern!r}: {len(matches)} matches")
    report["residuals"] = residuals

    if semantic_index:
        semantic_results, semantic_errors = audit_semantic_index(document, semantic_index)
        report["semantic_validation"] = semantic_results
        errors.extend(semantic_errors)

    baseline_package: dict[str, bytes] | None = None
    baseline_roots: dict[str, ET.Element] | None = None
    baseline_report: dict[str, Any] | None = None
    source_sha256: str | None = None
    if baseline is not None:
        try:
            baseline_bytes = baseline.read_bytes()
        except OSError as error:
            raise AuditError(f"cannot read baseline DOCX {baseline}: {error}") from error
        baseline_package = read_package_bytes(baseline_bytes, f"baseline DOCX {baseline}")
        baseline_report, _baseline_document = inspect(baseline, baseline_package, raw_bytes=baseline_bytes)
        baseline_roots = _story_roots(baseline_package)
        source_sha256 = sha256(baseline_bytes)
        if any(value is not None for value in (manifest, application_plan, job, artifact_id, artifact_kind)):
            # Bound audits allow only story parts that may carry planned
            # occurrence edits, plus the applicator's revision-tracking
            # setting.  Historical unbound CLI audits retain their old
            # allowlist for compatibility.
            allowed = (
                set(allow_parts)
                | {"word/settings.xml"}
                | set(_story_roots(baseline_package))
                | set(_story_roots(package))
            )
        else:
            allowed = DEFAULT_ALLOWED | set(allow_parts)
        package_diff, package_errors = _package_diff(baseline_package, package, allowed)
        report["package_diff"] = package_diff
        errors.extend(package_errors)
        if report["media"] != baseline_report["media"]:
            errors.append("media names or hashes changed")
        if report["relationships"] != baseline_report["relationships"]:
            errors.append("relationship definitions changed")
        for field in ("drawings", "sections", "comments", "bookmarks", "fields", "hyperlinks", "content_controls"):
            if report[field] != baseline_report[field]:
                errors.append(f"{field} count changed: {baseline_report[field]} -> {report[field]}")
        report["baseline"] = {
            "path": str(baseline),
            "sha256": baseline_report["sha256"],
            "changed_protected_parts": [item["part"] for item in report["package_diff"]["protected_changes"]],
        }

    bound_requested = any(value is not None for value in (manifest, application_plan, job, artifact_id, artifact_kind))
    manifest_obj: Manifest | None = None
    plan_obj: ApplicationPlan | None = None
    job_obj: FrozenJob | None = None
    artifact: dict[str, Any] | None = None
    if bound_requested:
        if manifest is None or application_plan is None:
            errors.append("bound audit requires both --manifest and --application-plan")
        else:
            manifest_obj = _coerce_manifest(manifest)
            plan_obj = _coerce_plan(application_plan)
            if (
                plan_obj.manifest_id != manifest_obj.manifest_id
                or plan_obj.manifest_sha256 != manifest_obj.manifest_sha256
            ):
                errors.append("application plan manifest identity does not match the supplied manifest")
            if source_sha256 is None:
                errors.append("bound audit requires --baseline to prove source identity and reconstruction")
            else:
                if plan_obj.source_sha256 != source_sha256:
                    errors.append("application plan source_sha256 does not match the baseline source")
                if manifest_obj.source_sha256 is not None and manifest_obj.source_sha256 != source_sha256:
                    errors.append("manifest source_sha256 does not match the baseline source")
            if job is not None:
                job_obj = _coerce_job(job)
                if source_sha256 is not None:
                    try:
                        verify_frozen_job(job_obj, manifest_obj, source_sha256)
                    except ContractError as error:
                        errors.append(f"frozen job binding failed: {error}")
                if job_obj.application_plan_sha256 != plan_obj.plan_sha256:
                    errors.append("frozen job application_plan_sha256 does not match the supplied plan")
                try:
                    artifact = _select_artifact(job_obj, artifact_id)
                except AuditError as error:
                    errors.append(str(error))
                else:
                    if "STRUCTURAL_AUDIT" not in artifact.get("required_gates", []):
                        errors.append("selected frozen artifact does not require the STRUCTURAL_AUDIT gate")
            elif artifact_id is not None or artifact_kind is not None:
                errors.append("per-artifact audit evidence requires the frozen --job")

            requested_kind = artifact_kind or (artifact["kind"] if artifact else None)
            kind, kind_errors = _candidate_kind(package, plan_obj, requested_kind, candidate_roots)
            report["artifact"] = {
                "id": artifact["id"] if artifact else artifact_id,
                "logical_id": artifact["logical_id"] if artifact else None,
                "kind": artifact["kind"] if artifact else requested_kind,
                "detected_kind": kind,
            }
            errors.extend(kind_errors)
            if baseline_package is not None:
                settings_drift, settings_errors = _audit_settings_drift(baseline_package, package, plan_obj)
                report["settings_drift"] = settings_drift
                errors.extend(settings_errors)
            accounting, occurrence_errors, generated_ids, delta_details, mapping = _audit_occurrences(
                manifest_obj,
                plan_obj,
                package,
                baseline_package,
                kind,
                job_obj,
                baseline_roots,
                candidate_roots,
            )
            report["occurrence_accounting"] = accounting
            report["omml_delta"] = delta_details
            errors.extend(occurrence_errors)
            if baseline_package is not None:
                story_drift, story_errors = _audit_story_content(
                    baseline_package,
                    package,
                    plan_obj,
                    kind,
                    mapping=mapping,
                    baseline_roots=baseline_roots,
                    candidate_roots=candidate_roots,
                )
                report["story_content_drift"] = story_drift
                errors.extend(story_errors)
            if baseline_package is not None:
                expected_counts = _part_formula_counts(baseline_package, baseline_roots)
                for action in plan_obj.actions:
                    if action.decision == "APPLY":
                        part = action.package_part or "word/document.xml"
                        expected_counts[part] = expected_counts.get(part, 0) + 1
                actual_counts = _part_formula_counts(package, candidate_roots)
                report["expected_omath_by_part"] = expected_counts
                if actual_counts != expected_counts:
                    errors.append(f"formula counts by package part changed unexpectedly: {actual_counts} != {expected_counts}")

            if baseline_package is not None:
                baseline_omml = _omml_records(baseline_package, roots=baseline_roots)
                candidate_omml = _omml_records(
                    package,
                    ignored_ids=generated_ids,
                    ignored_revision_author=plan_obj.revision_author,
                    roots=candidate_roots,
                )
                omml_drift, omml_record_errors = _compare_records(baseline_omml, candidate_omml, "pre-existing OMML")
                report["omml_fingerprint_drift"] = omml_drift
                errors.extend(omml_record_errors)
                if any(omml_drift.values()):
                    errors.append("pre-existing OMML fingerprint changed")

            reconstruction, reconstruction_errors = _paragraph_reconstruction(
                baseline_package or {}, package, plan_obj, kind, mapping, baseline_roots, candidate_roots
            )
            report["source_reconstruction"] = reconstruction
            errors.extend(reconstruction_errors)

            if baseline_package is not None:
                baseline_revisions, baseline_revision_errors = _revision_records(
                    baseline_package, ignored_author=plan_obj.revision_author, roots=baseline_roots
                )
                candidate_revisions, candidate_revision_errors = _revision_records(
                    package,
                    ignored_author=plan_obj.revision_author,
                    ignored_ids=generated_ids,
                    roots=candidate_roots,
                )
                revision_drift, revision_record_errors = _compare_records(
                    baseline_revisions, candidate_revisions, "pre-existing revision"
                )
                report["revision_fingerprint_drift"] = revision_drift
                errors.extend(baseline_revision_errors + candidate_revision_errors + revision_record_errors)
                if any(revision_drift.values()):
                    errors.append("pre-existing revision fingerprint changed")
    elif baseline_package is not None and author:
        baseline_revisions, baseline_revision_errors = _revision_records(baseline_package, ignored_author=author)
        candidate_revisions, candidate_revision_errors = _revision_records(package, ignored_author=author)
        revision_drift, revision_record_errors = _compare_records(
            baseline_revisions, candidate_revisions, "pre-existing revision"
        )
        report["revision_fingerprint_drift"] = revision_drift
        errors.extend(baseline_revision_errors + candidate_revision_errors + revision_record_errors)
        if any(revision_drift.values()):
            errors.append("pre-existing revision fingerprint changed")

    try:
        if docx.read_bytes() != candidate_bytes:
            errors.append("candidate DOCX changed while the audit was running")
        if baseline is not None and baseline.read_bytes() != baseline_bytes:
            errors.append("baseline DOCX changed while the audit was running")
    except OSError as error:
        errors.append(f"cannot recheck DOCX immutability after audit: {error}")

    report["errors"] = errors
    passed = not errors
    report["status"] = "PASS" if passed else "FAIL"
    report["audit"]["state"] = "PASS" if passed else "FAIL"
    report["audit"]["delivery_status"] = "NOT_DERIVED"

    if (
        passed
        and manifest_obj is not None
        and plan_obj is not None
        and job_obj is not None
        and artifact is not None
        and source_sha256 is not None
        and "STRUCTURAL_AUDIT" in artifact.get("required_gates", [])
    ):
        actual_sha256 = sha256(candidate_bytes)
        if artifact.get("content_sha256") != actual_sha256:
            report["evidence_state"] = "NOT_EMITTED"
            report["errors"].append("frozen artifact content_sha256 does not match the audited candidate")
            report["status"] = "FAIL"
            report["audit"]["state"] = "FAIL"
        else:
            report["evidence"] = _audit_evidence_payload(
                source_sha256=source_sha256,
                manifest=manifest_obj,
                plan=plan_obj,
                artifact=artifact,
                candidate_sha256=actual_sha256,
                job=job_obj,
            )
            report["evidence_state"] = "PASS"
    else:
        report["evidence_state"] = "NOT_EMITTED"
    return report


# Short aliases make the script convenient for the downstream orchestrator
# without introducing another audit implementation.
audit = audit_artifact
run_audit = audit_artifact


def main() -> int:
    args = parse_args()
    report = audit_artifact(
        args.docx,
        baseline=args.baseline,
        expected_formulas=args.expected_formulas,
        expected_author_insertions=args.expected_author_insertions,
        expected_author_deletions=args.expected_author_deletions,
        author=args.author,
        residual=args.residual,
        allow_parts=args.allow_part,
        require_cambria_math=args.require_cambria_math,
        semantic_index=args.semantic_index,
        manifest=args.manifest,
        application_plan=args.application_plan,
        job=args.job,
        artifact_id=args.artifact_id,
        artifact_kind=args.artifact_kind,
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json:
        args.json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, zipfile.BadZipFile, ET.ParseError, re.error, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
