"""Occurrence-level Word math style resolution.

This module resolves the style that may be applied to newly generated OMML.
It deliberately separates prose formatting from math-font selection: source
fonts are evidence about context, never a reason to put a prose font on a
math run.  All decisions are returned with provenance so a later applicator
can use the same result without inventing another precedence path.

Style sources, from highest to lowest precedence, are:

``occurrence_override`` -> ``source_run`` -> ``semantic_block`` ->
``character_style`` -> ``paragraph_style`` -> ``document_default``.

The source run is normally the manifest's ``style_snapshot``.  Character and
paragraph style arguments may be IDs resolved through ``styles`` or direct
mappings.  A style catalog can be a mapping with ``character`` and
``paragraph`` dictionaries, or a ``w:styles`` XML element.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from xml.etree import ElementTree as ET

from word_formula_omml.contract import (
    Manifest,
    OccurrenceStatus,
    SourceType,
    load_manifest,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MATH_FONT = "Cambria Math"


class StyleError(ValueError):
    """Raised for a style input that cannot be resolved safely."""


class StyleStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"


STYLE_FIELDS = (
    "color",
    "size",
    "highlight",
    "underline",
    "math_style",
    "alignment",
    "spacing_before",
    "spacing_after",
    "line",
    "line_rule",
)
PARAGRAPH_FIELDS = frozenset({"alignment", "spacing_before", "spacing_after", "line", "line_rule"})
MATH_STYLES = frozenset({"bold", "italic", "calligraphic", "blackboard", "none"})
ALIGNMENTS = frozenset({"left", "center", "right", "both", "justify", "distribute", "start", "end"})
HIGHLIGHTS = frozenset(
    {
        "black",
        "blue",
        "cyan",
        "darkblue",
        "darkcyan",
        "darkgray",
        "darkgreen",
        "darkmagenta",
        "darkred",
        "darkyellow",
        "green",
        "lightgray",
        "magenta",
        "none",
        "red",
        "white",
        "yellow",
    }
)
UNDERLINES = frozenset(
    {
        "dash",
        "dashedheavy",
        "dashlong",
        "dashlongheavy",
        "dotdash",
        "dotdashheavy",
        "dotdotdash",
        "dotdotdashheavy",
        "dotted",
        "dottedheavy",
        "double",
        "none",
        "single",
        "thick",
        "wave",
        "wavyheavy",
        "wavydouble",
        "words",
    }
)
_MISSING = object()


@dataclass(frozen=True)
class StyleResolution:
    """Serializable result of resolving one occurrence's math style."""

    status: str
    layout: str
    style: Mapping[str, Any]
    provenance: Mapping[str, str]
    conflicts: tuple[str, ...]
    warnings: tuple[str, ...]
    reason: str

    @property
    def auto_eligible(self) -> bool:
        return self.status == StyleStatus.RESOLVED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "auto_eligible": self.auto_eligible,
            "layout": self.layout,
            "style": copy.deepcopy(dict(self.style)),
            "provenance": copy.deepcopy(dict(self.provenance)),
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "reason": self.reason,
        }


def _q(local: str) -> str:
    return f"{{{W}}}{local}"


def _direct(parent: ET.Element | None, local: str) -> ET.Element | None:
    if parent is None:
        return None
    matches = [child for child in parent if child.tag == _q(local)]
    if len(matches) > 1:
        raise StyleError(f"style XML contains duplicate w:{local}")
    return matches[0] if matches else None


def _attr(node: ET.Element | None, local: str) -> str | None:
    if node is None:
        return None
    return node.get(_q(local)) or node.get(local)


def _on_off(node: ET.Element) -> bool:
    value = _attr(node, "val")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "nil", "off", "none"}


