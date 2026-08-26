"""Shared OMML-to-canonical semantic validation.

The generator and audit stages must agree on what a valid equation means.  This
module is the single semantic bridge for the deliberately small V1 subset.  It
parses structure from OMML rather than treating the visible text of an
``m:oMath`` node as proof of mathematical equivalence.

Unsupported OMML is reported explicitly.  Callers may inspect the parsed
value for review, but an unsupported or mismatching value is never an
automatic semantic pass.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

from word_formula_omml.canonical import CanonicalError, canonical_equal, canonicalize_formula


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


class SemanticError(ValueError):
    """Base error for malformed or unsupported OMML semantic input."""


class UnsupportedOMML(SemanticError):
    """Raised when an OMML structure is outside the declared V1 subset."""


class SemanticStatus(str, Enum):
    PASS = "PASS"
    MISMATCH = "MISMATCH"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


# This is intentionally explicit.  A future workstream may extend the matrix,
# but a new OMML tag must not become supported merely because the parser happens
# to ignore it.
SEMANTIC_SUPPORT_MATRIX: dict[str, dict[str, Any]] = {
    "atom": {"canonical_kinds": ["string"], "omml_tags": ["r"]},
    "identifier": {"canonical_kinds": ["identifier"], "omml_tags": ["r"]},
    "sequence": {"canonical_kinds": ["sequence"], "omml_tags": ["oMath"]},
    "implicit_product": {"canonical_kinds": ["implicit_product"], "omml_tags": ["oMath"]},
    "script": {"canonical_kinds": ["script"], "omml_tags": ["sSub", "sSup", "sSubSup"]},
    "grouped_exponent": {"canonical_kinds": ["grouped_exponent"], "omml_tags": ["sSub"]},
    "fraction": {"canonical_kinds": ["fraction"], "omml_tags": ["f"]},
    "root": {"canonical_kinds": ["root"], "omml_tags": ["rad"]},
    "delimited": {"canonical_kinds": ["delimited"], "omml_tags": ["d"]},
    "interval": {"canonical_kinds": ["interval"], "omml_tags": ["d", "r", "oMath"]},
    "relation": {"canonical_kinds": ["relation"], "omml_tags": ["r", "oMath"]},
    "addition": {"canonical_kinds": ["addition"], "omml_tags": ["r", "oMath"]},
    "subtraction": {"canonical_kinds": ["subtraction"], "omml_tags": ["r", "oMath"]},
    "binary_operator": {"canonical_kinds": ["binary_operator"], "omml_tags": ["r", "oMath"]},
    "operator_sequence": {"canonical_kinds": ["operator_sequence"], "omml_tags": ["r", "oMath"]},
    "unicode_operator_sequence": {"canonical_kinds": ["unicode_operator_sequence"], "omml_tags": ["r", "oMath"]},
    "operator": {"canonical_kinds": ["operator"], "omml_tags": ["r"]},
    "function": {"canonical_kinds": ["function"], "omml_tags": ["func"]},
    "function_call": {"canonical_kinds": ["function_call"], "omml_tags": ["func"]},
    "styled": {"canonical_kinds": ["styled"], "omml_tags": ["r"]},
    "roman": {"canonical_kinds": ["roman"], "omml_tags": ["r"]},
    "text": {"canonical_kinds": ["text"], "omml_tags": ["r"]},
    "accent": {"canonical_kinds": ["accent"], "omml_tags": ["acc", "bar", "groupChr"]},
    "unary_plus": {"canonical_kinds": ["unary_plus"], "omml_tags": ["r", "oMath"]},
    "unary_minus": {"canonical_kinds": ["unary_minus"], "omml_tags": ["r", "oMath"]},
}

SUPPORTED_CANONICAL_KINDS = frozenset(SEMANTIC_SUPPORT_MATRIX)


@dataclass(frozen=True)
class SemanticResult:
    """Serializable outcome of one OMML/canonical comparison."""

    status: str
    expected: Any | None
    actual: Any | None
    family: str | None
    reason: str
    differences: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == SemanticStatus.PASS.value

    @property
    def auto_eligible(self) -> bool:
        return self.passed

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "expected": copy.deepcopy(self.expected),
            "actual": copy.deepcopy(self.actual),
            "family": self.family,
            "reason": self.reason,
            "differences": list(self.differences),
            "auto_eligible": self.auto_eligible,
        }
        return result


@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    unicode: bool = False


_UNICODE_ATOMS = {
    "\u03b1": "alpha",
    "\u03b2": "beta",
    "\u03b3": "gamma",
    "\u03b4": "delta",
    "\u03b5": "epsilon",
    "\u03b8": "theta",
    "\u03bb": "lambda",
    "\u03bc": "mu",
    "\u03c0": "pi",
    "\u03c3": "sigma",
    "\u03c6": "phi",
    "\u03c9": "omega",
}
_UNICODE_OPERATORS = {
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2260": "!=",
    "\u2248": "~",
    "\u2208": "in",
    "\u2209": "notin",
    "\u00b1": "+/-",
    "\u2213": "-/+",
    "\u00d7": "*",
    "\u00b7": "*",
    "\u00f7": "/",
    "\u2212": "-",
}
_RELATIONS = {"=", "<", ">", "<=", ">=", "!=", "~", "in", "notin"}
_ADDITIVE = {"+", "-", "+/-", "-/+"}
_MULTIPLICATIVE = {"*", "/"}
_OPERATORS = _RELATIONS | _ADDITIVE | _MULTIPLICATIVE
_FUNCTION_NAMES = {"sin", "cos", "tan", "log", "ln", "exp"}
_NUMERIC_LITERAL = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
_NUMERIC_LITERAL_RE = re.compile(rf"^{_NUMERIC_LITERAL}$")
_RAW_NUMERIC_INTERVAL_RE = re.compile(
    rf"^[ \t]*([\(\[])[ \t]*({_NUMERIC_LITERAL})[ \t]*,[ \t]*({_NUMERIC_LITERAL})[ \t]*([\)\]])[ \t]*$"
)


def _tag(node: ET.Element) -> tuple[str, str]:
    if not isinstance(node.tag, str):
        raise SemanticError("OMML node has no element tag")
    if not node.tag.startswith("{") or "}" not in node.tag:
        raise UnsupportedOMML(f"OMML node {node.tag!r} has no namespace")
    namespace, local = node.tag[1:].split("}", 1)
    return namespace, local


def _local(node: ET.Element) -> str:
    return _tag(node)[1]


def _attr(node: ET.Element, name: str) -> str | None:
    return node.get(f"{{{M}}}{name}") or node.get(name)


def _direct(node: ET.Element, local: str) -> list[ET.Element]:
    return [child for child in node if _local(child) == local and _tag(child)[0] == M]


def _one(node: ET.Element, local: str, *, required: bool = True) -> ET.Element | None:
    matches = _direct(node, local)
    if len(matches) > 1:
        raise SemanticError(f"OMML {local!r} has more than one child")
    if not matches:
        if required:
            raise SemanticError(f"OMML node is missing {local!r}")
        return None
    return matches[0]


def _ensure_children(node: ET.Element, allowed: set[str]) -> None:
    for child in node:
        namespace, local = _tag(child)
        if namespace != M or local not in allowed:
            raise UnsupportedOMML(f"unsupported_{_local(node)}_child:{local}")


def _term(value: Any) -> _Token:
    return _Token("term", value)


def _operator(value: str) -> _Token:
    return _Token("operator", value)


def _comma() -> _Token:
    return _Token("comma", ",")


def _tokenize_text(text: str) -> list[_Token]:
    if not isinstance(text, str) or not text:
        raise SemanticError("OMML math run has empty text")
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if text.startswith("+/-", index):
            tokens.append(_operator("+/-"))
            index += 3
            continue
        if text.startswith("-/+", index):
            tokens.append(_operator("-/+"))
            index += 3
            continue
        if text.startswith("<=", index) or text.startswith(">=", index) or text.startswith("!=", index):
            tokens.append(_operator(text[index : index + 2]))
            index += 2
            continue
        if character in _UNICODE_ATOMS:
            tokens.append(_Token("term", _UNICODE_ATOMS[character], True))
            index += 1
            continue
        if character in _UNICODE_OPERATORS:
            tokens.append(_Token("operator", _UNICODE_OPERATORS[character], True))
            index += 1
            continue
        if character == ",":
            tokens.append(_comma())
            index += 1
            continue
        if character == "\\":
            raise UnsupportedOMML("literal_latex_in_omml_run")
        if character in "_^{}()[]|":
            raise UnsupportedOMML(f"raw_structural_character:{character}")
        if character.isdigit() or (character == "." and index + 1 < len(text) and text[index + 1].isdigit()):
            match = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)", text[index:])
            if match is None:
                raise SemanticError(f"invalid numeric OMML text at {index}")
            tokens.append(_term(match.group(0)))
            index += len(match.group(0))
            continue
        if character.isalpha() or character == "@":
            match = re.match(r"[A-Za-z@]+", text[index:])
            if match is None:
                raise SemanticError(f"invalid identifier OMML text at {index}")
            word = match.group(0)
            value: str | dict[str, str] = word if len(word) == 1 else {"kind": "identifier", "text": word}
            tokens.append(_term(value))
            index += len(word)
            continue
        if character in _OPERATORS:
            tokens.append(_operator(character))
            index += 1
            continue
        if character == "~":
            tokens.append(_operator("~"))
            index += 1
            continue
        raise UnsupportedOMML(f"unsupported_math_run_character:{character}")
    if not tokens:
        raise SemanticError("OMML math run has no semantic tokens")
    return tokens


def _run_properties(node: ET.Element) -> tuple[str | None, bool]:
    properties = _one(node, "rPr", required=False)
    if properties is None:
        return None, False
    style: str | None = None
    script_style: str | None = None
    literal = False
    for child in properties:
        namespace, local = _tag(child)
        if namespace != M:
            raise UnsupportedOMML(f"foreign_run_property:{local}")
        if local == "sty":
            value = _attr(child, "val")
            if value in {None, "p"}:
                continue
            if value == "b":
                style = "bold"
            elif value == "i":
                style = "italic"
            else:
                raise UnsupportedOMML(f"unsupported_math_style:{value}")
        elif local == "scr":
            value = _attr(child, "val")
            if value == "script":
                script_style = "calligraphic"
            elif value == "double-struck":
                script_style = "blackboard"
            else:
                raise UnsupportedOMML(f"unsupported_math_script_style:{value}")
        elif local == "lit":
            value = _attr(child, "val")
            literal = value in {None, "1", "true", "on"}
        elif local == "ctrlPr":
            continue
        elif local in {"nor", "brk", "ins", "del"}:
            # These properties either describe layout or an unsupported
            # revision operation.  A revision property is not silently
            # ignored because it can change the semantic source surface.
            if local in {"ins", "del"}:
                raise UnsupportedOMML(f"unsupported_math_revision_property:{local}")
        else:
            raise UnsupportedOMML(f"unsupported_math_run_property:{local}")
    if style is not None and script_style is not None:
        raise UnsupportedOMML("combined_math_style_not_supported")
    style = script_style or style
    return style, literal


def _literal_text(tokens: Iterable[_Token]) -> str:
    values: list[str] = []
    for token in tokens:
        if token.kind != "term":
            raise UnsupportedOMML("literal_run_contains_operator")
        value = token.value
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict) and value.get("kind") == "identifier":
            values.append(str(value.get("text", "")))
        else:
            raise UnsupportedOMML("literal_run_contains_structured_math")
    result = "".join(values)
    if not result:
        raise SemanticError("literal math run is empty")
    return result


def _style_tokens(tokens: list[_Token], style: str | None, literal: bool) -> list[_Token]:
    if literal:
        if style is not None:
            raise UnsupportedOMML("combined_math_style_not_supported")
        return [_term({"kind": "roman", "text": _literal_text(tokens)})]
    if style is None:
        return tokens
    if len(tokens) != 1 or tokens[0].kind != "term":
        raise UnsupportedOMML("styled_multi_token_run")
    return [_term({"kind": "styled", "style": style, "value": tokens[0].value})]


def _run_text_nodes(node: ET.Element) -> list[ET.Element]:
    text_nodes: list[ET.Element] = []
    for child in node:
        namespace, local = _tag(child)
        if namespace != M:
            raise UnsupportedOMML(f"foreign_math_run_child:{local}")
        if local == "rPr":
            continue
        if local == "t":
            text_nodes.append(child)
        else:
            raise UnsupportedOMML(f"unsupported_math_run_child:{local}")
    if not text_nodes:
        raise SemanticError("OMML math run is missing m:t")
    return text_nodes


def _parse_run(node: ET.Element) -> list[_Token]:
    style, literal = _run_properties(node)
    tokens: list[_Token] = []
    for text_node in _run_text_nodes(node):
        tokens.extend(_tokenize_text(text_node.text or ""))
    return _style_tokens(tokens, style, literal)


def _collect(node: ET.Element, *, allow_comma: bool = False) -> list[_Token]:
    tokens: list[_Token] = []
    for child in node:
        namespace, local = _tag(child)
        if namespace != M:
            raise UnsupportedOMML(f"foreign_omml_child:{local}")
        if local == "r":
            tokens.extend(_parse_run(child))
            continue
        if local in {
            "e",
            "num",
            "den",
            "sub",
            "sup",
            "deg",
            "fName",
            "oMath",
        }:
            tokens.append(_term(_parse_sequence(child, allow_comma=allow_comma)))
            continue
        if local in {"f", "rad", "sSub", "sSup", "sSubSup", "d", "acc", "bar", "func", "groupChr"}:
            tokens.append(_term(_parse_expression(child)))
            continue
        raise UnsupportedOMML(f"unsupported_omml_structure:{local}")
    if not tokens:
        raise SemanticError("OMML expression container is empty")
    if not allow_comma and any(token.kind == "comma" for token in tokens):
        raise UnsupportedOMML("comma_outside_delimiter")
    return tokens


def _precedence(operator: str) -> int:
    if operator in _RELATIONS:
        return 10
    if operator in _ADDITIVE:
        return 20
    return 30


def _combine(operator: str, left: Any, right: Any) -> dict[str, Any]:
    if operator == "-":
        return {"kind": "subtraction", "left": left, "right": right}
    if operator == "+":
        return {"kind": "addition", "left": left, "right": right}
    if operator in _RELATIONS:
        return {"kind": "relation", "operator": operator, "left": left, "right": right}
    return {"kind": "binary_operator", "operator": operator, "left": left, "right": right}


def _implicit(left: Any, right: Any) -> dict[str, Any]:
    if isinstance(left, dict) and left.get("kind") == "function":
        argument = right.get("body") if isinstance(right, dict) and right.get("kind") == "delimited" and right.get("left") == "(" else right
        return {"kind": "function_call", "name": left["name"], "argument": argument}
    if isinstance(left, dict) and left.get("kind") == "identifier" and isinstance(right, dict) and right.get("kind") == "delimited" and right.get("left") == "(":
        return {"kind": "function_call", "name": left["text"], "argument": right["body"]}
    if isinstance(left, str) and isinstance(right, dict) and right.get("kind") == "delimited" and right.get("left") == "(":
        return {"kind": "function_call", "name": left, "argument": right["body"]}
    factors: list[Any] = []
    if isinstance(left, dict) and left.get("kind") == "implicit_product":
        factors.extend(left["factors"])
    else:
        factors.append(left)
    if isinstance(right, dict) and right.get("kind") == "implicit_product":
        factors.extend(right["factors"])
    else:
        factors.append(right)
    return {"kind": "implicit_product", "factors": factors}


class _ExpressionParser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def parse(self) -> Any:
        if not self.tokens:
            raise SemanticError("OMML expression has no tokens")
        value = self.parse_expression(0)
        if self.current is not None:
            raise UnsupportedOMML(f"unparsed_omml_token:{self.current.value!r}")
        return value

    def parse_expression(self, minimum: int) -> Any:
        left = self.parse_atom()
        while self.current is not None:
            token = self.current
            if token.kind == "operator":
                precedence = _precedence(str(token.value))
                if precedence < minimum:
                    break
                self.index += 1
                right = self.parse_expression(precedence + 1)
                left = _combine(str(token.value), left, right)
                continue
            if token.kind == "term":
                if 35 < minimum:
                    break
                self.index += 1
                left = _implicit(left, token.value)
                continue
            if token.kind == "comma":
                break
            raise UnsupportedOMML(f"unexpected_omml_token:{token.value!r}")
        return left

    def parse_atom(self) -> Any:
        token = self.current
        if token is None:
            raise SemanticError("OMML expression ended after an operator")
        if token.kind == "operator" and token.value in {"+", "-"}:
            self.index += 1
            operand = self.parse_atom()
            return {"kind": "unary_plus" if token.value == "+" else "unary_minus", "operand": operand}
        if token.kind != "term":
            raise SemanticError(f"expected OMML term, got {token.value!r}")
        self.index += 1
        return token.value


def _parse_sequence(node: ET.Element, *, allow_comma: bool = False) -> Any:
    tokens = _collect(node, allow_comma=allow_comma)
    if any(token.kind == "comma" for token in tokens):
        raise UnsupportedOMML("comma_requires_delimiter_context")
    return _ExpressionParser(tokens).parse()


def _container_value(node: ET.Element, local: str) -> Any:
    child = _one(node, local)
    assert child is not None
    return _parse_sequence(child)


def _parse_fraction(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"fPr", "num", "den"})
    fraction_type = _one(node, "fPr", required=False)
    if fraction_type is not None:
        _ensure_children(fraction_type, {"type", "ctrlPr"})
        type_node = _one(fraction_type, "type", required=False)
        value = _attr(type_node, "val") if type_node is not None else None
        if value not in {None, "bar"}:
            raise UnsupportedOMML(f"unsupported_fraction_type:{value}")
    return {
        "kind": "fraction",
        "numerator": _container_value(node, "num"),
        "denominator": _container_value(node, "den"),
    }


def _parse_root(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"radPr", "deg", "e"})
    root_properties = _one(node, "radPr", required=False)
    if root_properties is not None:
        _ensure_children(root_properties, {"degHide", "ctrlPr"})
    hidden_node = _one(root_properties, "degHide", required=False) if root_properties is not None else None
    hidden_value = _attr(hidden_node, "val") if hidden_node is not None else None
    if hidden_value not in {None, "0", "1", "false", "true", "off", "on"}:
        raise UnsupportedOMML(f"unsupported_root_degree_visibility:{hidden_value}")
    degree = _one(node, "deg", required=False)
    index = None
    if degree is not None:
        meaningful = list(degree)
        if meaningful and hidden_value in {"1", "true", "on"}:
            raise UnsupportedOMML("visible_root_degree_marked_hidden")
        if meaningful:
            index = _parse_sequence(degree)
    elif hidden_value in {"0", "false", "off"}:
        raise SemanticError("root_degree_visibility_requires_degree")
    result: dict[str, Any] = {"kind": "root", "radicand": _container_value(node, "e")}
    if index is not None:
        result["index"] = index
    return result


def _parse_scripts(node: ET.Element, kind: str) -> dict[str, Any]:
    _ensure_children(node, {f"{kind}Pr", "e", "sub", "sup"})
    result: dict[str, Any] = {"kind": "script", "base": _container_value(node, "e")}
    if kind in {"sSub", "sSubSup"}:
        result["subscript"] = _container_value(node, "sub")
    if kind in {"sSup", "sSubSup"}:
        result["superscript"] = _container_value(node, "sup")
    if kind == "sSub" and isinstance(result.get("subscript"), dict) and result["subscript"].get("kind") == "script":
        return {"kind": "grouped_exponent", "exponent": result["subscript"]}
    return result


def _delimiter_char(properties: ET.Element | None, local: str) -> str | None:
    if properties is None:
        return None
    child = _one(properties, local, required=False)
    if child is None:
        return None
    return _attr(child, "val")


def _scalar(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("kind") == "identifier":
        return str(value.get("text"))
    return None


def _numeric_interval_semantics(
    left: str | None,
    right: str | None,
    lower: Any,
    upper: Any,
) -> dict[str, Any] | None:
    lower_text = _scalar(lower)
    upper_text = _scalar(upper)
    if (
        left not in {"(", "["}
        or right not in {")", "]"}
        or lower_text is None
        or upper_text is None
        or _NUMERIC_LITERAL_RE.fullmatch(lower_text) is None
        or _NUMERIC_LITERAL_RE.fullmatch(upper_text) is None
    ):
        return None
    return {
        "kind": "interval",
        "left": "open" if left == "(" else "closed",
        "right": "open" if right == ")" else "closed",
        "lower": lower_text,
        "upper": upper_text,
    }


def _parse_raw_run_interval(node: ET.Element) -> dict[str, Any] | None:
    """Parse only Pandoc's plain-run projection of a numeric interval."""

    if _local(node) != "oMath":
        return None
    runs = list(node)
    if not runs:
        return None
    text_parts: list[str] = []
    for run in runs:
        namespace, local = _tag(run)
        if namespace != M or local != "r":
            return None
        style, literal = _run_properties(run)
        if style is not None or literal:
            return None
        for text_node in _run_text_nodes(run):
            if text_node.text is None:
                return None
            text_parts.append(text_node.text)
    match = _RAW_NUMERIC_INTERVAL_RE.fullmatch("".join(text_parts))
    if match is None:
        return None
    return _numeric_interval_semantics(match.group(1), match.group(4), match.group(2), match.group(3))


