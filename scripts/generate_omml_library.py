#!/usr/bin/env python3
"""Generate a DOCX containing labeled native OMML equations via Pandoc."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.canonical import CanonicalError, canonicalize_formula
from word_formula_omml.contract import load_manifest
from word_formula_omml.semantic import compare_omml_to_canonical


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W, "m": M}
MARKER_PREFIX = "OMML_ID:"


class GenerationError(RuntimeError):
    """Raised when generation or its semantic gate cannot complete safely."""


class PandocError(GenerationError):
    """Raised with actionable diagnostics for a Pandoc failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON array or object with a formulas array")
    parser.add_argument("output", type=Path, help="Output DOCX library")
    parser.add_argument("--index", type=Path, help="Sidecar JSON path")
    parser.add_argument("--pandoc", default="pandoc", help="Pandoc executable")
    return parser.parse_args()


def load_formulas(path: Path, *, include_semantics: bool = False) -> list[dict[str, object]]:
    manifest = load_manifest(path)
    rows = manifest.formulas
    if not rows:
        raise GenerationError("manifest must contain a non-empty formulas array")
    formulas: list[dict[str, object]] = []
    for row in rows:
        formula = {"id": row["id"], "latex": row["latex"], "layout": row["layout"]}
        if include_semantics:
            source = row.get("normalized_latex", row["latex"])
            canonical = row.get("canonical")
            if canonical is None:
                try:
                    canonical = canonicalize_formula(source, source_type=row.get("source_type"))
                except CanonicalError as error:
                    raise GenerationError(
                        f"formula {row['id']!r} has no supported canonical semantics: {error}"
                    ) from error
            formula["canonical"] = canonical
        formulas.append(formula)
    return formulas


