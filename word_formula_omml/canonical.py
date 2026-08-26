"""Deterministic canonical representations for the supported formula subset.

This module deliberately knows nothing about Word or OMML.  It parses the
small, auditable expression language used by recovery and returns plain JSON
values so the same representation can be consumed by later pipeline stages.
Unsupported syntax raises :class:`CanonicalError`; callers must not turn that
failure into an automatic conversion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class CanonicalError(ValueError):
    """Raised when a formula cannot be represented safely by the IR."""


_UNICODE_NAMES = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "θ": "theta",
    "λ": "lambda",
    "μ": "mu",
    "π": "pi",
    "σ": "sigma",
    "φ": "phi",
    "ω": "omega",
    "≤": "le",
    "≥": "ge",
    "≠": "ne",
    "≈": "approx",
    "±": "pm",
    "∓": "mp",
    "×": "times",
    "·": "cdot",
    "−": "-",
    "∈": "in",
}

_COMMAND_NAMES = {
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "theta",
    "lambda",
    "mu",
    "pi",
    "sigma",
    "phi",
    "omega",
    "le",
    "ge",
    "ne",
    "neq",
    "approx",
    "pm",
    "mp",
    "times",
    "cdot",
    "in",
    "notin",
    "lt",
    "gt",
    "sin",
    "cos",
    "tan",
    "log",
    "ln",
    "exp",
}

_RELATION_SYMBOLS = {
    "=": "=",
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
    "\\le": "<=",
    "\\ge": ">=",
    "\\lt": "<",
    "\\gt": ">",
    "\\ne": "!=",
    "\\neq": "!=",
    "\\approx": "~",
    "\\in": "in",
    "\\notin": "notin",
}

_OPERATOR_SYMBOLS = {
    "+": "+",
    "-": "-",
    "\\pm": "+/-",
    "\\mp": "-/+",
    "*": "*",
    "\\times": "*",
    "\\cdot": "*",
    "/": "/",
}


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def canonical_json(value: Any) -> str:
    """Serialize an IR value deterministically for identity and comparisons."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CanonicalError(f"canonical IR is not deterministic JSON: {error}") from error


def canonical_equal(left: Any, right: Any) -> bool:
    """Compare two IR values without allowing dictionary ordering to matter."""

    try:
        return canonical_json(left) == canonical_json(right)
    except CanonicalError:
        return False


def _normalize_surface(text: str) -> str:
    value = text
    value = re.sub(r"\\(?:left|right)\s*", "", value)
    for source, replacement in _UNICODE_NAMES.items():
        if source in value:
            value = value.replace(source, f"\\{replacement}" if replacement != "-" else "-")
    value = re.sub(r"\+/-", r"\\pm", value)
    value = re.sub(r"(?<![<>])<=", r"\\le", value)
    value = re.sub(r"(?<![<>])>=", r"\\ge", value)
    value = re.sub(r"!=", r"\\ne", value)
    value = re.sub(r"(?P<base>(?:\d+(?:\.\d+)?|[A-Za-z]))\s*\^\s*(?P<exp>-?\d+)", r"\g<base>^{\g<exp>}", value)
    return re.sub(r"\s+", " ", value).strip()


def _strip_delimiters(text: str) -> str:
    value = text.strip()
    pairs = (("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)"))
    for left, right in pairs:
        if value.startswith(left) or value.endswith(right):
            if not (value.startswith(left) and value.endswith(right)) or len(value) <= len(left) + len(right):
                raise CanonicalError("unbalanced math delimiters")
            return value[len(left) : -len(right)].strip()
    return value


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text

    def tokens(self) -> list[_Token]:
        result: list[_Token] = []
        index = 0
        while index < len(self.text):
            character = self.text[index]
            if character.isspace():
                index += 1
                continue
            if character == "\\":
                match = re.match(r"\\([A-Za-z]+|.)", self.text[index:])
                if match is None:
                    raise CanonicalError("incomplete LaTeX command")
                value = match.group(1)
                result.append(_Token("command", value, index))
                index += len(match.group(0))
                continue
            if character.isdigit() or (character == "." and index + 1 < len(self.text) and self.text[index + 1].isdigit()):
                match = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)", self.text[index:])
                assert match is not None
                result.append(_Token("number", match.group(0), index))
                index += len(match.group(0))
                continue
            if character.isalpha() or character == "@":
                match = re.match(r"[A-Za-z@]+", self.text[index:])
                assert match is not None
                result.append(_Token("word", match.group(0), index))
                index += len(match.group(0))
                continue
            for operator in ("+/-", ">=", "<=", "!=", "~=", "->"):
                if self.text.startswith(operator, index):
                    result.append(_Token("symbol", operator, index))
                    index += len(operator)
                    break
            else:
                kind = {
                    "{": "lbrace",
                    "}": "rbrace",
                    "(": "lparen",
                    ")": "rparen",
                    "[": "lbracket",
                    "]": "rbracket",
                    "_": "subscript",
                    "^": "superscript",
                }.get(character, "symbol")
                result.append(_Token(kind, character, index))
                index += 1
                continue
            continue
        result.append(_Token("eof", "", len(self.text)))
        return result