def _parse_delimiter(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"dPr", "e"})
    properties = _one(node, "dPr", required=False)
    if properties is not None:
        _ensure_children(properties, {"begChr", "endChr", "sepChr", "grow", "shp", "ctrlPr"})
    left = _delimiter_char(properties, "begChr")
    right = _delimiter_char(properties, "endChr")
    separator = _delimiter_char(properties, "sepChr")
    if separator not in {None, ","}:
        raise UnsupportedOMML(f"unsupported_delimiter_separator:{separator}")
    if left not in {"(", "[", "{"} or right not in {")", "]", "}"}:
        raise UnsupportedOMML(f"unsupported_delimiter_pair:{left!r},{right!r}")
    expression_nodes = _direct(node, "e")
    if len(expression_nodes) != 1:
        raise UnsupportedOMML("multiple_delimiter_arguments")
    tokens = _collect(expression_nodes[0], allow_comma=True)
    comma_positions = [index for index, token in enumerate(tokens) if token.kind == "comma"]
    if len(comma_positions) == 1 and left in "([" and right in ")]":
        comma_index = comma_positions[0]
        if comma_index == 1 and comma_index + 1 == len(tokens) - 1:
            interval = _numeric_interval_semantics(left, right, tokens[0].value, tokens[-1].value)
            if interval is not None:
                return interval
    if comma_positions:
        raise UnsupportedOMML("comma_delimiter_body_not_supported")
    body = _ExpressionParser(tokens).parse()
    return {"kind": "delimited", "left": left, "right": right, "body": body}


