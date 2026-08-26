#!/usr/bin/env python3
"""Read-only structural audit for DOCX files containing native OMML formulas."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "m": M, "r": R, "pr": PKG_R}
QW = lambda name: f"{{{W}}}{name}"
DEFAULT_ALLOWED = {"word/document.xml", "word/styles.xml", "word/settings.xml"}


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
    parser.add_argument("--json", type=Path, help="Write the report to this JSON file")
    return parser.parse_args()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_package(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC failure in {bad}")
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def xml(package: dict[str, bytes], part: str) -> ET.Element:
    if part not in package:
        raise RuntimeError(f"missing required part: {part}")
    return ET.fromstring(package[part])


def media_hashes(package: dict[str, bytes]) -> dict[str, str]:
    return {
        name: sha256(data)
        for name, data in sorted(package.items())
        if name.startswith("word/media/")
    }


def relationship_targets(package: dict[str, bytes]) -> dict[str, list[dict[str, str]]]:
    result = {}
    for name, data in sorted(package.items()):
        if not name.endswith(".rels"):
            continue
        root = ET.fromstring(data)
        result[name] = sorted(
            ({key: value for key, value in relation.attrib.items()} for relation in root),
            key=lambda item: (item.get("Id", ""), item.get("Target", "")),
        )
    return result


def comparable_part(name: str, data: bytes | None) -> bytes | str | None:
    if data is None:
        return None
    if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
        root = ET.fromstring(data)
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


def current_text(root: ET.Element) -> str:
    return "\n".join(
        "".join(text.text or "" for text in paragraph.findall(".//w:t", NS))
        for paragraph in root.findall(".//w:p", NS)
    )


def inspect(path: Path, package: dict[str, bytes]) -> tuple[dict, ET.Element]:
    document = xml(package, "word/document.xml")
    styles = xml(package, "word/styles.xml") if "word/styles.xml" in package else None
    equations = document.findall(".//m:oMath", NS)
    math_runs_without_cambria = 0
    for equation in equations:
        for run in equation.findall(".//m:r", NS):
            fonts = run.findall(".//w:rFonts", NS)
            if not any(
                font.get(QW("ascii")) == "Cambria Math" and font.get(QW("hAnsi")) == "Cambria Math"
                for font in fonts
            ):
                math_runs_without_cambria += 1

    report = {
        "path": str(path),
        "sha256": sha256(path.read_bytes()),
        "parts": len(package),
        "paragraphs": len(document.findall(".//w:p", NS)),
        "omath": len(equations),
        "omath_para": len(document.findall(".//m:oMathPara", NS)),
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
        "math_runs_without_explicit_cambria": math_runs_without_cambria,
    }
    return report, document


def main() -> int:
    args = parse_args()
    errors = []
    package = read_package(args.docx)
    report, document = inspect(args.docx, package)

    if args.expected_formulas is not None and report["omath"] != args.expected_formulas:
        errors.append(f"formula count {report['omath']} != {args.expected_formulas}")
    if args.require_cambria_math and report["math_runs_without_explicit_cambria"]:
        errors.append(
            f"{report['math_runs_without_explicit_cambria']} math runs lack explicit Cambria Math"
        )
    if args.author:
        inserted = report["insertions_by_author"].get(args.author, 0)
        deleted = report["deletions_by_author"].get(args.author, 0)
        if args.expected_author_insertions is not None and inserted != args.expected_author_insertions:
            errors.append(f"author insertion count {inserted} != {args.expected_author_insertions}")
        if args.expected_author_deletions is not None and deleted != args.expected_author_deletions:
            errors.append(f"author deletion count {deleted} != {args.expected_author_deletions}")

    text = current_text(document)
    residuals = {}
    for pattern in args.residual:
        matches = re.findall(pattern, text)
        if matches:
            residuals[pattern] = len(matches)
            errors.append(f"residual pattern {pattern!r}: {len(matches)} matches")
    report["residuals"] = residuals

    if args.baseline:
        baseline_package = read_package(args.baseline)
        baseline_report, _ = inspect(args.baseline, baseline_package)
        allowed = DEFAULT_ALLOWED | set(args.allow_part)
        changed_protected = []
        all_parts = set(package) | set(baseline_package)
        for part in sorted(all_parts - allowed):
            if comparable_part(part, package.get(part)) != comparable_part(part, baseline_package.get(part)):
                changed_protected.append(part)
        if changed_protected:
            errors.append(f"protected package parts changed: {changed_protected}")
        if report["media"] != baseline_report["media"]:
            errors.append("media names or hashes changed")
        if report["relationships"] != baseline_report["relationships"]:
            errors.append("relationship definitions changed")
        for field in ("drawings", "sections", "comments", "bookmarks", "fields", "hyperlinks", "content_controls"):
            if report[field] != baseline_report[field]:
                errors.append(f"{field} count changed: {baseline_report[field]} -> {report[field]}")
        report["baseline"] = {
            "path": str(args.baseline),
            "sha256": baseline_report["sha256"],
            "changed_protected_parts": changed_protected,
        }

    report["errors"] = errors
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json:
        args.json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, zipfile.BadZipFile, ET.ParseError, re.error) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
