#!/usr/bin/env python3
"""Generate a DOCX containing labeled native OMML equations via Pandoc."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.contract import load_manifest


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W, "m": M}
MARKER_PREFIX = "OMML_ID:"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON array or object with a formulas array")
    parser.add_argument("output", type=Path, help="Output DOCX library")
    parser.add_argument("--index", type=Path, help="Sidecar JSON path")
    parser.add_argument("--pandoc", default="pandoc", help="Pandoc executable")
    return parser.parse_args()


def load_formulas(path: Path) -> list[dict[str, str]]:
    manifest = load_manifest(path)
    rows = manifest.formulas
    if not rows:
        raise ValueError("manifest must contain a non-empty formulas array")
    return [
        {"id": row["id"], "latex": row["latex"], "layout": row["layout"]}
        for row in rows
    ]


def pandoc_api_version(executable: str) -> list[int]:
    result = subprocess.run(
        [executable, "--from=markdown", "--to=json"],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)["pandoc-api-version"]


def build_ast(formulas: list[dict[str, str]], api_version: list[int]) -> dict:
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


def inspect_library(path: Path, formulas: list[dict[str, str]]) -> list[dict]:
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
        entries.append(
            {
                **row,
                "marker_paragraph": index,
                "equation_paragraph": index + 1,
                "omml_sha256": hashlib.sha256(xml).hexdigest(),
            }
        )

    if len(entries) != len(formulas):
        found = {entry["id"] for entry in entries}
        missing = [row["id"] for row in formulas if row["id"] not in found]
        raise RuntimeError(f"missing generated equations: {missing}")
    return entries


def main() -> int:
    args = parse_args()
    formulas = load_formulas(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    index_path = args.index or args.output.with_suffix(".index.json")
    ast = build_ast(formulas, pandoc_api_version(args.pandoc))
    subprocess.run(
        [args.pandoc, "--from=json", "--to=docx", f"--output={args.output}"],
        input=json.dumps(ast, ensure_ascii=False).encode("utf-8"),
        stderr=subprocess.PIPE,
        check=True,
    )
    entries = inspect_library(args.output, formulas)
    index_path.write_text(
        json.dumps({"library": str(args.output), "formulas": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"generated={len(entries)} library={args.output} index={index_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