def _accent_name(value: str | None) -> str:
    names = {
        "^": "hat",
        "\u0302": "hat",
        "\u005e": "hat",
        "\u00af": "bar",
        "\u0305": "bar",
        "\u2192": "vec",
        "\u20d7": "vec",
        "_": "underline",
        "\u0332": "underline",
        ".": "dot",
        "\u00a8": "ddot",
        "\u0308": "ddot",
    }
    if value not in names:
        raise UnsupportedOMML(f"unsupported_accent:{value!r}")
    return names[value]


def _parse_accent(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"accPr", "e"})
    properties = _one(node, "accPr", required=False)
    if properties is not None:
        _ensure_children(properties, {"chr", "ctrlPr"})
    char = _one(properties, "chr", required=False) if properties is not None else None
    return {"kind": "accent", "accent": _accent_name(_attr(char, "val") if char is not None else None), "base": _container_value(node, "e")}


def _parse_bar(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"barPr", "e"})
    properties = _one(node, "barPr", required=False)
    if properties is not None:
        _ensure_children(properties, {"pos", "ctrlPr"})
    position = _one(properties, "pos", required=False) if properties is not None else None
    value = _attr(position, "val") if position is not None else "top"
    if value not in {"top", "bot"}:
        raise UnsupportedOMML(f"unsupported_bar_position:{value}")
    return {"kind": "accent", "accent": "bar" if value == "top" else "underline", "base": _container_value(node, "e")}