def pandoc_api_version(executable: str) -> list[int]:
    try:
        result = subprocess.run(
            [executable, "--from=markdown", "--to=json"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise PandocError(f"Pandoc preflight failed for {executable!r}: {error}") from error
    diagnostics = _diagnostics(result)
    if result.returncode != 0:
        raise PandocError(f"Pandoc preflight exited {result.returncode}: {diagnostics}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise PandocError(f"Pandoc preflight returned invalid JSON: {error}; {diagnostics}") from error
    version = payload.get("pandoc-api-version") if isinstance(payload, dict) else None
    if not isinstance(version, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in version):
        raise PandocError(f"Pandoc preflight JSON has no valid pandoc-api-version: {diagnostics}")
    return version


def _diagnostics(result: subprocess.CompletedProcess[bytes]) -> str:
    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    parts = []
    if stderr:
        parts.append(f"stderr: {stderr}")
    if stdout:
        parts.append(f"stdout: {stdout}")
    return " | ".join(parts) or "no diagnostics"


def build_ast(formulas: list[dict[str, object]], api_version: list[int]) -> dict:
    blocks = []
    for row in formulas:
        blocks.append({"t": "Para", "c": [{"t": "Str", "c": MARKER_PREFIX + row["id"]}]})
        math_type = "DisplayMath" if row["layout"] == "display" else "InlineMath"
        blocks.append(
            {
                "t": "Para",
                "c": [{"t": "Math", "c": [{"t": math_type}, row["latex"]]}],
            }
        )
    return {"pandoc-api-version": api_version, "meta": {}, "blocks": blocks}


def inspect_library(
    path: Path,
    formulas: list[dict[str, object]],
    *,
    pandoc_diagnostics: list[str] | None = None,
) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("generated DOCX failed ZIP CRC validation")
        root = ET.fromstring(archive.read("word/document.xml"))

    paragraphs = root.findall(".//w:body//w:p", NS)
    expected = {MARKER_PREFIX + row["id"]: row for row in formulas}
    entries = []
    for index, paragraph in enumerate(paragraphs, 1):
        marker = "".join(paragraph.itertext()).strip()
        if marker not in expected:
            continue
        following = paragraphs[index] if index < len(paragraphs) else None
        if following is None:
            raise RuntimeError(f"missing equation after {marker}")
        equations = following.findall(".//m:oMath", NS)
        if len(equations) != 1:
            raise RuntimeError(f"{marker} generated {len(equations)} equations, expected 1")
        xml = ET.tostring(equations[0], encoding="utf-8")
        row = expected[marker]
        canonical = row.get("canonical")
        if canonical is None:
            try:
                canonical = canonicalize_formula(row["latex"])
            except CanonicalError as error:
                raise GenerationError(f"formula {row['id']!r} has no supported canonical semantics: {error}") from error
        semantic = compare_omml_to_canonical(
            equations[0],
            canonical,
            source_latex=str(row["latex"]),
        )
        diagnostics = list(pandoc_diagnostics or [])
        semantic_pass = semantic.passed and not diagnostics
        if diagnostics:
            entry_status = "GENERATED_WITH_DIAGNOSTICS"
        elif semantic.status == "PASS":
            entry_status = "SEMANTICALLY_VALIDATED"
        else:
            entry_status = "SEMANTIC_VALIDATION_FAILED"
        entries.append(
            {
                **row,
                "marker_paragraph": index,
                "equation_paragraph": index + 1,
                "omml_sha256": hashlib.sha256(xml).hexdigest(),
                "semantic": semantic.to_dict(),
                "generation": {
                    "status": "SUCCESS" if not diagnostics else "SUCCESS_WITH_DIAGNOSTICS",
                    "diagnostics": diagnostics,
                },
                "status": entry_status,
                "auto_eligible": semantic_pass,
            }
        )

    if len(entries) != len(formulas):
        found = {entry["id"] for entry in entries}
        missing = [row["id"] for row in formulas if row["id"] not in found]
        raise RuntimeError(f"missing generated equations: {missing}")
    return entries


def main() -> int:
    args = parse_args()
    formulas = load_formulas(args.manifest, include_semantics=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    index_path = args.index or args.output.with_suffix(".index.json")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    ast = build_ast(formulas, pandoc_api_version(args.pandoc))
    temporary_output: str | None = None
    temporary_index: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=args.output.parent,
            prefix=f".{args.output.name}.",
            suffix=".staging.docx",
            delete=False,
        ) as stream:
            temporary_output = stream.name
        try:
            result = subprocess.run(
                [args.pandoc, "--from=json", "--to=docx", f"--output={temporary_output}"],
                input=json.dumps(ast, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise PandocError(f"Pandoc generation failed for {args.pandoc!r}: {error}") from error
        diagnostics_text = _diagnostics(result)
        if result.returncode != 0:
            raise PandocError(f"Pandoc generation exited {result.returncode}: {diagnostics_text}")
        diagnostics = [] if diagnostics_text == "no diagnostics" else [diagnostics_text]
        entries = inspect_library(Path(temporary_output), formulas, pandoc_diagnostics=diagnostics)
        failures = [
            entry
            for entry in entries
            if not entry["auto_eligible"]
        ]
        if failures:
            details = "; ".join(
                f"{entry['id']}: {entry['status']} ({entry['semantic']['reason']})"
                for entry in failures
            )
            raise GenerationError(
                "semantic validation did not produce an automatic template; "
                f"review required: {details}"
            )
        with tempfile.NamedTemporaryFile(
            dir=index_path.parent,
            prefix=f".{index_path.name}.",
            suffix=".staging.json",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as stream:
            temporary_index = stream.name
            json.dump(
                {
                    "schema_version": 1,
                    "library": str(args.output),
                    "formulas": entries,
                },
                stream,
                indent=2,
                ensure_ascii=False,
            )
            stream.write("\n")
        os.replace(temporary_output, args.output)
        temporary_output = None
        os.replace(temporary_index, index_path)
        temporary_index = None
        print(f"generated={len(entries)} library={args.output} index={index_path}")
        return 0
    finally:
        for path in (temporary_output, temporary_index):
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, GenerationError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
