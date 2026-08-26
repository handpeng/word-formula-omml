#!/usr/bin/env python3
"""Build the repository-controlled representative W7 acceptance bundle.

This is an acceptance-fixture entry point, not a general document converter. It
uses the W1 adversarial corpus, selects only the reviewed representative P0
occurrences, generates semantically checked OMML with Pandoc, resolves Word
math style, stages the exact redlined+clean pair, binds W6 audit evidence, and
freezes the native-Word handoff request. It deliberately stops before inventing
Microsoft Word observations.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.docx_fixture import load_expectations, write_fixture
from word_formula_omml.acceptance import (
    acceptance_observation_template,
    prepare_representative_acceptance,
)
from word_formula_omml.applicator import (
    dump_application_plan,
    prepare_application,
    stage_application,
)
from word_formula_omml.canonical import canonicalize_formula
from word_formula_omml.contract import (
    Confidence,
    OccurrenceStatus,
    SourceType,
    dump_manifest,
    load_manifest,
)
from word_formula_omml.gates import bind_structural_audit_evidence
from word_formula_omml.inventory import inventory_docx
from word_formula_omml.style import resolve_style


ROOT = Path(__file__).resolve().parents[1]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W, "m": M}
MARKER_PREFIX = "OMML_ID:"
SELECTED_IDS = (
    "raw-latex",
    "plain-scripts",
    "plain-operators",
    "unicode-operators",
    "display-omml",
    "interval",
)
ACCOUNTED_IDS = SELECTED_IDS + ("existing-omml",)
APPLY_IDS = frozenset(SELECTED_IDS) - {"display-omml"}


class BundleError(RuntimeError):
    """Raised when the deterministic representative bundle cannot be proven."""


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expectation_rows() -> dict[str, dict]:
    data = load_expectations()
    formulas = data.get("formulas")
    if not isinstance(formulas, list):
        raise BundleError("W1 expectations have no formulas array")
    rows = {row.get("id"): row for row in formulas if isinstance(row, dict)}
    missing = [occurrence_id for occurrence_id in ACCOUNTED_IDS if occurrence_id not in rows]
    if missing:
        raise BundleError(f"W1 expectations are missing representative occurrences: {missing}")
    return {occurrence_id: rows[occurrence_id] for occurrence_id in ACCOUNTED_IDS}


def _match_inventory(expectation: dict, inventory_rows: list[dict]) -> dict:
    source_type = expectation.get("source_type")
    paragraph = expectation.get("paragraph")
    layout = expectation.get("layout")
    story = expectation.get("story", "main")
    candidates = [
        row
        for row in inventory_rows
        if row.get("source_type") == source_type
        and row.get("story") == story
        and row.get("package_part") == "word/document.xml"
    ]
    if paragraph is not None:
        candidates = [row for row in candidates if row.get("paragraph") == paragraph]
    if source_type == SourceType.EXISTING_OMML.value:
        candidates = [row for row in candidates if row.get("layout") == layout]
    else:
        expected_source = expectation.get("source")
        candidates = [
            row
            for row in candidates
            if row.get("raw_source", row.get("source")) == expected_source
        ]
    if len(candidates) != 1:
        identity = expectation.get("id")
        raise BundleError(
            f"representative occurrence {identity!r} resolved to {len(candidates)} inventory rows"
        )
    return copy.deepcopy(candidates[0])


def _styles_root(source: Path) -> ET.Element:
    try:
        with zipfile.ZipFile(source) as archive:
            return ET.fromstring(archive.read("word/styles.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise BundleError(f"cannot read W1 Word styles: {error}") from error


def _representative_manifest(source: Path):
    inventory = inventory_docx(source)
    expectations = _expectation_rows()
    styles = _styles_root(source)
    rows: list[dict] = []
    for occurrence_id in ACCOUNTED_IDS:
        expectation = expectations[occurrence_id]
        row = _match_inventory(expectation, list(inventory.formulas))
        row["id"] = occurrence_id
        row["latex"] = expectation["latex"]
        if row.get("source_type") == SourceType.EXISTING_OMML.value:
            row["status"] = OccurrenceStatus.PRESERVED.value
            row["confidence"] = Confidence.AUTHORITATIVE.value
        else:
            normalized = expectation["latex"]
            row["normalized_latex"] = normalized
            row["canonical"] = canonicalize_formula(normalized)
            row["confidence"] = Confidence.AUTHORITATIVE.value
            row["ambiguity"] = []
            row["status"] = OccurrenceStatus.APPROVED.value
            resolution = resolve_style(row, styles=styles)
            if not resolution.auto_eligible:
                raise BundleError(
                    f"representative occurrence {occurrence_id!r} style is not auto-eligible: {resolution.reason}"
                )
            row["resolved_style"] = resolution.to_dict()
        rows.append(row)
    return load_manifest(
        {
            "schema_version": 1,
            "source_sha256": inventory.source_sha256,
            "formulas": rows,
            "extensions": {
                "p0_representative_acceptance": {
                    "version": 1,
                    "fixture": "W1 adversarial-v1",
                    "selected_occurrence_ids": list(SELECTED_IDS),
                }
            },
        }
    )


def _generator_manifest(manifest):
    return load_manifest(
        {
            "schema_version": 1,
            "source_sha256": manifest.source_sha256,
            "formulas": [copy.deepcopy(row) for row in manifest.formulas if row["id"] in APPLY_IDS],
            "extensions": {"p0_representative_template_set": {"version": 1}},
        }
    )


def _generate_library(manifest, directory: Path, pandoc: str) -> tuple[Path, Path]:
    generator_manifest = directory / "p0-template-manifest.json"
    dump_manifest(_generator_manifest(manifest), generator_manifest)
    library = directory / "p0-template-library.docx"
    index = directory / "p0-template-library.index.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_omml_library.py"),
            str(generator_manifest),
            str(library),
            "--index",
            str(index),
            "--pandoc",
            pandoc,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostics = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise BundleError(f"representative OMML generation failed: {diagnostics}")
    return library, index


def _templates(library: Path, index_path: Path) -> dict[str, dict]:
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entries = index["formulas"]
        with zipfile.ZipFile(library) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile, ET.ParseError) as error:
        raise BundleError(f"cannot read generated representative OMML library: {error}") from error
    by_id = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
    paragraphs = root.findall(".//w:body//w:p", NS)
    nodes: dict[str, ET.Element] = {}
    for index, paragraph in enumerate(paragraphs):
        marker = "".join(paragraph.itertext()).strip()
        if not marker.startswith(MARKER_PREFIX):
            continue
        occurrence_id = marker[len(MARKER_PREFIX) :]
        if occurrence_id not in APPLY_IDS:
            continue
        if index + 1 >= len(paragraphs):
            raise BundleError(f"generated library has no equation after {marker}")
        equations = paragraphs[index + 1].findall(".//m:oMath", NS)
        if len(equations) != 1:
            raise BundleError(f"generated library has {len(equations)} equations after {marker}")
        nodes[occurrence_id] = equations[0]
    missing = sorted(APPLY_IDS - set(nodes))
    if missing:
        raise BundleError(f"generated library is missing representative templates: {missing}")
    result: dict[str, dict] = {}
    for occurrence_id in sorted(APPLY_IDS):
        entry = by_id.get(occurrence_id)
        if not isinstance(entry, dict) or entry.get("auto_eligible") is not True:
            raise BundleError(f"generated template {occurrence_id!r} is not semantically auto-eligible")
        result[occurrence_id] = {
            "node": nodes[occurrence_id],
            "semantic": copy.deepcopy(entry.get("semantic")),
            "auto_eligible": True,
            "omml_sha256": entry.get("omml_sha256"),
        }
    return result


def _load_audit_module():
    specification = importlib.util.spec_from_file_location(
        "p0_acceptance_audit",
        ROOT / "scripts" / "audit_docx_formulas.py",
    )
    if specification is None or specification.loader is None:
        raise BundleError("cannot load W6 audit module")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _bind_structural(source, manifest, plan, staged, candidates):
    audit = _load_audit_module()
    job = staged.job
    reports: dict[str, dict] = {}
    for logical_id in ("redlined", "clean"):
        report = audit.audit_artifact(
            candidates[logical_id],
            baseline=source,
            manifest=manifest,
            application_plan=plan,
            job=job,
            artifact_id=logical_id,
            artifact_kind=logical_id,
        )
        if report.get("status") != "PASS":
            raise BundleError(
                f"W6 audit failed for representative {logical_id}: {report.get('errors')}"
            )
        reports[logical_id] = report
        job = bind_structural_audit_evidence(
            job,
            logical_id,
            candidates[logical_id],
            report,
        )
    return job, reports


def build_bundle(output: Path, *, pandoc: str = "pandoc") -> dict:
    output.mkdir(parents=True, exist_ok=True)
    source = output / "p0-representative-source.docx"
    write_fixture(source)
    source_hash_before = _sha256(source)
    manifest = _representative_manifest(source)
    manifest_path = output / "p0-representative.manifest.json"
    dump_manifest(manifest, manifest_path)
    library, library_index = _generate_library(manifest, output, pandoc)
    templates = _templates(library, library_index)

    job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
    plan_path = output / "p0-representative.application-plan.json"
    dump_application_plan(plan, plan_path)
    candidates = {
        "redlined": output / "p0-representative-redlined.docx",
        "clean": output / "p0-representative-clean.docx",
    }
    staged = stage_application(source, manifest, job, plan, templates, candidates)
    structural_job, reports = _bind_structural(source, manifest, plan, staged, candidates)
    if _sha256(source) != source_hash_before:
        raise BundleError("representative source changed while building the bundle")

    structural_job_path = output / "p0-representative.job.structural.json"
    _write_json(structural_job_path, structural_job.to_dict())
    for logical_id, report in reports.items():
        _write_json(output / f"p0-representative.{logical_id}.audit.json", report)

    coverage_path = ROOT / "tests" / "fixtures" / "p0_acceptance_coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    request = prepare_representative_acceptance(structural_job, candidates, coverage)
    observations = acceptance_observation_template(request)
    request_path = output / "p0-acceptance-request.json"
    observations_path = output / "p0-word-observations.json"
    _write_json(request_path, request)
    _write_json(observations_path, observations)

    files = {
        "source": source,
        "manifest": manifest_path,
        "application_plan": plan_path,
        "job_structural": structural_job_path,
        "redlined": candidates["redlined"],
        "clean": candidates["clean"],
        "redlined_audit": output / "p0-representative.redlined.audit.json",
        "clean_audit": output / "p0-representative.clean.audit.json",
        "acceptance_request": request_path,
        "word_observations": observations_path,
        "template_library": library,
        "template_index": library_index,
    }
    bundle = {
        "schema_version": 1,
        "kind": "P0_REPRESENTATIVE_ACCEPTANCE_BUNDLE",
        "state": "AWAITING_NATIVE_WORD",
        "acceptance_request_id": request["acceptance_request_id"],
        "source_sha256": source_hash_before,
        "selected_occurrence_ids": list(SELECTED_IDS),
        "files": {
            name: {"path": path.name, "sha256": _sha256(path)}
            for name, path in sorted(files.items())
        },
    }
    bundle_path = output / "p0-acceptance-bundle.json"
    _write_json(bundle_path, bundle)
    return {**bundle, "bundle": str(bundle_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pandoc", default="pandoc")
    args = parser.parse_args()
    try:
        result = build_bundle(args.output, pandoc=args.pandoc)
    except (BundleError, OSError, ValueError, RuntimeError) as error:
        print(f"P0 representative bundle failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