def _parse_function(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"funcPr", "fName", "e"})
    name = _container_value(node, "fName")
    if isinstance(name, dict) and name.get("kind") == "identifier":
        name = name.get("text")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z]+", name):
        raise UnsupportedOMML("unsupported_function_name")
    if name not in _FUNCTION_NAMES:
        raise UnsupportedOMML(f"unsupported_function_name:{name}")
    return {"kind": "function_call", "name": name, "argument": _container_value(node, "e")}


def _parse_group_character(node: ET.Element) -> dict[str, Any]:
    _ensure_children(node, {"groupChrPr", "e"})
    properties = _one(node, "groupChrPr", required=False)
    if properties is not None:
        _ensure_children(properties, {"chr", "pos", "vertJc", "ctrlPr"})
    char = _one(properties, "chr", required=False) if properties is not None else None
    value = _attr(char, "val") if char is not None else None
    accent = _accent_name(value)
    return {"kind": "accent", "accent": accent, "base": _container_value(node, "e")}


def _parse_expression(node: ET.Element) -> Any:
    namespace, local = _tag(node)
    if namespace != M:
        raise UnsupportedOMML(f"foreign_omml_expression:{local}")
    if local == "r":
        tokens = _parse_run(node)
        if any(token.kind == "comma" for token in tokens):
            raise UnsupportedOMML("comma_outside_delimiter")
        return _ExpressionParser(tokens).parse()
    if local == "oMath":
        raw_interval = _parse_raw_run_interval(node)
        if raw_interval is not None:
            return raw_interval
        return _parse_sequence(node)
    if local in {"e", "num", "den", "sub", "sup", "deg", "fName"}:
        return _parse_sequence(node)
    if local == "f":
        return _parse_fraction(node)
    if local == "rad":
        return _parse_root(node)
    if local in {"sSub", "sSup", "sSubSup"}:
        return _parse_scripts(node, local)
    if local == "d":
        return _parse_delimiter(node)
    if local == "acc":
        return _parse_accent(node)
    if local == "bar":
        return _parse_bar(node)
    if local == "func":
        return _parse_function(node)
    if local == "groupChr":
        return _parse_group_character(node)
    raise UnsupportedOMML(f"unsupported_omml_structure:{local}")