def _run_properties(properties: ET.Element | None) -> dict[str, Any]:
    if properties is None:
        return {}
    result: dict[str, Any] = {}
    fonts = _direct(properties, "rFonts")
    if fonts is not None:
        values = {
            key: _attr(fonts, key)
            for key in ("ascii", "hAnsi", "eastAsia", "cs", "asciiTheme", "hAnsiTheme", "eastAsiaTheme", "csTheme")
            if _attr(fonts, key)
        }
        if values:
            result["fonts"] = values
    for local, key in (
        ("rStyle", "character_style"),
        ("color", "color"),
        ("sz", "size"),
        ("highlight", "highlight"),
        ("u", "underline"),
    ):
        child = _direct(properties, local)
        value = _attr(child, "val")
        if value is not None:
            result[key] = value
    for local, key in (("b", "bold"), ("i", "italic")):
        child = _direct(properties, local)
        if child is not None:
            result[key] = _on_off(child)
    return result


def _paragraph_properties(properties: ET.Element | None) -> dict[str, Any]:
    if properties is None:
        return {}
    result: dict[str, Any] = {}
    for local, key in (("pStyle", "paragraph_style"), ("jc", "alignment")):
        child = _direct(properties, local)
        value = _attr(child, "val")
        if value is not None:
            result[key] = value
    spacing = _direct(properties, "spacing")
    if spacing is not None:
        for attribute, key in (("before", "spacing_before"), ("after", "spacing_after"), ("line", "line"), ("lineRule", "line_rule")):
            value = _attr(spacing, attribute)
            if value is not None:
                result[key] = value
    return result


def snapshot_run_style(run: ET.Element | None) -> dict[str, Any]:
    """Extract direct ``w:rPr`` style evidence using the manifest shape."""

    if run is None:
        return {}
    return _run_properties(_direct(run, "rPr"))


def snapshot_paragraph_style(paragraph: ET.Element | None) -> dict[str, Any]:
    """Extract direct paragraph style evidence relevant to equation layout."""

    if paragraph is None:
        return {}
    return _paragraph_properties(_direct(paragraph, "pPr"))


def _style_element_values(style: ET.Element) -> dict[str, Any]:
    values = {}
    values.update(_paragraph_properties(_direct(style, "pPr")))
    values.update(_run_properties(_direct(style, "rPr")))
    based_on = _direct(style, "basedOn")
    if based_on is not None and _attr(based_on, "val") is not None:
        values["based_on"] = _attr(based_on, "val")
    return values