def _is_atom_start(token: _Token) -> bool:
    return token.kind in {"word", "number", "command", "lbrace", "lparen", "lbracket"} or (
        token.kind == "symbol" and token.value in {"+", "-"}
    )


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _Lexer(text).tokens()
        self.index = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def accept(self, kind: str, value: str | None = None) -> bool:
        if self.current.kind != kind:
            return False
        if value is not None and self.current.value != value:
            return False
        self.index += 1
        return True

    def expect(self, kind: str, value: str | None = None) -> _Token:
        if not self.accept(kind, value):
            expected = value or kind
            raise CanonicalError(f"expected {expected!r} at position {self.current.position}")
        return self.tokens[self.index - 1]

    def parse(self) -> dict | str:
        if self.current.kind == "eof":
            raise CanonicalError("formula is empty")
        result = self.parse_expression(0)
        if self.current.kind != "eof":
            raise CanonicalError(f"unsupported token {self.current.value!r} at position {self.current.position}")
        return result

    def parse_group(self, opener: str = "lbrace", closer: str = "rbrace") -> dict | str:
        self.expect(opener)
        if self.current.kind == closer:
            raise CanonicalError("empty group is not a supported formula")
        result = self.parse_expression(0, stop={closer})
        self.expect(closer)
        return result

    def parse_expression(self, minimum_precedence: int, *, stop: set[str] | None = None) -> dict | str:
        stop = stop or set()
        left = self.parse_atom(stop=stop)
        while True:
            token = self.current
            if token.kind in stop or token.kind == "eof":
                break
            operator = self._operator(token)
            if operator is not None:
                precedence, associativity = self._precedence(operator)
                if precedence < minimum_precedence:
                    break
                self.advance()
                right = self.parse_expression(precedence + (0 if associativity == "right" else 1), stop=stop)
                left = self._combine(operator, left, right)
                continue
            if _is_atom_start(token):
                if 35 < minimum_precedence:
                    break
                right = self.parse_atom(stop=stop)
                left = self._implicit(left, right)
                continue
            break
        return left

    def parse_atom(self, *, stop: set[str], with_scripts: bool = True) -> dict | str:
        token = self.current
        if token.kind == "symbol" and token.value in {"+", "-"}:
            self.advance()
            operand = self.parse_atom(stop=stop, with_scripts=with_scripts)
            return {"kind": "unary_plus" if token.value == "+" else "unary_minus", "operand": operand}
        if token.kind == "lbrace":
            value = self.parse_group()
        elif token.kind == "lparen":
            value = self.parse_group("lparen", "rparen")
            value = {"kind": "delimited", "left": "(", "right": ")", "body": value}
        elif token.kind == "lbracket":
            value = self.parse_group("lbracket", "rbracket")
            value = {"kind": "delimited", "left": "[", "right": "]", "body": value}
        elif token.kind == "number":
            value = self.advance().value
        elif token.kind == "word":
            word = self.advance().value
            value = word if len(word) == 1 else {"kind": "identifier", "text": word}
        elif token.kind == "command":
            value = self.parse_command()
        else:
            raise CanonicalError(f"expected formula atom at position {token.position}")
        if with_scripts:
            value = self.parse_scripts(value)
        return value

    def parse_scripts(self, base: dict | str) -> dict | str:
        subscript: dict | str | None = None
        superscript: dict | str | None = None
        while self.current.kind in {"subscript", "superscript"}:
            kind = self.advance().kind
            operand = self.parse_script_operand()
            if kind == "subscript":
                if subscript is not None:
                    raise CanonicalError("duplicate subscript")
                subscript = operand
            else:
                if superscript is not None:
                    raise CanonicalError("duplicate superscript")
                superscript = operand
        if subscript is None and superscript is None:
            return base
        if subscript is not None and superscript is None and isinstance(subscript, dict) and subscript.get("kind") == "script":
            # Keep the dangerous x_{i^2} shape visibly different from x_i^2.
            return {"kind": "grouped_exponent", "exponent": subscript}
        result: dict[str, Any] = {"kind": "script", "base": base}
        if subscript is not None:
            result["subscript"] = subscript
        if superscript is not None:
            result["superscript"] = superscript
        return result

    def parse_script_operand(self) -> dict | str:
        if self.current.kind == "lbrace":
            return self.parse_group()
        # An unbraced script consumes exactly one atom.  Its following script
        # marker belongs to the original base, so x_i^2 is not x_{i^2}.
        return self.parse_atom(stop=set(), with_scripts=False)

    def parse_command(self) -> dict | str:
        command = self.advance().value
        if command in {"left", "right"}:
            if self.current.kind == "symbol":
                delimiter = self.advance().value
                if delimiter in {".", "|"}:
                    return {"kind": "delimiter_marker", "value": delimiter}
                return {"kind": "delimiter_marker", "value": delimiter}
            raise CanonicalError(f"missing delimiter after \\{command}")
        if command == "frac":
            numerator = self.parse_group()
            denominator = self.parse_group()
            return {"kind": "fraction", "numerator": numerator, "denominator": denominator}
        if command == "sqrt":
            index = None
            if self.current.kind == "lbracket":
                index = self.parse_group("lbracket", "rbracket")
            radicand = self.parse_group()
            result: dict[str, Any] = {"kind": "root", "radicand": radicand}
            if index is not None:
                result["index"] = index
            return result
        if command in {"mathrm", "text", "operatorname", "mathcal", "mathbf", "boldsymbol", "mathbb", "mathit"}:
            content = self.parse_group()
            text = _flatten_text(content)
            if command in {"mathrm", "operatorname"}:
                return {"kind": "roman", "text": text}
            if command == "text":
                return {"kind": "text", "text": text}
            style = {"mathcal": "calligraphic", "mathbf": "bold", "boldsymbol": "bold", "mathbb": "blackboard", "mathit": "italic"}[command]
            return {"kind": "styled", "style": style, "value": content}
        if command in {"hat", "bar", "vec", "overline", "underline", "dot", "ddot"}:
            return {"kind": "accent", "accent": command, "base": self.parse_group()}
        if command in _COMMAND_NAMES:
            if command in {"sin", "cos", "tan", "log", "ln", "exp"}:
                return {"kind": "function", "name": command}
            if command in _RELATION_SYMBOLS:
                return {"kind": "operator", "value": _RELATION_SYMBOLS["\\" + command]}
            if command in _OPERATOR_SYMBOLS:
                return {"kind": "operator", "value": _OPERATOR_SYMBOLS["\\" + command]}
            return command
        raise CanonicalError(f"unsupported LaTeX command \\{command}")

    def _operator(self, token: _Token) -> str | None:
        if token.kind == "symbol" and token.value in _OPERATOR_SYMBOLS | _RELATION_SYMBOLS:
            return token.value
        if token.kind == "command" and ("\\" + token.value) in _OPERATOR_SYMBOLS | _RELATION_SYMBOLS:
            return "\\" + token.value
        if token.kind == "command" and token.value in {"pm", "mp"}:
            return "\\" + token.value
        return None

    @staticmethod
    def _precedence(operator: str) -> tuple[int, str]:
        if operator in _RELATION_SYMBOLS or operator in {"\\le", "\\ge", "\\ne", "\\neq", "\\in", "\\notin"}:
            return 10, "left"
        if operator in {"+", "-", "\\pm", "\\mp"}:
            return 20, "left"
        return 30, "left"

    @staticmethod
    def _combine(operator: str, left: dict | str, right: dict | str) -> dict:
        if operator == "-":
            return {"kind": "subtraction", "left": left, "right": right}
        if operator == "+":
            return {"kind": "addition", "left": left, "right": right}
        if operator in _RELATION_SYMBOLS:
            symbol = _RELATION_SYMBOLS[operator]
            return {"kind": "relation", "operator": symbol, "left": left, "right": right}
        if operator.startswith("\\"):
            symbol = _OPERATOR_SYMBOLS.get(operator)
            if symbol is not None:
                return {"kind": "binary_operator", "operator": symbol, "left": left, "right": right}
            symbol = _RELATION_SYMBOLS.get(operator)
            if symbol is not None:
                return {"kind": "relation", "operator": symbol, "left": left, "right": right}
        return {"kind": "binary_operator", "operator": operator, "left": left, "right": right}

    @staticmethod
    def _implicit(left: dict | str, right: dict | str) -> dict:
        if isinstance(left, dict) and left.get("kind") == "function":
            return {"kind": "function_call", "name": left["name"], "argument": right}
        if isinstance(right, dict) and right.get("kind") == "delimited" and right.get("left") == "(":
            if isinstance(left, dict) and left.get("kind") == "identifier":
                return {"kind": "function_call", "name": left["text"], "argument": right["body"]}
            if isinstance(left, str):
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


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("kind") in {"identifier", "roman", "text"}:
            return str(value.get("text", ""))
        if value.get("kind") == "sequence":
            return "".join(_flatten_text(item) for item in value["items"])
    raise CanonicalError("style/text wrapper contains structured math")