def _coerce_root(value: ET.Element | bytes | str) -> ET.Element:
    if isinstance(value, ET.Element):
        return value
    if isinstance(value, bytes):
        try:
            return ET.fromstring(value)
        except ET.ParseError as error:
            raise SemanticError(f"invalid OMML XML: {error}") from error
    if isinstance(value, str):
        try:
            return ET.fromstring(value.encode("utf-8"))
        except ET.ParseError as error:
            raise SemanticError(f"invalid OMML XML: {error}") from error
    raise SemanticError("OMML input must be an Element, bytes, or XML string")


def parse_omml_semantics(value: ET.Element | bytes | str) -> Any:
    """Parse one ``m:oMath`` (or ``m:oMathPara``) into canonical semantics."""

    root = _coerce_root(value)
    namespace, local = _tag(root)
    if namespace != M:
        raise UnsupportedOMML(f"expected_math_namespace:{namespace}")
    if local == "oMathPara":
        equations = _direct(root, "oMath")
        if len(equations) != 1:
            raise SemanticError("m:oMathPara must contain exactly one m:oMath")
        root = equations[0]
        local = "oMath"
    if local not in {"oMath", "f", "rad", "sSub", "sSup", "sSubSup", "d", "acc", "bar", "func", "groupChr", "r"}:
        raise UnsupportedOMML(f"expected_equation_root:{local}")
    return copy.deepcopy(_parse_expression(root))


