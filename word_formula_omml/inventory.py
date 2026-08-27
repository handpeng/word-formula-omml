"""Read-only DOCX inventory and conservative source classification.

The inventory stage records where formula-like material occurs.  It does not
recover mathematical meaning, approve candidates, or mutate an OOXML part.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from word_formula_omml.contract import (
    Confidence,
    ContractError,
    Manifest,
    OccurrenceStatus,
    SourceType,
    deterministic_occurrence_id,
    dump_manifest,
    load_manifest,
)
from word_formula_omml.style import snapshot_paragraph_style, snapshot_run_style


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
O = "urn:schemas-microsoft-com:office:office"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

NS = {"w": W, "r": R, "m": M, "o": O, "ct": CT}


class InventoryError(ContractError):
    """Raised when a source package cannot be inventoried safely."""


STORY_ROOTS = {
    "document": "main",
    "hdr": "header",
    "ftr": "footer",
    "footnotes": "footnote",
    "endnotes": "endnote",
    "comments": "comment",
}

PROTECTED_KEYS = (
    "table",
    "bookmark",
    "hyperlink",
    "field",
    "drawing",
    "content_control",
    "embedded_object",
    "comment_range",
)
NON_TEXT_BOUNDARIES = frozenset(
    {
        "br",
        "cr",
        "drawing",
        "endnoteReference",
        "fldChar",
        "footnoteReference",
        "noBreakHyphen",
        "object",
        "pict",
        "ptab",
        "softHyphen",
        "sym",
        "tab",
    }
)

_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")
_LOST_ESCAPE = re.compile(r"\b(?:frac|sqrt|mathcal|mathrm|text)\s*\{")
_SCRIPT = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*(?:_\{[^{}\n]+\}|_[A-Za-z0-9]+|\^\{[^{}\n]+\}|\^[A-Za-z0-9-]+)+"
)
_BRACED_SCRIPT = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*(?:_\{[^{}\n]+\}|\^\{[^{}\n]+\})+"
)
_OPERATOR_OPERAND = r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+|\^[A-Za-z0-9-]+)?"
_OPERATOR = re.compile(
    rf"\b{_OPERATOR_OPERAND}\s*(?:>=|<=|\+/-)\s*{_OPERATOR_OPERAND}"
    rf"(?:\s*\+/-\s*{_OPERATOR_OPERAND})?"
)
_UNICODE = re.compile(
    r"\b[\u0370-\u03ffA-Za-z][A-Za-z0-9]*\s*"
    r"(?:\s*[\u2264\u2265\u00b1\u2212]\s*[\u0370-\u03ffA-Za-z][A-Za-z0-9]*)+"
)
_CORRUPTED = re.compile(
    r"\b[A-Za-z0-9]+\s+[^\s]*[\u00c2\u00e2\u00c3][^\s]*\s+[A-Za-z0-9]+\b"
)
_INTERVAL = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\)\]]")
_UNKNOWN_OPERATOR = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*\s*[\u2200-\u22ff]\s*[A-Za-z][A-Za-z0-9]*\b"
)


def _qn(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else str(tag)


def _namespace(tag: str) -> str | None:
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def _is(element: ET.Element, namespace: str, local: str) -> bool:
    return element.tag == _qn(namespace, local)


def _attribute(element: ET.Element, namespace: str, local: str) -> str | None:
    return element.get(_qn(namespace, local))


def _relationship_ids(nodes: Iterable[ET.Element]) -> list[str]:
    return sorted(
        {
            value
            for node in nodes
            if (value := _attribute(node, R, "id")) is not None
        }
    )


@dataclass(frozen=True)
class NodeInfo:
    element: ET.Element
    ancestors: tuple[ET.Element, ...]
    path: str
    ordinal: int


@dataclass(frozen=True)
class CharRef:
    node: ET.Element
    run: ET.Element | None
    run_offset: int
    node_offset: int
    ancestors: tuple[ET.Element, ...]


@dataclass
class TextSurface:
    text: str
    refs: tuple[CharRef, ...]
    events: dict[int, tuple[int, int]]


@dataclass(frozen=True)
class TextMatch:
    start: int
    end: int
    source_type: str
    signal: str


@dataclass
class Candidate:
    block: ET.Element | None
    order: int
    row: dict


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    relationship_type: str
    target: str
    external: bool


def _index_tree(root: ET.Element) -> dict[int, NodeInfo]:
    result: dict[int, NodeInfo] = {}
    ordinal = 0

    def visit(node: ET.Element, ancestors: tuple[ET.Element, ...], path: str) -> None:
        nonlocal ordinal
        info = NodeInfo(node, ancestors + (node,), path, ordinal)
        result[id(node)] = info
        ordinal += 1
        sibling_counts: dict[str, int] = {}
        for child in list(node):
            child_local = _local(child.tag)
            sibling_counts[child_local] = sibling_counts.get(child_local, 0) + 1
            child_path = f"{path}/{child_local}[{sibling_counts[child_local]}]"
            visit(child, ancestors + (node,), child_path)

    root_local = _local(root.tag)
    visit(root, (), f"/{root_local}[1]")
    return result


def _nearest(info: NodeInfo, candidates: set[tuple[str, str]]) -> ET.Element | None:
    for node in reversed(info.ancestors):
        if (_namespace(node.tag), _local(node.tag)) in candidates:
            return node
    return None


def _build_surface(paragraph: ET.Element, *, deleted: bool, index: dict[int, NodeInfo]) -> TextSurface:
    chars: list[str] = []
    refs: list[CharRef] = []
    events: dict[int, tuple[int, int]] = {}
    run_offsets: dict[int, int] = {}

    def visit(node: ET.Element, ancestors: tuple[ET.Element, ...], in_deletion: bool) -> None:
        start = len(chars)
        is_deletion = in_deletion or _is(node, W, "del")
        if _is(node, W, "del") and not deleted:
            events[id(node)] = (start, start)
            return
        if _is(node, W, "ins") and deleted:
            events[id(node)] = (start, start)
            return
        if _is(node, W, "t") and not deleted and not is_deletion:
            value = node.text or ""
            run = _nearest(index[id(node)], {(W, "r")})
            run_offset = run_offsets.get(id(run), 0) if run is not None else 0
            for offset, character in enumerate(value):
                chars.append(character)
                refs.append(CharRef(node, run, run_offset + offset, offset, ancestors + (node,)))
            if run is not None:
                run_offsets[id(run)] = run_offset + len(value)
        elif _is(node, W, "delText") and deleted and is_deletion:
            value = node.text or ""
            run = _nearest(index[id(node)], {(W, "r")})
            run_offset = run_offsets.get(id(run), 0) if run is not None else 0
            for offset, character in enumerate(value):
                chars.append(character)
                refs.append(CharRef(node, run, run_offset + offset, offset, ancestors + (node,)))
            if run is not None:
                run_offsets[id(run)] = run_offset + len(value)
        else:
            for child in list(node):
                visit(child, ancestors + (node,), is_deletion)
        events[id(node)] = (start, len(chars))

    initial_ancestors = index[id(paragraph)].ancestors[:-1]
    visit(paragraph, initial_ancestors, False)
    return TextSurface("".join(chars), tuple(refs), events)


def _braced_end(text: str, start: int) -> int:
    depth = 0
    seen_brace = False
    end = start
    index = start
    while index < len(text):
        character = text[index]
        if character == "{":
            depth += 1
            seen_brace = True
        elif character == "}":
            depth -= 1
            if depth < 0:
                return end
            if depth == 0 and seen_brace:
                end = index + 1
                lookahead = index + 1
                while lookahead < len(text) and text[lookahead].isspace():
                    lookahead += 1
                if lookahead >= len(text) or text[lookahead] != "{":
                    return end
        elif character in "\n;":
            return end or index
        index += 1
    return len(text) if depth else (end or index)


def _latex_end(text: str, start: int, command_end: int) -> int:
    if "{" in text[command_end:]:
        return _braced_end(text, start)
    end = command_end
    while end < len(text):
        whitespace_end = end
        while whitespace_end < len(text) and text[whitespace_end].isspace():
            whitespace_end += 1
        if whitespace_end >= len(text):
            return whitespace_end
        if text[whitespace_end] in "+-=<>(),[]{}":
            end = whitespace_end + 1
            continue
        command = _LATEX_COMMAND.match(text, whitespace_end)
        if command:
            end = command.end()
            continue
        return end
    return end


def _balanced_braces(text: str) -> bool:
    depth = 0
    for character in text:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _text_matches(text: str) -> list[TextMatch]:
    matches: list[TextMatch] = []
    for match in _LATEX_COMMAND.finditer(text):
        end = _latex_end(text, match.start(), match.end())
        source_type = SourceType.RAW_LATEX.value if _balanced_braces(text[match.start() : end]) else SourceType.PARTIAL_LATEX.value
        signal = "latex_command" if source_type == SourceType.RAW_LATEX.value else "unbalanced_latex"
        matches.append(TextMatch(match.start(), end, source_type, signal))
    for match in _LOST_ESCAPE.finditer(text):
        matches.append(TextMatch(match.start(), _braced_end(text, match.start()), SourceType.PARTIAL_LATEX.value, "lost_escape"))
    for regex, source_type, signal in (
        (_CORRUPTED, SourceType.CORRUPTED_TEXT.value, "mojibake"),
        (_UNICODE, SourceType.UNICODE_MATH.value, "unicode_operator"),
        (_OPERATOR, SourceType.PLAIN_MATH.value, "plain_operator"),
        (_INTERVAL, SourceType.PLAIN_MATH.value, "interval"),
        (_BRACED_SCRIPT, SourceType.RAW_LATEX.value, "grouped_script"),
        (_SCRIPT, SourceType.PLAIN_MATH.value, "script_marker"),
        (_UNKNOWN_OPERATOR, SourceType.UNKNOWN_FORMULA.value, "unknown_operator"),
    ):
        matches.extend(TextMatch(item.start(), item.end(), source_type, signal) for item in regex.finditer(text))

    priority = {
        SourceType.RAW_LATEX.value: 0,
        SourceType.PARTIAL_LATEX.value: 1,
        SourceType.CORRUPTED_TEXT.value: 2,
        SourceType.UNICODE_MATH.value: 3,
        SourceType.PLAIN_MATH.value: 4,
        SourceType.UNKNOWN_FORMULA.value: 5,
    }
    selected: list[TextMatch] = []
    for candidate in sorted(matches, key=lambda item: (-(item.end - item.start), priority[item.source_type], item.start)):
        if candidate.end <= candidate.start:
            continue
        if any(candidate.start < item.end and item.start < candidate.end for item in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: item.start)


def classify_formula_text(text: str) -> str | None:
    """Classify one text surface without asserting mathematical correctness."""

    if not isinstance(text, str):
        raise InventoryError("formula text must be a string")
    matches = _text_matches(text)
    return matches[0].source_type if matches else None


def _is_eq_field(node: ET.Element) -> bool:
    instruction = _attribute(node, W, "instr") or ""
    return _is(node, W, "fldSimple") and bool(re.search(r"(?:^|\s)EQ(?:\s|$)", instruction, re.IGNORECASE))


def _match_is_eq_field_result(match: TextMatch, surface: TextSurface) -> bool:
    return any(_is_eq_field(node) for ref in surface.refs[match.start : match.end] for node in ref.ancestors)


def _root_story(root: ET.Element) -> tuple[str, bool]:
    if _namespace(root.tag) != W:
        return "unknown", True
    local = _local(root.tag)
    if local in STORY_ROOTS:
        return STORY_ROOTS[local], False
    return "unknown", True


def _relationship_owner(relationship_part: str) -> str:
    if relationship_part == "_rels/.rels":
        return ""
    marker = "/_rels/"
    if marker not in relationship_part or not relationship_part.endswith(".rels"):
        raise InventoryError(f"invalid relationship part name: {relationship_part}")
    return relationship_part.replace(marker, "/")[:-5]


def _load_package(
    path: Path,
) -> tuple[bytes, dict[str, bytes], dict[str, ET.Element], dict[str, dict[str, Relationship]]]:
    try:
        source_bytes = path.read_bytes()
    except OSError as error:
        raise InventoryError(f"cannot read source DOCX {path}: {error}") from error
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise InventoryError("DOCX contains duplicate package part names")
            for name in names:
                path_parts = name.split("/")
                if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in path_parts):
                    raise InventoryError(f"unsafe package part name: {name!r}")
            bad_name = archive.testzip()
            if bad_name is not None:
                raise InventoryError(f"DOCX package CRC check failed for {bad_name}")
            parts = {name: archive.read(name) for name in names}
    except InventoryError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise InventoryError(f"source is not a readable DOCX ZIP package: {error}") from error

    roots: dict[str, ET.Element] = {}
    for name, data in parts.items():
        if name.endswith(".xml") or name.endswith(".rels"):
            try:
                roots[name] = ET.fromstring(data)
            except (ET.ParseError, ValueError) as error:
                raise InventoryError(f"cannot parse XML package part {name}: {error}") from error

    relationships: dict[str, dict[str, Relationship]] = {}
    for name, root in roots.items():
        if not name.endswith(".rels"):
            continue
        owner = _relationship_owner(name)
        entries: dict[str, Relationship] = {}
        for node in root:
            if _namespace(node.tag) != PKG or _local(node.tag) != "Relationship":
                continue
            relationship_id = node.get("Id")
            target = node.get("Target")
            relationship_type = node.get("Type")
            if not relationship_id or not target or not relationship_type:
                raise InventoryError(f"incomplete relationship in {name}")
            if relationship_id in entries:
                raise InventoryError(f"duplicate relationship id {relationship_id!r} in {name}")
            external = node.get("TargetMode") == "External"
            entries[relationship_id] = Relationship(relationship_id, relationship_type, target, external)
            if not external:
                target_part = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
                if target_part not in parts:
                    raise InventoryError(f"relationship {name}:{relationship_id} targets missing part {target_part}")
        relationships[owner] = entries

    content_types = roots.get("[Content_Types].xml")
    if content_types is None:
        raise InventoryError("DOCX is missing [Content_Types].xml")
    for override in content_types.findall("ct:Override", NS):
        part_name = (override.get("PartName") or "").lstrip("/")
        if part_name not in parts:
            raise InventoryError(f"content type override targets missing part {part_name}")
    try:
        current_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise InventoryError(f"cannot reread source DOCX {path}: {error}") from error
    if source_sha256 != current_sha256:
        raise InventoryError("source DOCX changed while package was being read")
    return source_bytes, parts, roots, relationships


def _block_map(index: dict[int, NodeInfo]) -> dict[int, int]:
    blocks = []
    for info in index.values():
        kind = (_namespace(info.element.tag), _local(info.element.tag))
        if kind == (W, "p"):
            blocks.append(info)
        elif kind == (M, "oMathPara") and not any(
            _is(ancestor, W, "p") for ancestor in info.ancestors
        ):
            blocks.append(info)
    blocks.sort(key=lambda item: item.ordinal)
    return {id(info.element): number for number, info in enumerate(blocks, 1)}


def _paragraph_style(paragraph: ET.Element) -> str | None:
    properties = paragraph.find("w:pPr", NS)
    if properties is None:
        return None
    style = properties.find("w:pStyle", NS)
    return _attribute(style, W, "val") if style is not None else None


def _layout(paragraph: ET.Element | None, *, display_node: bool = False) -> str:
    if display_node:
        return "display"
    style = _paragraph_style(paragraph) if paragraph is not None and _is(paragraph, W, "p") else None
    return "display" if style and "display" in style.lower() else "inline"


def _surface_span(surface: TextSurface, element: ET.Element) -> tuple[int, int] | None:
    return surface.events.get(id(element))


def _run_indices(paragraph: ET.Element) -> dict[int, int]:
    return {id(run): number for number, run in enumerate(paragraph.findall(".//w:r", NS), 1)}


def _revision_ancestry_from_nodes(
    nodes: Iterable[ET.Element], *, omitted: bool = False
) -> list[dict]:
    revisions: dict[tuple[str, str, str], dict] = {}
    for node in nodes:
        if _namespace(node.tag) != W or _local(node.tag) not in {"ins", "del"}:
            continue
        kind = _local(node.tag)
        revision_id = _attribute(node, W, "id") or ""
        author = _attribute(node, W, "author") or ""
        key = (kind, revision_id, author)
        item = revisions.setdefault(
            key,
            {
                "kind": kind,
                "id": revision_id,
                "author": author,
                "deleted": kind == "del",
            },
        )
        if omitted:
            item["omitted"] = True
    return sorted(revisions.values(), key=lambda item: (item["kind"], item["id"], item["author"]))


def _omitted_revision_nodes(
    surface: TextSurface,
    index: dict[int, NodeInfo],
    start: int,
    end: int,
    *,
    deleted: bool,
) -> tuple[ET.Element, ...]:
    omitted_kind = "ins" if deleted else "del"
    result = []
    for node_id, span in surface.events.items():
        node = index[node_id].element
        if _namespace(node.tag) == W and _local(node.tag) == omitted_kind:
            position = span[0]
            if start < position < end:
                result.append(node)
    return tuple(result)


def _non_text_boundary_nodes(
    surface: TextSurface,
    index: dict[int, NodeInfo],
    start: int,
    end: int,
) -> tuple[ET.Element, ...]:
    result = []
    for node_id, span in surface.events.items():
        node = index[node_id].element
        if _namespace(node.tag) == W and _local(node.tag) in NON_TEXT_BOUNDARIES:
            position = span[0]
            if start < position < end:
                result.append(node)
    return tuple(result)


def _revision_metadata(
    refs: Iterable[CharRef],
    *,
    extra_nodes: Iterable[ET.Element] = (),
    omitted_nodes: Iterable[ET.Element] = (),
) -> tuple[bool, list[dict]]:
    regular_nodes = [node for ref in refs for node in ref.ancestors]
    revisions = _revision_ancestry_from_nodes(regular_nodes)
    omitted_revisions = _revision_ancestry_from_nodes(omitted_nodes, omitted=True)
    for item in omitted_revisions:
        key = (item["kind"], item["id"], item["author"])
        existing = next((value for value in revisions if (value["kind"], value["id"], value["author"]) == key), None)
        if existing is None:
            revisions.append(item)
        else:
            existing["omitted"] = True
    revisions.extend(
        item
        for item in _revision_ancestry_from_nodes(extra_nodes)
        if (item["kind"], item["id"], item["author"])
        not in {(value["kind"], value["id"], value["author"]) for value in revisions}
    )
    revisions.sort(key=lambda item: (item["kind"], item["id"], item["author"]))
    return bool(revisions), revisions


def _protected_from_ancestors(ancestors: Iterable[ET.Element]) -> dict[str, bool]:
    unique = {id(node): node for node in ancestors}
    return {
        "table": any(_is(node, W, "tbl") for node in unique.values()),
        "bookmark": False,
        "comment_range": False,
        "hyperlink": any(_is(node, W, "hyperlink") for node in unique.values()),
        "field": any(_is(node, W, "fldSimple") or _is(node, W, "fldChar") for node in unique.values()),
        "drawing": False,
        "content_control": any(_is(node, W, "sdt") for node in unique.values()),
        "embedded_object": any(_is(node, W, "object") for node in unique.values()),
    }


def _bookmark_intersection(paragraph: ET.Element, surface: TextSurface, start: int, end: int) -> bool:
    starts: dict[str, int] = {}
    for node in paragraph.iter():
        if _is(node, W, "bookmarkStart"):
            marker_id = _attribute(node, W, "id")
            position = surface.events.get(id(node), (None, None))[0]
            if marker_id is not None and position is not None:
                starts[marker_id] = position
        elif _is(node, W, "bookmarkEnd"):
            marker_id = _attribute(node, W, "id")
            position = surface.events.get(id(node), (None, None))[0]
            if marker_id in starts and position is not None:
                if starts[marker_id] <= end and start <= position:
                    return True
    return False


def _comment_range_intersection(paragraph: ET.Element, surface: TextSurface, start: int, end: int) -> bool:
    starts: dict[str, int] = {}
    for node in paragraph.iter():
        if _is(node, W, "commentRangeStart"):
            marker_id = _attribute(node, W, "id")
            position = surface.events.get(id(node), (None, None))[0]
            if marker_id is not None and position is not None:
                starts[marker_id] = position
        elif _is(node, W, "commentRangeEnd"):
            marker_id = _attribute(node, W, "id")
            position = surface.events.get(id(node), (None, None))[0]
            if marker_id in starts and position is not None:
                if starts[marker_id] <= end and start <= position:
                    return True
    return False


def _drawing_adjacency(paragraph: ET.Element, surface: TextSurface, start: int, end: int) -> bool:
    for node in paragraph.iter():
        if _is(node, W, "drawing"):
            position = surface.events.get(id(node), (None, None))[0]
            if position is not None and min(abs(position - start), abs(position - end)) <= 1:
                return True
    return False


def _protected_context(
    refs: tuple[CharRef, ...],
    paragraph: ET.Element,
    surface: TextSurface,
    start: int,
    end: int,
) -> tuple[dict[str, bool], dict[str, bool]]:
    ancestors = tuple(node for ref in refs for node in ref.ancestors)
    protected = _protected_from_ancestors(ancestors)
    protected["bookmark"] = _bookmark_intersection(paragraph, surface, start, end)
    protected["comment_range"] = _comment_range_intersection(paragraph, surface, start, end)
    protected["drawing"] = _drawing_adjacency(paragraph, surface, start, end)
    adjacent = {
        "adjacent_bookmark": protected["bookmark"],
        "adjacent_field": protected["field"],
        "adjacent_hyperlink": protected["hyperlink"],
        "adjacent_drawing": protected["drawing"],
    }
    return protected, adjacent


def _run_boundaries(refs: tuple[CharRef, ...], run_indices: dict[int, int]) -> tuple[dict, int | None, int | None, int | None]:
    runs: list[dict] = []
    seen: set[int] = set()
    for ref in refs:
        if ref.run is None or id(ref.run) in seen:
            continue
        seen.add(id(ref.run))
        run_refs = [item for item in refs if item.run is ref.run]
        runs.append(
            {
                "index": run_indices.get(id(ref.run)),
                "start": min(item.run_offset for item in run_refs),
                "end": max(item.run_offset for item in run_refs) + 1,
            }
        )
    if not runs:
        return {"run_count": 0, "runs": []}, None, None, None
    return (
        {"run_count": len(runs), "runs": runs},
        runs[0]["index"],
        runs[0]["start"],
        runs[-1]["end"],
    )


def _status_for(
    source_type: str,
    *,
    deleted: bool,
    inside_revision: bool,
    protected: dict[str, bool],
    run_count: int,
    structural_boundary: bool,
) -> tuple[str, str]:
    if source_type == SourceType.EXISTING_OMML.value:
        return OccurrenceStatus.PRESERVED.value, "native_omml_preserve"
    if source_type in {SourceType.PARTIAL_LATEX.value, SourceType.CORRUPTED_TEXT.value}:
        return OccurrenceStatus.NEEDS_REVIEW.value, "classifier_requires_review"
    if (
        deleted
        or inside_revision
        or run_count > 1
        or structural_boundary
        or any(protected.values())
        or source_type
        in {
            SourceType.EQ_FIELD.value,
            SourceType.EMBEDDED_EQUATION_OBJECT.value,
            SourceType.UNKNOWN_FORMULA.value,
        }
    ):
        return OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value, "structural_handler_required"
    return OccurrenceStatus.DISCOVERED.value, "candidate_detection_only"


def _text_candidate(
    *,
    match: TextMatch,
    surface: TextSurface,
    paragraph: ET.Element,
    index: dict[int, NodeInfo],
    block_map: dict[int, int],
    story: str,
    package_part: str,
    deleted: bool,
    order: int,
) -> Candidate:
    refs = surface.refs[match.start : match.end]
    if not refs:
        raise InventoryError("formula match has no source character mapping")
    protected, adjacent = _protected_context(refs, paragraph, surface, match.start, match.end)
    omitted_nodes = _omitted_revision_nodes(
        surface,
        index,
        match.start,
        match.end,
        deleted=deleted,
    )
    non_text_boundaries = _non_text_boundary_nodes(surface, index, match.start, match.end)
    inside_revision, ancestry = _revision_metadata(refs, omitted_nodes=omitted_nodes)
    boundaries, run_index, run_start, run_end = _run_boundaries(refs, _run_indices(paragraph))
    relationship_ids = _relationship_ids(ref for ref in refs for ref in ref.ancestors)
    style_values = [snapshot_run_style(ref.run) for ref in refs if ref.run is not None]
    paragraph_snapshot = snapshot_paragraph_style(paragraph)
    paragraph_context = {
        key: value for key, value in paragraph_snapshot.items() if key != "paragraph_style"
    }
    distinct_styles = []
    for style in style_values:
        if style not in distinct_styles:
            distinct_styles.append(style)
    style_snapshot: dict
    if len(distinct_styles) <= 1:
        style_snapshot = distinct_styles[0] if distinct_styles else {}
    else:
        style_snapshot = {"conflict": True, "runs": distinct_styles}
    if paragraph_context:
        style_snapshot = dict(style_snapshot)
        style_snapshot["paragraph"] = paragraph_context
    status, reason = _status_for(
        match.source_type,
        deleted=deleted,
        inside_revision=inside_revision,
        protected=protected,
        run_count=boundaries["run_count"],
        structural_boundary=bool(non_text_boundaries),
    )
    anchor_before = surface.text[max(0, match.start - 32) : match.start]
    anchor_after = surface.text[match.end : match.end + 32]
    row = {
        "latex": surface.text[match.start : match.end],
        "layout": _layout(paragraph),
        "paragraph": block_map[id(paragraph)],
        "sequence_in_paragraph": 0,
        "source": surface.text[match.start : match.end],
        "raw_source": surface.text[match.start : match.end],
        "anchor_before": anchor_before if anchor_before.strip() else None,
        "anchor_after": anchor_after if anchor_after.strip() else None,
        "run_index": run_index,
        "run_start": run_start,
        "run_end": run_end,
        "paragraph_style": _paragraph_style(paragraph),
        "inside_existing_revision": inside_revision,
        **adjacent,
        "package_part": package_part,
        "story": story,
        "source_type": match.source_type,
        "confidence": Confidence.REVIEW_REQUIRED.value,
        "revision_ancestry": ancestry,
        "protected_containers": protected,
        "run_boundaries": boundaries,
        "style_snapshot": style_snapshot,
        "expected_matches": 1,
        "status": status,
        "extensions": {
            "inventory": {
                "source_view": "deleted" if deleted else "current",
                "detection_signal": match.signal,
                "status_reason": reason,
                "source_span": {"start": match.start, "end": match.end},
                "node_paths": sorted({index[id(ref.node)].path for ref in refs}),
                "run_paths": sorted({index[id(ref.run)].path for ref in refs if ref.run is not None}),
                "crosses_omitted_revision": bool(omitted_nodes),
                "crosses_non_text_boundary": bool(non_text_boundaries),
                "boundary_node_paths": sorted({index[id(node)].path for node in non_text_boundaries}),
                "relationship_ids": relationship_ids,
            }
        },
    }
    row = {key: value for key, value in row.items() if value is not None}
    return Candidate(paragraph, order, row)


def _omml_candidate(
    node: ET.Element,
    *,
    index: dict[int, NodeInfo],
    block_map: dict[int, int],
    story: str,
    package_part: str,
    order: int,
) -> Candidate:
    info = index[id(node)]
    paragraph = _nearest(info, {(W, "p")})
    # Word stores display OMML inside a paragraph. Use that paragraph as the
    # block when present; retain support for legacy body-level oMathPara.
    block = paragraph or _nearest(info, {(M, "oMathPara")})
    source = "".join(item.text or "" for item in node.iter() if _is(item, M, "t")) or "native-omml"
    layout = _layout(paragraph, display_node=any(_is(item, M, "oMathPara") for item in info.ancestors))
    block_number = block_map.get(id(block), 0) if block is not None else 0
    protected = _protected_from_ancestors(info.ancestors)
    inside_revision, ancestry = _revision_metadata((), extra_nodes=info.ancestors)
    relationship_ids = _relationship_ids(info.ancestors)
    adjacent = {
        "adjacent_bookmark": protected["bookmark"],
        "adjacent_field": protected["field"],
        "adjacent_hyperlink": protected["hyperlink"],
        "adjacent_drawing": protected["drawing"],
    }
    row = {
        "latex": source,
        "layout": layout,
        "paragraph": block_number,
        "sequence_in_paragraph": 0,
        "source": source,
        "raw_source": source,
        "package_part": package_part,
        "story": story,
        "source_type": SourceType.EXISTING_OMML.value,
        "confidence": Confidence.REVIEW_REQUIRED.value,
        "expected_matches": 1,
        "status": OccurrenceStatus.PRESERVED.value,
        "protected_containers": protected,
        "inside_existing_revision": inside_revision,
        "revision_ancestry": ancestry,
        **adjacent,
        "extensions": {
            "inventory": {
                "source_view": "omml",
                "detection_signal": "native_omml",
                "node_path": index[id(node)].path,
                "unvalidated_latex": True,
                "relationship_ids": relationship_ids,
            }
        },
    }
    return Candidate(block, order, row)


def _field_candidate(
    field: ET.Element,
    *,
    surface: TextSurface,
    paragraph: ET.Element,
    index: dict[int, NodeInfo],
    block_map: dict[int, int],
    story: str,
    package_part: str,
    order: int,
) -> Candidate | None:
    instruction = _attribute(field, W, "instr") or ""
    if not _is_eq_field(field):
        return None
    span = _surface_span(surface, field)
    if span is None or span[0] == span[1]:
        source = instruction.strip() or "eq-field"
        start = end = 0
        refs: tuple[CharRef, ...] = ()
    else:
        start, end = span
        source = surface.text[start:end]
        refs = surface.refs[start:end]
    protected = _protected_from_ancestors(index[id(field)].ancestors)
    if refs:
        contextual, _adjacent = _protected_context(refs, paragraph, surface, start, end)
        for key, value in contextual.items():
            protected[key] = protected[key] or value
    adjacent = {
        "adjacent_bookmark": protected["bookmark"],
        "adjacent_field": protected["field"],
        "adjacent_hyperlink": protected["hyperlink"],
        "adjacent_drawing": protected["drawing"],
    }
    inside_revision, ancestry = _revision_metadata(
        refs,
        extra_nodes=index[id(field)].ancestors,
    )
    relationship_ids = _relationship_ids(
        node for ref in refs for node in ref.ancestors
    )
    anchor_before = surface.text[max(0, start - 32) : start]
    anchor_after = surface.text[end : end + 32]
    boundaries, run_index, run_start, run_end = _run_boundaries(refs, _run_indices(paragraph))
    row = {
        "latex": source,
        "layout": _layout(paragraph),
        "paragraph": block_map[id(paragraph)],
        "sequence_in_paragraph": 0,
        "source": source,
        "raw_source": source,
        "anchor_before": anchor_before if anchor_before.strip() else None,
        "anchor_after": anchor_after if anchor_after.strip() else None,
        "package_part": package_part,
        "story": story,
        "source_type": SourceType.EQ_FIELD.value,
        "confidence": Confidence.REVIEW_REQUIRED.value,
        "expected_matches": 1,
        "status": OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        "inside_existing_revision": inside_revision,
        "revision_ancestry": ancestry,
        **adjacent,
        "protected_containers": protected,
        "run_boundaries": boundaries,
        "run_index": run_index,
        "run_start": run_start,
        "run_end": run_end,
        "extensions": {
            "inventory": {
                "source_view": "current",
                "detection_signal": "eq_field_instruction",
                "field_instruction": instruction,
                "node_path": index[id(field)].path,
                "source_span": {"start": start, "end": end},
                "relationship_ids": relationship_ids,
            }
        },
    }
    return Candidate(paragraph, order, {key: value for key, value in row.items() if value is not None})


def _object_candidate(
    object_node: ET.Element,
    *,
    paragraph: ET.Element,
    surface: TextSurface,
    index: dict[int, NodeInfo],
    block_map: dict[int, int],
    story: str,
    package_part: str,
    order: int,
) -> Candidate:
    source = "".join(surface.text).strip() or "embedded-equation-object"
    span = _surface_span(surface, object_node)
    position = span[0] if span is not None else len(surface.text)
    anchor_before = surface.text[max(0, position - 32) : position]
    anchor_after = surface.text[position : position + 32]
    relationship_ids = _relationship_ids(object_node.iter())
    protected = _protected_from_ancestors(index[id(object_node)].ancestors)
    inside_revision, ancestry = _revision_metadata((), extra_nodes=index[id(object_node)].ancestors)
    adjacent = {
        "adjacent_bookmark": protected["bookmark"],
        "adjacent_field": protected["field"],
        "adjacent_hyperlink": protected["hyperlink"],
        "adjacent_drawing": protected["drawing"],
    }
    row = {
        "latex": source,
        "layout": _layout(paragraph),
        "paragraph": block_map[id(paragraph)],
        "sequence_in_paragraph": 0,
        "source": source,
        "raw_source": source,
        "anchor_before": anchor_before if anchor_before.strip() else None,
        "anchor_after": anchor_after if anchor_after.strip() else None,
        "package_part": package_part,
        "story": story,
        "source_type": SourceType.EMBEDDED_EQUATION_OBJECT.value,
        "confidence": Confidence.REVIEW_REQUIRED.value,
        "expected_matches": 1,
        "status": OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
        "inside_existing_revision": inside_revision,
        "revision_ancestry": ancestry,
        **adjacent,
        "protected_containers": protected,
        "extensions": {
            "inventory": {
                "source_view": "current",
                "detection_signal": "embedded_object",
                "node_path": index[id(object_node)].path,
                "relationship_ids": relationship_ids,
            }
        },
    }
    return Candidate(paragraph, order, row)


def _assign_sequences_and_ids(candidates: list[Candidate], source_sha256: str) -> list[dict]:
    candidates.sort(
        key=lambda item: (
            item.row.get("package_part", ""),
            item.row.get("paragraph", 0),
            item.order,
            item.row.get("extensions", {}).get("inventory", {}).get("source_view", ""),
        )
    )
    sequences: dict[tuple[str, int], int] = {}
    rows: list[dict] = []
    for position, candidate in enumerate(candidates, 1):
        row = candidate.row
        key = (row.get("package_part", ""), row.get("paragraph", 0))
        sequences[key] = sequences.get(key, 0) + 1
        row["sequence_in_paragraph"] = sequences[key]
        row["id"] = deterministic_occurrence_id(row, source_sha256=source_sha256, position=position)
        rows.append(row)
    return rows


def inventory_docx(source: str | Path) -> Manifest:
    """Inventory a DOCX without changing the source file or its package."""

    path = Path(source)
    source_bytes, parts, roots, relationships = _load_package(path)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    candidates: list[Candidate] = []
    unsupported_parts: list[str] = []
    source_views = {"current": 0, "deleted": 0, "omml": 0}

    for package_part in sorted(roots):
        root = roots[package_part]
        if package_part.endswith(".rels") or _namespace(root.tag) != W:
            continue
        story, unknown_story = _root_story(root)
        if story == "unknown":
            unsupported_parts.append(package_part)
        index = _index_tree(root)
        block_map = _block_map(index)
        paragraphs = [info.element for info in index.values() if _is(info.element, W, "p")]
        paragraphs.sort(key=lambda item: index[id(item)].ordinal)
        for paragraph in paragraphs:
            current = _build_surface(paragraph, deleted=False, index=index)
            deleted = _build_surface(paragraph, deleted=True, index=index)
            for surface, is_deleted in ((current, False), (deleted, True)):
                for match in _text_matches(surface.text):
                    if _match_is_eq_field_result(match, surface):
                        continue
                    candidate = _text_candidate(
                        match=match,
                        surface=surface,
                        paragraph=paragraph,
                        index=index,
                        block_map=block_map,
                        story=story,
                        package_part=package_part,
                        deleted=is_deleted,
                        order=match.start,
                    )
                    if unknown_story:
                        candidate.row["status"] = OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value
                        candidate.row["extensions"]["inventory"]["unknown_story"] = True
                    candidates.append(candidate)
                    source_views["deleted" if is_deleted else "current"] += 1
            for field in paragraph.findall(".//w:fldSimple", NS):
                field_span = _surface_span(current, field)
                candidate = _field_candidate(
                    field,
                    surface=current,
                    paragraph=paragraph,
                    index=index,
                    block_map=block_map,
                    story=story,
                    package_part=package_part,
                    order=field_span[0] if field_span is not None else index[id(field)].ordinal,
                )
                if candidate is not None:
                    if unknown_story:
                        candidate.row["extensions"]["inventory"]["unknown_story"] = True
                    candidates.append(candidate)
                    source_views["current"] += 1
            for object_node in paragraph.findall(".//w:object", NS):
                if not object_node.findall(".//o:OLEObject", NS):
                    continue
                object_span = _surface_span(current, object_node)
                candidate = _object_candidate(
                    object_node,
                    paragraph=paragraph,
                    surface=current,
                    index=index,
                    block_map=block_map,
                    story=story,
                    package_part=package_part,
                    order=object_span[0] if object_span is not None else index[id(object_node)].ordinal,
                )
                if unknown_story:
                    candidate.row["extensions"]["inventory"]["unknown_story"] = True
                candidates.append(candidate)
                source_views["current"] += 1

        for node in (info.element for info in index.values() if _is(info.element, M, "oMath")):
            candidate = _omml_candidate(
                node,
                index=index,
                block_map=block_map,
                story=story,
                package_part=package_part,
                order=index[id(node)].ordinal,
            )
            if unknown_story:
                candidate.row["extensions"]["inventory"]["unknown_story"] = True
            candidates.append(candidate)
            source_views["omml"] += 1

    rows = _assign_sequences_and_ids(candidates, source_sha256)
    inventory_extension = {
        "inventory": {
            "version": 1,
            "candidate_count": len(rows),
            "source_views": source_views,
            "package_parts": sorted(parts),
            "part_sha256": {
                name: hashlib.sha256(data).hexdigest() for name, data in sorted(parts.items())
            },
            "non_xml_parts": sorted(
                name for name in parts if not (name.endswith(".xml") or name.endswith(".rels"))
            ),
            "unsupported_parts": sorted(set(unsupported_parts)),
            "relationship_owner_count": len(relationships),
            "relationships": {
                owner: {
                    relationship_id: {
                        "relationship_type": relationship.relationship_type,
                        "target": relationship.target,
                        "external": relationship.external,
                    }
                    for relationship_id, relationship in sorted(entries.items())
                }
                for owner, entries in sorted(relationships.items())
            },
        }
    }
    try:
        return load_manifest(
            {
                "schema_version": 1,
                "source_sha256": source_sha256,
                "formulas": rows,
                "extensions": inventory_extension,
            }
        )
    except ContractError as error:
        raise InventoryError(f"inventory output violates the manifest contract: {error}") from error


def write_inventory(source: str | Path, output: str | Path) -> Manifest:
    """Write an inventory manifest to a path distinct from the source DOCX."""

    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    same_file = False
    if output_path.exists():
        try:
            same_file = os.path.samefile(source_path, output_path)
        except OSError:
            same_file = False
    if source_path == output_path or same_file:
        raise InventoryError("inventory output must not overwrite the source DOCX")
    manifest = inventory_docx(source_path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        dump_manifest(manifest, output_path)
    except (OSError, ContractError) as error:
        raise InventoryError(f"cannot write inventory manifest {output_path}: {error}") from error
    return manifest