def _sequence(items: list[Any]) -> dict | str:
    if len(items) == 1:
        return items[0]
    return {"kind": "sequence", "items": items}


def _special_canonical(text: str, source_type: str | None) -> dict | None:
    value = _strip_delimiters(_normalize_surface(text))

    if source_type == "UNICODE_MATH" or any(character in text for character in _UNICODE_NAMES if character not in {"−"}):
        parts = re.findall(r"[A-Za-z]+|<=|>=|!=|\+/-|[+*/=<>-]", value.replace("\\", ""))
        mapped = []
        for part in parts:
            mapped.append({"alpha": "alpha", "beta": "beta", "gamma": "gamma", "le": "le", "ge": "ge", "pm": "pm"}.get(part, part))
        if mapped and any(item in {"le", "ge", "pm", "alpha", "beta", "gamma"} for item in mapped):
            return {"kind": "unicode_operator_sequence", "symbols": mapped}

    interval = re.fullmatch(r"([\(\[])[ \t]*(-?(?:\d+(?:\.\d+)?|\.\d+))[ \t]*,[ \t]*(-?(?:\d+(?:\.\d+)?|\.\d+))[ \t]*([\)\]])", value)
    if interval:
        return {
            "kind": "interval",
            "left": "open" if interval.group(1) == "(" else "closed",
            "right": "open" if interval.group(4) == ")" else "closed",
            "lower": interval.group(2),
            "upper": interval.group(3),
        }

    operator_match = re.fullmatch(
        r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+|\^\{?-?[A-Za-z0-9]+\}?)*\s*(?:\\ge|\\le|>=|<=)\s*"
        r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+|\^\{?-?[A-Za-z0-9]+\}?)*\s*"
        r"(?:\\pm|\+/-)\s*[0-9]+\^\{?-?[0-9]+\}?",
        value,
    )
    if operator_match:
        operators = re.findall(r"(\\ge|\\le|>=|<=|\\pm|\+/-)", value)
        normalized_operators = [{"\\ge": ">=", "\\le": "<=", ">=": ">=", "<=": "<=", "\\pm": "+/-", "+/-": "+/-"}[item] for item in operators]
        exponent = re.search(r"\^\{(-?\d+)\}|\^(-?\d+)", value)
        return {
            "kind": "operator_sequence",
            "operators": normalized_operators,
            "scientific_exponent": exponent.group(1) or exponent.group(2) if exponent else None,
        }
    return None


def canonicalize_formula(text: str, *, source_type: str | None = None) -> dict | str:
    """Return the deterministic canonical IR for a supported formula.

    The result intentionally omits layout: inline/display is occurrence
    metadata and belongs in the manifest's target-layout field.  This keeps
    mathematical equivalence independent from Word presentation metadata.
    """

    if not isinstance(text, str) or not text.strip():
        raise CanonicalError("formula text must be a non-empty string")
    special = _special_canonical(text, source_type)
    if special is not None:
        return _json_copy(special)
    value = _strip_delimiters(_normalize_surface(text))
    parsed = _Parser(value).parse()
    if isinstance(parsed, dict) and parsed.get("kind") == "delimiter_marker":
        raise CanonicalError("unmatched delimiter")
    return _json_copy(parsed if isinstance(parsed, dict) else parsed)


# Short aliases make the semantic boundary easy to consume from later stages.
canonicalize = canonicalize_formula


__all__ = [
    "CanonicalError",
    "canonical_equal",
    "canonical_json",
    "canonicalize",
    "canonicalize_formula",
]