def _validate_canonical(value: Any, path: str = "canonical") -> None:
    if isinstance(value, str):
        if not value:
            raise UnsupportedOMML(f"empty_canonical_value:{path}")
        return
    if not isinstance(value, dict):
        raise UnsupportedOMML(f"unsupported_canonical_value:{path}")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in SUPPORTED_CANONICAL_KINDS:
        raise UnsupportedOMML(f"unsupported_canonical_kind:{kind!r}")
    fields = {
        "identifier": {"text"},
        "sequence": {"items"},
        "implicit_product": {"factors"},
        "script": {"base", "subscript", "superscript"},
        "grouped_exponent": {"exponent"},
        "fraction": {"numerator", "denominator"},
        "root": {"radicand", "index"},
        "delimited": {"left", "right", "body"},
        "interval": {"left", "right", "lower", "upper"},
        "relation": {"operator", "left", "right"},
        "addition": {"left", "right"},
        "subtraction": {"left", "right"},
        "binary_operator": {"operator", "left", "right"},
        "operator_sequence": {"operators", "scientific_exponent"},
        "unicode_operator_sequence": {"symbols"},
        "operator": {"value"},
        "function": {"name"},
        "function_call": {"name", "argument"},
        "styled": {"style", "value"},
        "roman": {"text"},
        "text": {"text"},
        "accent": {"accent", "base"},
        "unary_plus": {"operand"},
        "unary_minus": {"operand"},
    }.get(kind, set())
    unknown_fields = set(value) - (fields | {"kind"})
    if unknown_fields:
        raise UnsupportedOMML(f"unsupported_canonical_fields:{path}:{sorted(unknown_fields)}")
    required = {
        "identifier": {"text"},
        "sequence": {"items"},
        "implicit_product": {"factors"},
        "script": {"base"},
        "grouped_exponent": {"exponent"},
        "fraction": {"numerator", "denominator"},
        "root": {"radicand"},
        "delimited": {"left", "right", "body"},
        "interval": {"left", "right", "lower", "upper"},
        "relation": {"operator", "left", "right"},
        "addition": {"left", "right"},
        "subtraction": {"left", "right"},
        "binary_operator": {"operator", "left", "right"},
        "operator_sequence": {"operators", "scientific_exponent"},
        "unicode_operator_sequence": {"symbols"},
        "operator": {"value"},
        "function": {"name"},
        "function_call": {"name", "argument"},
        "styled": {"style", "value"},
        "roman": {"text"},
        "text": {"text"},
        "accent": {"accent", "base"},
        "unary_plus": {"operand"},
        "unary_minus": {"operand"},
    }.get(kind, set())
    missing = sorted(required - set(value))
    if missing:
        raise UnsupportedOMML(f"canonical_missing_fields:{path}:{missing}")
    if kind in {"sequence", "implicit_product"}:
        items = value["items"] if kind == "sequence" else value["factors"]
        if not isinstance(items, list) or not items:
            raise UnsupportedOMML(f"canonical_{kind}_must_be_nonempty:{path}")
    if kind == "script" and "subscript" not in value and "superscript" not in value:
        raise UnsupportedOMML(f"canonical_script_has_no_script:{path}")
    if kind == "operator_sequence":
        operators = value["operators"]
        if not isinstance(operators, list) or not operators or not all(isinstance(item, str) for item in operators):
            raise UnsupportedOMML(f"canonical_operator_sequence_has_invalid_operators:{path}")
        if value["scientific_exponent"] is not None and not isinstance(value["scientific_exponent"], str):
            raise UnsupportedOMML(f"canonical_operator_sequence_has_invalid_exponent:{path}")
    if kind == "unicode_operator_sequence":
        symbols = value["symbols"]
        if not isinstance(symbols, list) or not symbols or not all(isinstance(item, str) for item in symbols):
            raise UnsupportedOMML(f"canonical_unicode_operator_sequence_has_invalid_symbols:{path}")
    if kind == "interval":
        if value["left"] not in {"open", "closed"} or value["right"] not in {"open", "closed"}:
            raise UnsupportedOMML(f"canonical_interval_has_invalid_boundary:{path}")
        if not all(isinstance(value[field], str) and value[field] for field in ("lower", "upper")):
            raise UnsupportedOMML(f"canonical_interval_has_invalid_bounds:{path}")
    if kind == "styled" and value["style"] not in {"bold", "italic", "calligraphic", "blackboard"}:
        raise UnsupportedOMML(f"canonical_styled_value_is_unsupported:{path}")
    if kind == "accent" and value["accent"] not in {"hat", "bar", "vec", "underline", "dot", "ddot"}:
        raise UnsupportedOMML(f"canonical_accent_value_is_unsupported:{path}")
    for key, child in value.items():
        if key != "kind":
            if isinstance(child, (dict, str)):
                _validate_canonical(child, f"{path}.{key}")
            elif isinstance(child, list):
                for index, item in enumerate(child):
                    if isinstance(item, (dict, str)):
                        _validate_canonical(item, f"{path}.{key}[{index}]")
                    elif not isinstance(item, (int, float)):
                        raise UnsupportedOMML(f"unsupported_canonical_field:{path}.{key}[{index}]")
            elif child is not None and not isinstance(child, (bool, int, float)):
                raise UnsupportedOMML(f"unsupported_canonical_field:{path}.{key}")


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "kind":
                yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _operator_signature(value: Any) -> list[str]:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind == "relation":
            return _operator_signature(value["left"]) + [str(value["operator"])] + _operator_signature(value["right"])
        if kind in {"binary_operator", "addition", "subtraction"}:
            operator = value.get("operator", "+" if kind == "addition" else "-")
            return _operator_signature(value["left"]) + [str(operator)] + _operator_signature(value["right"])
        if kind == "sequence":
            result: list[str] = []
            for item in value["items"]:
                result.extend(_operator_signature(item))
            return result
        if kind == "implicit_product":
            result = []
            for item in value["factors"]:
                result.extend(_operator_signature(item))
            return result
    return []