def style_catalog(styles: ET.Element | Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a style catalog mapping or ``w:styles`` XML element."""

    if styles is None:
        return {"character": {}, "paragraph": {}, "document_default": {}}
    if isinstance(styles, Mapping):
        recognized = {"character", "character_styles", "paragraph", "paragraph_styles", "document_default", "defaults"}
        if not any(key in styles for key in recognized):
            return {"character": copy.deepcopy(dict(styles)), "paragraph": copy.deepcopy(dict(styles)), "document_default": {}}
        catalog = {
            "character": styles.get("character", styles.get("character_styles", {})),
            "paragraph": styles.get("paragraph", styles.get("paragraph_styles", {})),
            "document_default": styles.get("document_default", styles.get("defaults", {})),
        }
        for role, definitions in catalog.items():
            if not isinstance(definitions, Mapping):
                raise StyleError(f"{role} style catalog must be a mapping")
        return {role: copy.deepcopy(dict(definitions)) for role, definitions in catalog.items()}
    if not isinstance(styles, ET.Element):
        raise StyleError("styles must be a mapping, w:styles element, or None")
    catalog: dict[str, Any] = {"character": {}, "paragraph": {}, "document_default": {}}
    for style in (child for child in styles if child.tag == _q("style")):
        style_id = _attr(style, "styleId")
        style_type = _attr(style, "type")
        if not style_id or style_type not in {"character", "paragraph"}:
            continue
        if style_id in catalog[style_type]:
            raise StyleError(f"duplicate_{style_type}_style:{style_id}")
        catalog[style_type][style_id] = _style_element_values(style)
    defaults = _direct(styles, "docDefaults")
    if defaults is not None:
        default_run = _direct(_direct(defaults, "rPrDefault"), "rPr")
        default_paragraph = _direct(_direct(defaults, "pPrDefault"), "pPr")
        catalog["document_default"].update(_paragraph_properties(default_paragraph))
        catalog["document_default"].update(_run_properties(default_run))
    return catalog


def _merge_style(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(parent))
    result.update(copy.deepcopy(dict(child)))
    return result


def _resolve_style_reference(
    reference: Any,
    role: str,
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    if reference is None:
        return {}, None
    if isinstance(reference, Mapping):
        if "based_on" in reference or "basedOn" in reference:
            return {}, f"inline_{role}_style_inheritance_requires_catalog"
        return copy.deepcopy(dict(reference)), None
    if not isinstance(reference, str) or not reference.strip():
        return {}, f"invalid_{role}_style_reference"
    definitions = catalog.get(role, {})
    if not isinstance(definitions, Mapping) or reference not in definitions:
        return {}, f"unknown_{role}_style:{reference}"
    visiting: set[str] = set()

    def visit(style_id: str) -> dict[str, Any]:
        if style_id in visiting:
            raise StyleError(f"cyclic_{role}_style_inheritance:{style_id}")
        raw = definitions.get(style_id)
        if not isinstance(raw, Mapping):
            raise StyleError(f"invalid_{role}_style_definition:{style_id}")
        visiting.add(style_id)
        parent_id = raw.get("based_on", raw.get("basedOn"))
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.strip()):
            raise StyleError(f"invalid_{role}_style_parent:{style_id}")
        inherited = visit(parent_id) if parent_id is not None else {}
        visiting.remove(style_id)
        own = {key: value for key, value in raw.items() if key not in {"based_on", "basedOn", "style_id", "type", "name"}}
        return _merge_style(inherited, own)

    try:
        return visit(reference), None
    except StyleError as error:
        return {}, str(error)


def _normalize_color(value: Any) -> str:
    if not isinstance(value, str):
        raise StyleError("color must be a six-digit hex value or 'auto'")
    value = value.strip()
    if value.lower() == "auto":
        return "auto"
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise StyleError(f"unsupported_color:{value!r}")
    return value.upper()


def _normalize_size(value: Any) -> str:
    if isinstance(value, bool):
        raise StyleError("size must be a positive Word half-point integer")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", value.strip()):
        raise StyleError(f"unsupported_size:{value!r}")
    numeric = int(value)
    if numeric > 32767:
        raise StyleError(f"unsupported_size:{value!r}")
    return str(numeric)


def _normalize_choice(value: Any, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StyleError(f"{field} must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise StyleError(f"unsupported_{field}:{value!r}")
    return normalized


def _normalize_spacing(value: Any, field: str) -> str:
    if isinstance(value, bool):
        raise StyleError(f"{field} must be an integer or supported token")
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value.strip() or not re.fullmatch(r"-?[0-9]+|auto", value.strip(), re.IGNORECASE):
        raise StyleError(f"unsupported_{field}:{value!r}")
    return value.strip().lower() if value.strip().lower() == "auto" else str(int(value.strip()))


def _normalize_math_style(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in MATH_STYLES:
        raise StyleError(f"unsupported_math_style:{value!r}")
    return value.strip().lower()


def _merge_nested_style(value: Mapping[str, Any]) -> dict[str, Any]:
    nested = value.get("style")
    if nested is None:
        return dict(value)
    if not isinstance(nested, Mapping):
        raise StyleError("style field must be an object")
    merged = dict(nested)
    merged.update({key: item for key, item in value.items() if key != "style"})
    return merged


def _extract_layer(layer: Any, layer_name: str) -> tuple[dict[str, Any], set[str], set[str], list[str]]:
    """Return normalized values, conflicts, unsupported reasons, and warnings."""

    if layer is None:
        return {}, set(), set(), []
    if not isinstance(layer, Mapping):
        return {}, set(), {f"invalid_style_layer:{layer_name}"}, []
    value = _merge_nested_style(layer)
    values: dict[str, Any] = {}
    conflicts: set[str] = set()
    unsupported: set[str] = set()
    warnings: list[str] = []
    metadata = {
        "character_style",
        "paragraph_style",
        "conflict",
        "runs",
        "based_on",
        "basedOn",
        "style_id",
        "type",
        "name",
        "semantic_emphasis",
        "font",
        "fonts",
        "math_font",
        "math_bold",
        "math_italic",
        "math_style",
        "vector",
        "math_vector",
        "bold",
        "italic",
    }
    aliases = {
        "font_size": "size",
        "font_color": "color",
        "lineRule": "line_rule",
        "spacingBefore": "spacing_before",
        "spacingAfter": "spacing_after",
    }
    for key, raw in value.items():
        if key in metadata or key in {"style", "paragraph"}:
            continue
        field = aliases.get(key, key)
        if field not in STYLE_FIELDS:
            unsupported.add(f"unsupported_style_field:{layer_name}.{key}")
            continue
        try:
            if raw is None:
                continue
            if field == "color":
                values[field] = _normalize_color(raw)
            elif field == "size":
                values[field] = _normalize_size(raw)
            elif field == "highlight":
                values[field] = _normalize_choice(raw, HIGHLIGHTS, field)
            elif field == "underline":
                values[field] = _normalize_choice(raw, UNDERLINES, field)
            elif field == "alignment":
                values[field] = _normalize_choice(raw, ALIGNMENTS, field)
            elif field in {"spacing_before", "spacing_after", "line"}:
                values[field] = _normalize_spacing(raw, field)
            elif field == "line_rule":
                values[field] = _normalize_choice(raw, frozenset({"auto", "exact", "atleast"}), field)
        except StyleError as error:
            unsupported.add(f"{layer_name}:{error}")

    if "paragraph" in value:
        paragraph = value["paragraph"]
        if not isinstance(paragraph, Mapping):
            unsupported.add(f"invalid_paragraph_style_layer:{layer_name}")
        else:
            nested_values, nested_conflicts, nested_unsupported, nested_warnings = _extract_layer(paragraph, layer_name)
            values.update({key: item for key, item in nested_values.items() if key in PARAGRAPH_FIELDS})
            conflicts.update(nested_conflicts)
            unsupported.update(nested_unsupported)
            warnings.extend(nested_warnings)

    runs = value.get("runs")
    if runs is not None and value.get("conflict") is not True:
        unsupported.add(f"{layer_name}:runs_require_conflict_marker")
    if value.get("conflict") is True:
        if not isinstance(runs, list) or not runs:
            conflicts.add(f"{layer_name}:style_context")
        else:
            run_values = []
            for run in runs:
                extracted, run_conflicts, run_unsupported, run_warnings = _extract_layer(run, layer_name)
                run_values.append(extracted)
                conflicts.update(run_conflicts)
                unsupported.update(run_unsupported)
                warnings.extend(run_warnings)
            for field in STYLE_FIELDS:
                distinct = {repr(item.get(field, _MISSING)) for item in run_values}
                if len(distinct) > 1:
                    conflicts.add(f"{layer_name}:{field}")

    if "fonts" in value or "font" in value:
        raw_fonts = value.get("fonts", value.get("font"))
        if isinstance(raw_fonts, str):
            raw_fonts = {"font": raw_fonts}
        if not isinstance(raw_fonts, Mapping):
            unsupported.add(f"invalid_fonts:{layer_name}")
        else:
            font_values = [item for item in raw_fonts.values() if item is not None]
            if not all(isinstance(item, str) and item.strip() for item in font_values):
                unsupported.add(f"invalid_fonts:{layer_name}")
            elif font_values and any(item.strip().lower() != MATH_FONT.lower() for item in font_values):
                warnings.append(f"prose_fonts_not_copied:{layer_name}")

    explicit_math_styles: list[str] = []
    if "math_style" in value and value["math_style"] is not None:
        try:
            explicit_math_styles.append(_normalize_math_style(value["math_style"]))
        except StyleError as error:
            unsupported.add(f"{layer_name}:{error}")
    for key, style in (("math_bold", "bold"), ("math_italic", "italic")):
        if key in value:
            if not isinstance(value[key], bool):
                unsupported.add(f"{layer_name}:{key}_must_be_boolean")
            elif value[key]:
                explicit_math_styles.append(style)
            else:
                explicit_math_styles.append("none")
    emphasis = value.get("semantic_emphasis")
    if emphasis is not None and emphasis not in {"math", "prose", "contextual"}:
        unsupported.add(f"unsupported_semantic_emphasis:{emphasis!r}")
    default_math_emphasis = layer_name in {"occurrence_override", "source_run", "semantic_block"}
    if emphasis == "math" or (emphasis is None and default_math_emphasis):
        for key, style in (("bold", "bold"), ("italic", "italic")):
            if key in value:
                if not isinstance(value[key], bool):
                    unsupported.add(f"{layer_name}:{key}_must_be_boolean")
                elif value[key]:
                    explicit_math_styles.append(style)
                else:
                    explicit_math_styles.append("none")
    if value.get("vector") is True or value.get("math_vector") is True:
        conflicts.add(f"{layer_name}:vector_semantics_requires_formula_ir")
    if value.get("vector") not in {None, True, False} or value.get("math_vector") not in {None, True, False}:
        unsupported.add(f"invalid_vector_flag:{layer_name}")
    if explicit_math_styles:
        if len(set(explicit_math_styles)) > 1 or (len(explicit_math_styles) == 1 and explicit_math_styles[0] not in MATH_STYLES):
            conflicts.add(f"{layer_name}:math_style")
        else:
            values["math_style"] = explicit_math_styles[0]
    if "math_font" in value and value["math_font"] is not None:
        if not isinstance(value["math_font"], str) or value["math_font"].strip().lower() != MATH_FONT.lower():
            conflicts.add(f"{layer_name}:math_font")
    return values, conflicts, unsupported, list(dict.fromkeys(warnings))


def _failure(layout: str, status: StyleStatus, reason: str) -> StyleResolution:
    return StyleResolution(
        status.value,
        layout,
        {"math_font": MATH_FONT, "math_font_policy": "CAMBRIA_MATH"},
        {"math_font": "word_math_default"},
        (reason,),
        (),
        reason,
    )


def resolve_style(
    occurrence: Mapping[str, Any],
    *,
    occurrence_override: Mapping[str, Any] | None = None,
    semantic_block: Mapping[str, Any] | None = None,
    character_style: Mapping[str, Any] | str | None = None,
    paragraph_style: Mapping[str, Any] | str | None = None,
    document_default: Mapping[str, Any] | None = None,
    styles: ET.Element | Mapping[str, Any] | None = None,
) -> StyleResolution:
    """Resolve one occurrence with explicit precedence and fail-closed errors."""

    if not isinstance(occurrence, Mapping):
        return _failure("inline", StyleStatus.UNSUPPORTED, "occurrence must be an object")
    layout = occurrence.get("target_layout", occurrence.get("layout", "inline"))
    if not isinstance(layout, str) or layout not in {"inline", "display"}:
        return _failure("inline", StyleStatus.UNSUPPORTED, f"unsupported_layout:{layout!r}")
    if occurrence.get("source_type") == SourceType.EXISTING_OMML.value:
        return _failure(layout, StyleStatus.UNSUPPORTED, "existing_omml_not_restyled")
    try:
        catalog = style_catalog(styles)
    except StyleError as error:
        return _failure(layout, StyleStatus.UNSUPPORTED, str(error))
    source_run = occurrence.get("style_snapshot") or {}
    if not isinstance(source_run, Mapping):
        return _failure(layout, StyleStatus.UNSUPPORTED, "invalid_source_run_style_snapshot")
    manifest_override = {
        field: occurrence[field]
        for field in ("color",)
        if field in occurrence and occurrence[field] is not None
    }
    extensions = occurrence.get("extensions")
    extension_style = extensions.get("style", {}) if isinstance(extensions, Mapping) else {}
    if occurrence_override is None and isinstance(extension_style, Mapping):
        occurrence_override = extension_style.get("occurrence_override")
    if occurrence_override is None and manifest_override:
        occurrence_override = manifest_override
    if semantic_block is None and isinstance(extension_style, Mapping):
        semantic_block = extension_style.get("semantic_block")
    source_character_id = source_run.get("character_style")
    source_paragraph_id = occurrence.get("paragraph_style")
    character_reference = character_style if character_style is not None else source_character_id
    paragraph_reference = paragraph_style if paragraph_style is not None else source_paragraph_id
    character_values, character_error = _resolve_style_reference(character_reference, "character", catalog)
    paragraph_values, paragraph_error = _resolve_style_reference(paragraph_reference, "paragraph", catalog)
    layers = [
        ("occurrence_override", occurrence_override),
        ("source_run", source_run),
        ("semantic_block", semantic_block),
        ("character_style", character_values),
        ("paragraph_style", paragraph_values),
        ("document_default", document_default if document_default is not None else catalog.get("document_default", {})),
    ]
    conflicts: set[str] = set()
    unsupported: set[str] = set()
    warnings: list[str] = []
    extracted: list[tuple[str, dict[str, Any], set[str], set[str]]] = []
    if character_error:
        unsupported.add(character_error)
    if paragraph_error:
        unsupported.add(paragraph_error)
    for name, layer in layers:
        try:
            values, layer_conflicts, layer_unsupported, layer_warnings = _extract_layer(layer, name)
        except StyleError as error:
            values, layer_conflicts, layer_unsupported, layer_warnings = {}, set(), {f"{name}:{error}"}, []
        extracted.append((name, values, layer_conflicts, layer_unsupported))
        for item in layer_conflicts:
            if not any(item == field or item.endswith(f":{field}") for field in STYLE_FIELDS):
                conflicts.add(f"{name}:{item}" if not item.startswith(f"{name}:") else item)
        unsupported.update(layer_unsupported)
        warnings.extend(layer_warnings)

    selected: dict[str, Any] = {}
    provenance: dict[str, str] = {"math_font": "word_math_default"}
    overridden: list[str] = []
    for field in STYLE_FIELDS:
        chosen_layer: str | None = None
        for name, values, _layer_conflicts, _layer_unsupported in extracted:
            if field in values:
                selected[field] = values[field]
                chosen_layer = name
                provenance[field] = name
                break
        layer_order = [item[0] for item in extracted]
        chosen_index = layer_order.index(chosen_layer) if chosen_layer is not None else len(layer_order)
        for name, _values, layer_conflicts, _layer_unsupported in extracted:
            if any(item == field or item.endswith(f":{field}") for item in layer_conflicts):
                if layer_order.index(name) <= chosen_index:
                    conflicts.add(f"{name}:{field}")
                else:
                    overridden.append(f"{name}:{field}")

    if overridden:
        warnings.extend(f"higher_precedence_conflict_overrode:{item}" for item in overridden)
    if "math_style" in selected and selected["math_style"] == "none":
        selected["math_style"] = "none"
    if layout == "display":
        paragraph: dict[str, Any] = {}
        for field in PARAGRAPH_FIELDS:
            if field in selected:
                paragraph[field] = selected.pop(field)
                provenance[f"paragraph.{field}"] = provenance.pop(field)
        if paragraph:
            selected["paragraph"] = paragraph
    else:
        if any(field in selected for field in PARAGRAPH_FIELDS):
            warnings.append("inline_paragraph_context_not_applied")
            for field in PARAGRAPH_FIELDS:
                selected.pop(field, None)
                provenance.pop(field, None)

    warnings = list(dict.fromkeys(warnings))
    if unsupported:
        reason = sorted(unsupported)[0]
        return StyleResolution(
            StyleStatus.UNSUPPORTED.value,
            layout,
            {"math_font": MATH_FONT, "math_font_policy": "CAMBRIA_MATH"},
            {"math_font": "word_math_default"},
            tuple(sorted(unsupported)),
            tuple(warnings),
            reason,
        )
    if conflicts:
        reason = sorted(conflicts)[0]
        return StyleResolution(
            StyleStatus.NEEDS_REVIEW.value,
            layout,
            {"math_font": MATH_FONT, "math_font_policy": "CAMBRIA_MATH", **selected},
            provenance,
            tuple(sorted(conflicts)),
            tuple(warnings),
            reason,
        )
    resolved = {"math_font": MATH_FONT, "math_font_policy": "CAMBRIA_MATH", **selected}
    reason = "style_resolved"
    return StyleResolution(
        StyleStatus.RESOLVED.value,
        layout,
        resolved,
        provenance,
        (),
        tuple(warnings),
        reason,
    )


def resolve_manifest_styles(
    manifest: Manifest | Mapping[str, Any] | str,
    *,
    contexts: Mapping[str, Mapping[str, Any]] | None = None,
    occurrence_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    styles: ET.Element | Mapping[str, Any] | None = None,
    document_default: Mapping[str, Any] | None = None,
) -> Manifest:
    """Attach deterministic ``resolved_style`` records to every occurrence."""

    current = manifest if isinstance(manifest, Manifest) else load_manifest(manifest)
    contexts = contexts or {}
    occurrence_overrides = occurrence_overrides or {}
    rows: list[dict[str, Any]] = []
    for original in current.formulas:
        row = copy.deepcopy(original)
        context = contexts.get(row["id"], {})
        if not isinstance(context, Mapping):
            raise StyleError(f"style context for {row['id']} must be an object")
        resolution = resolve_style(
            row,
            occurrence_override=occurrence_overrides.get(row["id"], context.get("occurrence_override")),
            semantic_block=context.get("semantic_block"),
            character_style=context.get("character_style"),
            paragraph_style=context.get("paragraph_style"),
            document_default=context.get("document_default", document_default),
            styles=styles,
        )
        row["resolved_style"] = resolution.to_dict()
        if not resolution.auto_eligible and row.get("status") not in {
            OccurrenceStatus.PRESERVED.value,
            OccurrenceStatus.EXCLUDED.value,
            OccurrenceStatus.STAGED.value,
            OccurrenceStatus.AUDITED.value,
            OccurrenceStatus.APPLIED.value,
            OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value,
            OccurrenceStatus.NEEDS_REVIEW.value,
            OccurrenceStatus.REFUSED.value,
            OccurrenceStatus.FAILED.value,
        }:
            row["status"] = OccurrenceStatus.NEEDS_REVIEW.value
        rows.append(row)
    data: dict[str, Any] = {
        "schema_version": current.schema_version,
        "formulas": rows,
        "extensions": copy.deepcopy(dict(current.extensions)),
    }
    if current.source_sha256 is not None:
        data["source_sha256"] = current.source_sha256
    if current.revision_author is not None:
        data["revision_author"] = current.revision_author
    return load_manifest(data)


__all__ = [
    "ALIGNMENTS",
    "HIGHLIGHTS",
    "MATH_FONT",
    "MATH_STYLES",
    "StyleError",
    "StyleResolution",
    "StyleStatus",
    "UNDERLINES",
    "resolve_manifest_styles",
    "resolve_style",
    "snapshot_paragraph_style",
    "snapshot_run_style",
    "style_catalog",
]
