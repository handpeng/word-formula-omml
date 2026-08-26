from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.fixtures.docx_fixture import build_fixture_package, write_fixture
from tests.test_applicator import (
    make_other_revision_paragraph,
    make_package,
    make_paragraph,
    make_row,
    write_case,
)
from tests.test_semantic_bridge import math_run, omath, script
from word_formula_omml.applicator import _Template, _styled_template, prepare_application, stage_application
from word_formula_omml.canonical import canonicalize_formula
from word_formula_omml.contract import load_manifest
from word_formula_omml.inventory import inventory_docx


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def load_audit():
    root = Path(__file__).resolve().parents[1]
    specification = importlib.util.spec_from_file_location("audit_for_test", root / "scripts" / "audit_docx_formulas.py")
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def write_parts(path: Path, parts: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            archive.writestr(name, parts[name])


def mutate_part(path: Path, part: str, mutate) -> None:
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(parts[part])
    mutate(root)
    parts[part] = ET.tostring(root, encoding="UTF-8", xml_declaration=True, short_empty_elements=True)
    write_parts(path, parts)


def add_minimal_content_types(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = ET.Element(q(CT, "Types"))
    ET.SubElement(
        root,
        q(CT, "Default"),
        {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"},
    )
    ET.SubElement(root, q(CT, "Default"), {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(
        root,
        q(CT, "Override"),
        {
            "PartName": "/word/document.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        },
    )
    ET.SubElement(
        root,
        q(CT, "Override"),
        {
            "PartName": "/word/settings.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
        },
    )
    parts["[Content_Types].xml"] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            archive.writestr(name, parts[name])
    return output.getvalue()


def semantic_template(node: ET.Element | None = None) -> dict:
    node = node or omath(script(math_run("x"), sub=math_run("i")))
    return {
        "node": node,
        "semantic": {"status": "PASS", "canonical": canonicalize_formula("x_i")},
        "auto_eligible": True,
    }


class AuditTests(unittest.TestCase):
    @staticmethod
    def make_case(directory: Path, *, other_revision: bool = False, wrong_template: bool = False):
        text = "before x_i after"
        row = make_row(
            "safe",
            text,
            1,
            style={
                "math_font": "Cambria Math",
                "math_font_policy": "CAMBRIA_MATH",
                "color": "0000FF",
                "size": "24",
            },
        )
        row["canonical"] = canonicalize_formula("x_i")
        paragraphs = [make_paragraph(text)]
        if other_revision:
            paragraphs.append(make_other_revision_paragraph())
        source, manifest, templates = write_case(directory, paragraphs, [row])
        if wrong_template:
            templates["safe"] = semantic_template(omath(math_run("z")))
        else:
            templates["safe"] = semantic_template()
        job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
        paths = {"redlined": directory / "redlined.docx", "clean": directory / "clean.docx"}
        staged = stage_application(source, manifest, job, plan, templates, paths)
        return source, manifest, plan, staged, paths

    def test_bound_audit_emits_identity_bound_evidence_for_both_artifacts(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name))
            for kind in ("redlined", "clean"):
                report = audit.audit_artifact(
                    paths[kind],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id=kind,
                    artifact_kind=kind,
                )
                self.assertEqual("PASS", report["status"], report["errors"])
                self.assertEqual("PASS", report["evidence_state"])
                self.assertEqual("STRUCTURAL_AUDIT", report["evidence"]["gate"])
                self.assertEqual(kind, report["evidence"]["artifact_logical_id"])
                self.assertEqual(kind, report["evidence"]["artifact_kind"])
                self.assertEqual(plan.plan_sha256, report["evidence"]["application_plan_sha256"])
                self.assertEqual(
                    hashlib.sha256(paths[kind].read_bytes()).hexdigest(),
                    report["evidence"]["artifact_sha256"],
                )
                self.assertEqual("NOT_DERIVED", report["audit"]["delivery_status"])
                self.assertNotIn("COMPLETE", report["audit"].values())
                accepted = audit.validate_audit_evidence(
                    report,
                    paths[kind],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id=kind,
                )
                self.assertEqual(report["evidence"], accepted)

    def test_same_count_revision_content_mutation_is_detected(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name), other_revision=True)

            def mutate(root):
                for node in root.findall(".//w:ins", {"w": W}):
                    if node.get(q(W, "author")) == "Other Reviewer":
                        node.find(".//w:t", {"w": W}).text = "other-author z_i"
                        return
                raise AssertionError("other-author insertion not found")

            mutate_part(paths["redlined"], "word/document.xml", mutate)
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(report["revision_fingerprint_drift"]["changed"])
            self.assertTrue(any("pre-existing revision fingerprint changed" in error for error in report["errors"]))
            self.assertEqual("NOT_EMITTED", report["evidence_state"])

    def test_source_visible_reconstruction_failure_is_explicit(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name))

            def mutate(root):
                for node in root.findall(".//w:t", {"w": W}):
                    if node.text == "before ":
                        node.text = "changed "
                        return
                raise AssertionError("source prefix not found")

            mutate_part(paths["redlined"], "word/document.xml", mutate)
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertEqual("FAIL", report["source_reconstruction"]["status"])
            self.assertTrue(any("source-visible text reconstruction" in error for error in report["errors"]))

    def test_bound_audit_rejects_unplanned_text_drift_in_document_story(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            text = "before x_i after"
            source, manifest, templates = write_case(
                directory,
                [make_paragraph(text), make_paragraph("untouched")],
                [make_row("safe", text, 1)],
            )
            job, plan = prepare_application(source, manifest, ["redlined"], templates)
            staged = stage_application(
                source,
                manifest,
                job,
                plan,
                templates,
                {"redlined": directory / "redlined.docx"},
            )

            def mutate(root):
                paragraphs = root.findall(".//w:body/w:p", {"w": W})
                self.assertEqual(2, len(paragraphs))
                paragraphs[1].find(".//w:t", {"w": W}).text = "changed"

            mutate_part(staged.artifact_paths["redlined"], "word/document.xml", mutate)
            report = audit.audit_artifact(
                staged.artifact_paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertEqual("FAIL", report["story_content_drift"]["status"])
            self.assertTrue(any("unplanned story content drift" in error for error in report["errors"]))

    def test_redlined_session_insertion_relocation_fails_closed(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name), other_revision=True)

            def move_session_insertion(root):
                paragraphs = root.findall(".//w:body/w:p", {"w": W})
                self.assertEqual(2, len(paragraphs))
                insertion = next(
                    child
                    for child in paragraphs[0]
                    if child.tag == q(W, "ins") and child.get(q(W, "author")) == plan.revision_author
                )
                paragraphs[0].remove(insertion)
                paragraphs[1].append(insertion)

            mutate_part(staged.artifact_paths["redlined"], "word/document.xml", move_session_insertion)
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(any("outside its frozen location" in error for error in report["errors"]))

    def test_affected_paragraph_structure_drift_is_not_masked(self):
        audit = load_audit()
        for kind in ("redlined", "clean"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                source, manifest, plan, staged, paths = self.make_case(Path(name))

                def mutate(root):
                    runs = root.findall(".//w:body/w:p/w:r", {"w": W})
                    self.assertTrue(runs)
                    rpr = runs[0].find("w:rPr", {"w": W})
                    if rpr is None:
                        rpr = ET.Element(q(W, "rPr"))
                        runs[0].insert(0, rpr)
                    ET.SubElement(rpr, q(W, "b"))

                mutate_part(paths[kind], "word/document.xml", mutate)
                report = audit.audit_artifact(paths[kind], baseline=source, manifest=manifest, application_plan=plan)
                self.assertEqual("FAIL", report["status"])
                self.assertEqual("FAIL", report["story_content_drift"]["status"])
                self.assertTrue(
                    any(
                        "run fragment changed" in error or "unplanned story content drift" in error
                        for error in report["errors"]
                    )
                )

    def test_protected_package_media_and_relationship_drift_fail_closed(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source = directory / "fixture.docx"
            candidate = directory / "candidate.docx"
            write_fixture(source)
            candidate.write_bytes(source.read_bytes())
            mutate_part(candidate, "customXml/item1.xml", lambda root: setattr(root, "text", "mutated"))
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("FAIL", report["status"])
            self.assertIn("customXml/item1.xml", report["baseline"]["changed_protected_parts"])
            self.assertTrue(any("protected package parts changed" in error for error in report["errors"]))

            candidate.write_bytes(source.read_bytes())
            with zipfile.ZipFile(candidate) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            parts["customXml/extra.xml"] = b'<extra xmlns="urn:test">added</extra>'
            write_parts(candidate, parts)
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("FAIL", report["status"])
            self.assertIn("customXml/extra.xml", report["package_diff"]["added"])
            self.assertTrue(any("protected package parts changed" in error for error in report["errors"]))

            candidate.write_bytes(source.read_bytes())
            with zipfile.ZipFile(candidate) as archive:
                parts = {name: archive.read(name) for name in archive.namelist() if name != "customXml/item1.xml"}
            write_parts(candidate, parts)
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("FAIL", report["status"])
            self.assertIn("customXml/item1.xml", report["package_diff"]["removed"])
            self.assertTrue(any("protected package parts changed" in error for error in report["errors"]))

            candidate.write_bytes(source.read_bytes())
            with zipfile.ZipFile(candidate) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            parts["word/media/image1.png"] = b"changed-media"
            write_parts(candidate, parts)
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("FAIL", report["status"])
            self.assertIn("media names or hashes changed", report["errors"])

            candidate.write_bytes(source.read_bytes())
            def mutate_relationships(root):
                root[0].set("Target", "changed.xml")
            mutate_part(candidate, "word/_rels/document.xml.rels", mutate_relationships)
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("FAIL", report["status"])
            self.assertIn("relationship definitions changed", report["errors"])

    def test_semantic_mismatch_from_shared_bridge_fails_audit(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name), wrong_template=True)
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            occurrence = report["occurrence_accounting"][0]
            self.assertEqual("MISMATCH", occurrence["semantic"]["status"])
            self.assertTrue(any("semantic MISMATCH" in error for error in report["errors"]))

    def test_unaccounted_manifest_occurrence_cannot_pass_or_emit_evidence(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source, manifest, plan, staged, paths = self.make_case(directory)
            extra_row = make_row("unaccounted", "after x_i", 2)
            extra_row["canonical"] = canonicalize_formula("x_i")
            expanded_manifest = manifest.to_dict()
            expanded_manifest.pop("manifest_id", None)
            expanded_manifest["formulas"].append(extra_row)
            from word_formula_omml.contract import load_manifest

            expanded_manifest_obj = load_manifest(expanded_manifest)
            # The plan remains frozen for the original one-row manifest.  A
            # structural audit must surface the missing occurrence instead of
            # treating the candidate as a complete batch.
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=expanded_manifest_obj,
                application_plan=plan,
                job=None,
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(any(item["status"] == "UNACCOUNTED" for item in report["occurrence_accounting"]))
            self.assertEqual("NOT_EMITTED", report["evidence_state"])

    def test_audit_evidence_rejects_stale_candidate_and_cross_artifact_use(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source, manifest, plan, staged, paths = self.make_case(Path(name))
            report = audit.audit_artifact(
                paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            original_redlined = paths["redlined"].read_bytes()
            with self.assertRaisesRegex(audit.AuditError, "artifact_sha256"):
                audit.validate_audit_evidence(
                    report,
                    paths["clean"],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id="clean",
                )
            paths["redlined"].write_bytes(paths["redlined"].read_bytes() + b"stale mutation")
            with self.assertRaisesRegex(audit.AuditError, "artifact_sha256"):
                audit.validate_audit_evidence(
                    report,
                    paths["redlined"],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id="redlined",
                )
            paths["redlined"].write_bytes(original_redlined)

            tampered = copy.deepcopy(report)
            tampered["evidence"]["artifact_kind"] = "clean"
            with self.assertRaisesRegex(audit.AuditError, "evidence_id"):
                audit.validate_audit_evidence(
                    tampered,
                    paths["redlined"],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id="redlined",
                )

            completed_claim = copy.deepcopy(report)
            completed_claim["audit"]["delivery_status"] = "COMPLETE"
            with self.assertRaisesRegex(audit.AuditError, "overall delivery completion"):
                audit.validate_audit_evidence(
                    completed_claim,
                    paths["redlined"],
                    baseline=source,
                    manifest=manifest,
                    application_plan=plan,
                    job=staged.job,
                    artifact_id="redlined",
                )

            with self.assertRaisesRegex(audit.AuditError, "both manifest and application_plan"):
                audit.validate_audit_evidence(report, paths["redlined"], job=staged.job)

            direct_complete = copy.deepcopy(report["evidence"])
            direct_complete["status"] = "COMPLETE"
            direct_complete["evidence_id"] = audit._audit_evidence_id(direct_complete)
            with self.assertRaisesRegex(audit.AuditError, "overall delivery completion"):
                audit.validate_audit_evidence(direct_complete, paths["redlined"])

            missing_plan_hash = copy.deepcopy(report["evidence"])
            missing_plan_hash.pop("application_plan_sha256")
            missing_plan_hash["evidence_id"] = audit._audit_evidence_id(missing_plan_hash)
            with self.assertRaisesRegex(audit.AuditError, "application_plan_sha256"):
                audit.validate_audit_evidence(missing_plan_hash, paths["redlined"])

    def test_clean_mapping_keeps_identical_native_omml_bound_to_the_native_node(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source = directory / "source.docx"
            style = {"math_font": "Cambria Math", "math_font_policy": "CAMBRIA_MATH"}
            template = omath(script(math_run("x"), sub=math_run("i")))
            native = _styled_template(_Template(template, "template"), style)
            paragraph = make_paragraph("x_i")
            paragraph.append(native)
            source.write_bytes(add_minimal_content_types(make_package([paragraph])))

            inventory = inventory_docx(source)
            rows = []
            for original in inventory.formulas:
                row = copy.deepcopy(original)
                if row["source_type"] != "EXISTING_OMML":
                    row.update(
                        {
                            "status": "APPROVED",
                            "source": "x_i",
                            "raw_source": "x_i",
                            "latex": "x_i",
                            "canonical": canonicalize_formula("x_i"),
                            "resolved_style": {"status": "RESOLVED", "auto_eligible": True, "style": style},
                        }
                    )
                rows.append(row)
            manifest_data = inventory.to_dict()
            manifest_data.pop("manifest_id", None)
            manifest_data["formulas"] = rows
            manifest = load_manifest(manifest_data)
            raw_id = next(row["id"] for row in rows if row["source_type"] != "EXISTING_OMML")
            templates = {
                raw_id: {
                    "node": template,
                    "semantic": {"status": "PASS", "canonical": canonicalize_formula("x_i")},
                    "auto_eligible": True,
                }
            }
            job, plan = prepare_application(source, manifest, ["clean"], templates)
            staged = stage_application(
                source,
                manifest,
                job,
                plan,
                templates,
                {"clean": directory / "clean.docx"},
            )
            report = audit.audit_artifact(
                staged.artifact_paths["clean"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="clean",
                artifact_kind="clean",
            )
            self.assertEqual("PASS", report["status"], report["errors"])

            def mutate_native(root):
                equations = root.findall(".//m:oMath", {"m": M})
                self.assertEqual(2, len(equations))
                equations[1].find(".//m:t", {"m": M}).text = "z"

            mutate_part(staged.artifact_paths["clean"], "word/document.xml", mutate_native)
            report = audit.audit_artifact(
                staged.artifact_paths["clean"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="clean",
                artifact_kind="clean",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(any("preserved native OMML content changed" in error for error in report["errors"]))

    def test_preserved_native_omml_is_bound_to_one_inventory_node(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source = directory / "native.docx"
            write_parts(source, build_fixture_package())
            manifest = inventory_docx(source)
            native_rows = [row for row in manifest.formulas if row["source_type"] == "EXISTING_OMML"]
            self.assertEqual(2, len(native_rows))
            job, plan = prepare_application(source, manifest, ["redlined"], {})
            staged = stage_application(source, manifest, job, plan, {}, {"redlined": directory / "redlined.docx"})

            report = audit.audit_artifact(
                staged.artifact_paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("PASS", report["status"], report["errors"])
            accounting = {item["id"]: item for item in report["occurrence_accounting"]}
            for row in native_rows:
                occurrence = accounting[row["id"]]
                self.assertTrue(occurrence["preserved"])
                self.assertEqual(row["extensions"]["inventory"]["node_path"], occurrence["omml"]["location"])
            self.assertEqual("PASS", report["evidence_state"])

            def add_unplanned_revision(root):
                body = root.find(".//w:body", {"w": W})
                self.assertIsNotNone(body)
                insertion = ET.SubElement(
                    body,
                    q(W, "ins"),
                    {q(W, "id"): "999", q(W, "author"): plan.revision_author},
                )
                run = ET.SubElement(insertion, q(W, "r"))
                ET.SubElement(run, q(W, "t")).text = "unplanned"

            mutate_part(staged.artifact_paths["redlined"], "word/document.xml", add_unplanned_revision)
            report = audit.audit_artifact(
                staged.artifact_paths["redlined"],
                baseline=source,
                manifest=manifest,
                application_plan=plan,
                job=staged.job,
                artifact_id="redlined",
                artifact_kind="redlined",
            )
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(any("no APPLY actions" in error for error in report["errors"]))

    def test_historical_structural_flags_remain_usable(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            source = directory / "source.docx"
            source.write_bytes(make_package([make_paragraph("ordinary text")]))
            report = audit.audit_artifact(source, baseline=source, expected_formulas=0)
            self.assertEqual("PASS", report["status"], report["errors"])
            self.assertEqual("NOT_EMITTED", report["evidence_state"])

    def test_directory_entries_remain_compatible_with_structural_audit(self):
        audit = load_audit()
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "source.docx"
            candidate = Path(name) / "candidate.docx"
            write_fixture(source)
            with zipfile.ZipFile(source) as archive:
                parts = {part: archive.read(part) for part in archive.namelist()}
            with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/", b"")
                for part, data in parts.items():
                    archive.writestr(part, data)
            report = audit.audit_artifact(candidate, baseline=source)
            self.assertEqual("PASS", report["status"], report["errors"])


if __name__ == "__main__":
    unittest.main()