def _scientific_exponent(value: Any) -> str | None:
    for item in _walk(value):
        if not isinstance(item, dict) or item.get("kind") != "script":
            continue
        base = item.get("base")
        if not isinstance(base, str) or not re.fullmatch(r"\d+(?:\.\d+)?", base):
            continue
        exponent = item.get("superscript")
        if isinstance(exponent, str):
            return exponent
        if isinstance(exponent, dict) and exponent.get("kind") == "unary_minus" and isinstance(exponent.get("operand"), str):
            return "-" + exponent["operand"]
    return None


def _symbol_signature(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []
    kind = value.get("kind")
    if kind == "identifier":
        return [str(value.get("text"))]
    if kind == "relation":
        return _symbol_signature(value["left"]) + [str(value["operator"])] + _symbol_signature(value["right"])
    if kind in {"binary_operator", "addition", "subtraction"}:
        operator = value.get("operator", "+" if kind == "addition" else "-")
        return _symbol_signature(value["left"]) + [str(operator)] + _symbol_signature(value["right"])
    if kind == "sequence":
        result: list[str] = []
        for item in value["items"]:
            result.extend(_symbol_signature(item))
        return result
    if kind == "implicit_product":
        result = []
        for item in value["factors"]:
            result.extend(_symbol_signature(item))
        return result
    return []


def _lossless_expected(expected: Any, source_latex: str | None) -> Any | None:
    """Expand W3A's compact sequence records when their source is available.

    W3A's operator sequence records intentionally retain only the operator
    shape and scientific exponent.  That is enough for the recovery stage's
    distinction checks, but a generator gate also needs to compare operands.
    Wrapping the normalized LaTeX in a group bypasses the compact recovery
    shortcut while reusing the W3A parser itself.
    """

    if not isinstance(expected, dict) or expected.get("kind") not in {
        "operator_sequence",
        "unicode_operator_sequence",
    }:
        return expected
    if not isinstance(source_latex, str) or not source_latex.strip():
        return None
    source = source_latex
    for character, name in _UNICODE_ATOMS.items():
        source = source.replace(character, f"\\{name}")
    for character, symbol in _UNICODE_OPERATORS.items():
        command = {
            "<=": r"\le",
            ">=": r"\ge",
            "!=": r"\ne",
            "~": r"\approx",
            "in": r"\in",
            "notin": r"\notin",
            "+/-": r"\pm",
            "-/+": r"\mp",
            "*": r"\times",
            "/": r"/",
            "-": "-",
        }[symbol]
        source = source.replace(character, command)
    try:
        expanded = canonicalize_formula("{" + source + "}")
    except (CanonicalError, TypeError):
        return None
    if isinstance(expanded, dict) and expanded.get("kind") == "delimited":
        expanded = expanded.get("body")
    if expected.get("kind") == "operator_sequence":
        if (
            _operator_signature(expanded) != list(expected["operators"])
            or _scientific_exponent(expanded) != expected["scientific_exponent"]
        ):
            return None
    else:
        symbols = {
            "le": "<=",
            "ge": ">=",
            "ne": "!=",
            "approx": "~",
            "pm": "+/-",
            "mp": "-/+",
            "times": "*",
            "cdot": "*",
        }
        expected_symbols = [symbols.get(str(item), str(item)) for item in expected["symbols"]]
        if _symbol_signature(expanded) != expected_symbols:
            return None
    return expanded


def compare_omml_to_canonical(
    omml: ET.Element | bytes | str,
    expected: Any,
    *,
    source_latex: str | None = None,
) -> SemanticResult:
    """Compare one OMML equation with an approved W3A canonical value."""

    try:
        _validate_canonical(expected)
    except UnsupportedOMML as error:
        return SemanticResult(
            SemanticStatus.UNSUPPORTED.value,
            copy.deepcopy(expected),
            None,
            None,
            str(error),
            (str(error),),
        )
    family = expected.get("kind") if isinstance(expected, dict) else "atom"
    comparison_expected = _lossless_expected(expected, source_latex)
    if comparison_expected is None:
        reason = "canonical_source_missing_or_conflicts_with_compact_semantics"
        return SemanticResult(
            SemanticStatus.UNSUPPORTED.value,
            copy.deepcopy(expected),
            None,
            family,
            reason,
            (reason,),
        )
    try:
        actual = parse_omml_semantics(omml)
    except UnsupportedOMML as error:
        return SemanticResult(
            SemanticStatus.UNSUPPORTED.value,
            copy.deepcopy(expected),
            None,
            family,
            str(error),
            (str(error),),
        )
    except SemanticError as error:
        return SemanticResult(
            SemanticStatus.INVALID.value,
            copy.deepcopy(expected),
            None,
            family,
            str(error),
            (str(error),),
        )
    if canonical_equal(actual, comparison_expected):
        return SemanticResult(
            SemanticStatus.PASS.value,
            copy.deepcopy(expected),
            actual,
            family,
            "semantic_equivalence",
        )
    return SemanticResult(
        SemanticStatus.MISMATCH.value,
        copy.deepcopy(expected),
        actual,
        family,
        "canonical_semantics_mismatch",
        ("canonical_semantics_mismatch",),
    )


def semantic_fingerprint(value: Any) -> str:
    """Return a stable digest for a canonical semantic value or result."""

    if isinstance(value, SemanticResult):
        value = value.to_dict()
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SemanticError(f"semantic value is not deterministic JSON: {error}") from error
    return hashlib.sha256(encoded).hexdigest()


# Short names keep downstream imports stable without creating another parser.
parse_omml = parse_omml_semantics
compare_omml = compare_omml_to_canonical
validate_omml_semantics = compare_omml_to_canonical


__all__ = [
    "M",
    "SEMANTIC_SUPPORT_MATRIX",
    "SUPPORTED_CANONICAL_KINDS",
    "SemanticError",
    "SemanticResult",
    "SemanticStatus",
    "UnsupportedOMML",
    "compare_omml",
    "compare_omml_to_canonical",
    "parse_omml",
    "parse_omml_semantics",
    "semantic_fingerprint",
    "validate_omml_semantics",
]
