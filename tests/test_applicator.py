from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

import word_formula_omml.applicator as applicator
from word_formula_omml.applicator import (
    ApplicationError,
    accept_session_revisions,
    apply_application_plan,
    bind_staged_artifacts,
    finalize_artifact_set,
    dump_application_plan,
    load_application_plan,
    prepare_application,
    reject_session_revisions,
    stage_application,
    validate_staged_artifacts,
)
from word_formula_omml.contract import (
    DeliveryStatus,
    GateState,
    OccurrenceStatus,
    bind_gate_evidence,
    load_manifest,
    set_artifact_content,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
XML = "http://www.w3.org/XML/1998/namespace"


def q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


ET.register_namespace("w", W)
ET.register_namespace("m", M)


def xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True, short_empty_elements=True)


def make_run(text: str, *, color: str | None = None, size: str | None = None) -> ET.Element:
    run = ET.Element(q(W, "r"))
    if color is not None or size is not None:
        properties = ET.SubElement(run, q(W, "rPr"))
        if color is not None:
            ET.SubElement(properties, q(W, "color"), {q(W, "val"): color})
        if size is not None:
            ET.SubElement(properties, q(W, "sz"), {q(W, "val"): size})
    text_node = ET.SubElement(run, q(W, "t"))
    if text.startswith((" ", "\t")) or text.endswith((" ", "\t")):
        text_node.set(q(XML, "space"), "preserve")
    text_node.text = text
    return run


def make_package(paragraphs: list[ET.Element], *, with_other_revision: bool = False) -> bytes:
    document = ET.Element(q(W, "document"))
    body = ET.SubElement(document, q(W, "body"))
    for paragraph in paragraphs:
        body.append(paragraph)
    settings = ET.Element(q(W, "settings"))
    if with_other_revision:
        ET.SubElement(
            settings,
            q(W, "rsids"),
        )
    parts = {
        "word/document.xml": xml_bytes(document),
        "word/settings.xml": xml_bytes(settings),
        "customXml/item1.xml": b'<fixture xmlns="urn:test">protected</fixture>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, parts[name])
    return output.getvalue()


def make_package_with_disabled_tracking(paragraphs: list[ET.Element]) -> bytes:
    document = ET.Element(q(W, "document"))
    body = ET.SubElement(document, q(W, "body"))
    for paragraph in paragraphs:
        body.append(paragraph)
    settings = ET.Element(q(W, "settings"))
    ET.SubElement(settings, q(W, "trackRevisions"), {q(W, "val"): "false"})
    parts = {
        "word/document.xml": xml_bytes(document),
        "word/settings.xml": xml_bytes(settings),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            archive.writestr(name, parts[name])
    return output.getvalue()


def make_paragraph(text: str) -> ET.Element:
    paragraph = ET.Element(q(W, "p"))
    paragraph.append(make_run(text))
    return paragraph


def make_other_revision_paragraph() -> ET.Element:
    paragraph = ET.Element(q(W, "p"))
    insertion = ET.SubElement(
        paragraph,
        q(W, "ins"),
        {q(W, "id"): "10", q(W, "author"): "Other Reviewer"},
    )
    insertion.append(make_run("other-author x_i"))
    return paragraph


def make_template(text: str = "x_i", *, math_style: str = "p") -> dict:
    equation = ET.Element(q(M, "oMath"))
    math_run = ET.SubElement(equation, q(M, "r"))
    math_properties = ET.SubElement(math_run, q(M, "rPr"))
    ET.SubElement(math_properties, q(M, "sty"), {q(M, "val"): math_style})
    ET.SubElement(math_run, q(M, "t")).text = text
    return {
        "node": equation,
        "semantic": {"status": "PASS", "canonical": {"kind": "test"}},
        "auto_eligible": True,
    }


def make_row(
    occurrence_id: str,
    text: str,
    paragraph: int,
    *,
    source: str = "x_i",
    status: str = OccurrenceStatus.APPROVED.value,
    start: int | None = None,
    protected: dict[str, bool] | None = None,
    inside_revision: bool = False,
    ancestry: list | None = None,
    run_count: int = 1,
    style: dict | None = None,
) -> dict:
    if start is None:
        start = text.index(source)
    end = start + len(source)
    prefix = text[:start]
    suffix = text[end:]
    protected = protected or {
        "table": False,
        "bookmark": False,
        "comment_range": False,
        "hyperlink": False,
        "field": False,
        "drawing": False,
        "content_control": False,
        "embedded_object": False,
    }
    style = style or {
        "math_font": "Cambria Math",
        "math_font_policy": "CAMBRIA_MATH",
        "color": "0000FF",
        "size": "24",
        "math_style": "italic",
    }
    row = {
        "id": occurrence_id,
        "latex": source,
        "source": source,
        "raw_source": source,
        "layout": "inline",
        "target_layout": "inline",
        "paragraph": paragraph,
        "run_index": 1,
        "run_start": start,
        "run_end": end,
        "anchor_before": prefix or None,
        "anchor_after": suffix or None,
        "run_boundaries": {
            "run_count": run_count,
            "runs": [{"index": 1, "start": start, "end": end}] if run_count == 1 else [],
        },
        "inside_existing_revision": inside_revision,
        "revision_ancestry": [] if ancestry is None else ancestry,
        "protected_containers": protected,
        "adjacent_bookmark": False,
        "adjacent_field": False,
        "adjacent_hyperlink": False,
        "adjacent_drawing": False,
        "source_type": "PLAIN_MATH",
        "status": status,
        "expected_matches": 1,
        "resolved_style": {
            "status": "RESOLVED",
            "auto_eligible": True,
            "style": style,
        },
    }
    return row


def write_case(directory: Path, paragraphs: list[ET.Element], rows: list[dict]) -> tuple[Path, object, bytes]:
    source = directory / "source.docx"
    source_bytes = make_package(paragraphs)
    source.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    manifest = load_manifest({"source_sha256": source_sha256, "formulas": rows})
    templates = {row["id"]: make_template(row["source"]) for row in rows}
    return source, manifest, templates


def parse_document(candidate: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(candidate)) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def revision_nodes(candidate: bytes, local: str, author: str | None = None) -> list[ET.Element]:
    document = parse_document(candidate)
    nodes = document.findall(f".//w:{local}", {"w": W})
    if author is None:
        return nodes
    return [node for node in nodes if node.get(q(W, "author")) == author]


def complete_job(staged) -> object:
    job = staged.job
    for artifact in job.artifacts:
        for gate in artifact["required_gates"]:
            job = bind_gate_evidence(
                job,
                artifact["id"],
                gate,
                GateState.PASS.value,
                tool="test-gate",
                tool_version="1",
            )
    return validate_staged_artifacts(job, staged.artifact_paths)


class ApplicatorTests(unittest.TestCase):
    def test_plan_and_safe_replacement_preserve_fragments_and_apply_math_style(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "before x_i after"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined"], templates)

            self.assertEqual(plan.applied_count, 1)
            self.assertEqual(plan.session_revision_ids, (1, 2))
            self.assertEqual(job.application_plan_sha256, plan.plan_sha256)
            original = source.read_bytes()
            candidate = apply_application_plan(source, manifest, plan, templates)
            self.assertEqual(original, source.read_bytes())

            document = parse_document(candidate)
            paragraph = document.find(".//w:body/w:p", {"w": W})
            self.assertIsNotNone(paragraph)
            self.assertEqual([child.tag for child in paragraph], [q(W, "r"), q(W, "del"), q(W, "ins"), q(W, "r")])
            self.assertEqual(paragraph[0].find("w:t", {"w": W}).text, "before ")
            self.assertEqual(paragraph[1].find(".//w:delText", {"w": W}).text, "x_i")
            self.assertEqual(paragraph[3].find("w:t", {"w": W}).text, " after")
            insertion = paragraph[2]
            self.assertEqual(insertion.get(q(W, "author")), applicator.SESSION_AUTHOR)
            self.assertEqual(insertion.find("m:oMath", {"m": M}).find(".//m:t", {"m": M}).text, "x_i")
            math_properties = insertion.find(".//m:rPr", {"m": M})
            self.assertEqual(math_properties.find("m:sty", {"m": M}).get(q(M, "val")), "i")
            word_properties = math_properties.find("m:ctrlPr/w:rPr", {"m": M, "w": W})
            self.assertEqual(word_properties.find("w:rFonts", {"w": W}).get(q(W, "ascii")), "Cambria Math")
            self.assertEqual(word_properties.find("w:color", {"w": W}).get(q(W, "val")), "0000FF")
            self.assertEqual(word_properties.find("w:sz", {"w": W}).get(q(W, "val")), "24")
            settings = candidate
            with zipfile.ZipFile(io.BytesIO(settings)) as archive:
                settings_root = ET.fromstring(archive.read("word/settings.xml"))
            self.assertEqual(len(settings_root.findall("w:trackRevisions", {"w": W})), 1)

    def test_multiple_occurrences_are_applied_in_reverse_anchor_order(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            texts = ["A x_i", "B x_i"]
            rows = [make_row("first", texts[0], 1), make_row("second", texts[1], 2)]
            source, manifest, templates = write_case(directory, [make_paragraph(text) for text in texts], rows)
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            self.assertEqual(plan.session_revision_ids, (1, 2, 3, 4))
            candidate = apply_application_plan(source, manifest, plan, templates)
            self.assertEqual(len(revision_nodes(candidate, "ins", applicator.SESSION_AUTHOR)), 2)
            self.assertEqual(len(revision_nodes(candidate, "del", applicator.SESSION_AUTHOR)), 2)
            self.assertEqual(
                [node.find(".//w:t", {"w": W}).text for node in parse_document(candidate).findall(".//w:body/w:p", {"w": W})],
                ["A ", "B "],
            )

    def test_protected_multirun_and_revision_evidence_refuse_explicitly(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            texts = ["x_i", "x_i", "x_i"]
            protected = {
                "table": True,
                "bookmark": False,
                "comment_range": False,
                "hyperlink": False,
                "field": False,
                "drawing": False,
                "content_control": False,
                "embedded_object": False,
            }
            rows = [
                make_row("multi", texts[0], 1, run_count=2),
                make_row("protected", texts[1], 2, protected=protected),
                make_row("revision", texts[2], 3, inside_revision=True, ancestry=[{"id": "10"}]),
            ]
            source, manifest, templates = write_case(directory, [make_paragraph(text) for text in texts], rows)
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            self.assertEqual(plan.applied_count, 0)
            self.assertEqual([action.decision for action in plan.actions], ["REFUSE", "REFUSE", "REFUSE"])
            self.assertEqual(
                [action.terminal_status for action in plan.actions],
                [OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value] * 3,
            )
            self.assertEqual(
                [action.reason for action in plan.actions],
                ["multi_run_occurrence", "protected_container_intersection", "existing_revision_intersection"],
            )
            self.assertEqual(source.read_bytes(), apply_application_plan(source, manifest, plan, templates))

    def test_source_drift_invalidates_frozen_plan(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            changed_package = make_package([make_paragraph("z_i")])
            source.write_bytes(changed_package)
            with self.assertRaisesRegex(ApplicationError, "source_sha256"):
                apply_application_plan(source, manifest, plan, templates)

    def test_explicitly_disabled_revision_tracking_refuses_application(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            source = directory / "source.docx"
            source_bytes = make_package_with_disabled_tracking([make_paragraph("x_i")])
            source.write_bytes(source_bytes)
            manifest = load_manifest(
                {
                    "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "formulas": [make_row("safe", "x_i", 1)],
                }
            )
            templates = {"safe": make_template()}
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            with self.assertRaisesRegex(ApplicationError, "disables tracked revisions"):
                apply_application_plan(source, manifest, plan, templates)

    def test_application_plan_round_trip_and_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            plan_path = directory / "application-plan.json"
            dump_application_plan(plan, plan_path)
            self.assertEqual(plan.to_dict(), load_application_plan(plan_path).to_dict())
            tampered = plan.to_dict()
            tampered["actions"][0]["source"] = "z_i"
            with self.assertRaisesRegex(ApplicationError, "hash"):
                load_application_plan(tampered)

    def test_unapproved_template_and_session_id_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            templates["safe"]["semantic"] = {"status": "FAIL"}
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            self.assertEqual(plan.actions[0].decision, "REFUSE")
            self.assertEqual(plan.actions[0].terminal_status, OccurrenceStatus.NEEDS_REVIEW.value)
            self.assertEqual(plan.actions[0].reason, "template_not_semantically_approved")

            templates["safe"]["semantic"] = {"status": "PASS"}
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            candidate = apply_application_plan(source, manifest, plan, templates)
            with zipfile.ZipFile(io.BytesIO(candidate), "r") as archive:
                document = ET.fromstring(archive.read("word/document.xml"))
            own_insertion = document.find(".//w:ins", {"w": W})
            own_insertion.set(q(W, "id"), "99")
            changed = io.BytesIO()
            with zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", xml_bytes(document))
                with zipfile.ZipFile(io.BytesIO(candidate), "r") as original:
                    for name in original.namelist():
                        if name != "word/document.xml":
                            archive.writestr(name, original.read(name))
            with self.assertRaisesRegex(ApplicationError, "unexpected"):
                accept_session_revisions(changed.getvalue(), plan)

            with zipfile.ZipFile(io.BytesIO(candidate), "r") as archive:
                document = ET.fromstring(archive.read("word/document.xml"))
            inserted_text = document.find(".//w:ins/m:oMath//m:t", {"w": W, "m": M})
            self.assertIsNotNone(inserted_text)
            inserted_text.text = "z_i"
            changed = io.BytesIO()
            with zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", xml_bytes(document))
                with zipfile.ZipFile(io.BytesIO(candidate), "r") as original:
                    for name in original.namelist():
                        if name != "word/document.xml":
                            archive.writestr(name, original.read(name))
            with self.assertRaisesRegex(ApplicationError, "does not match the frozen OMML"):
                accept_session_revisions(changed.getvalue(), plan)

    def test_template_math_emphasis_is_preserved_without_context_emphasis(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            style = {
                "math_font": "Cambria Math",
                "math_font_policy": "CAMBRIA_MATH",
                "color": "0000FF",
                "size": "24",
            }
            source, manifest, templates = write_case(
                directory,
                [make_paragraph(text)],
                [make_row("safe", text, 1, style=style)],
            )
            templates["safe"] = make_template("x_i", math_style="i")
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            candidate = apply_application_plan(source, manifest, plan, templates)
            document = parse_document(candidate)
            style_node = document.find(".//w:ins/m:oMath//m:sty", {"w": W, "m": M})
            self.assertIsNotNone(style_node)
            self.assertEqual("i", style_node.get(q(M, "val")))

    def test_session_reject_restores_source_and_clean_accepts_only_session(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "safe x_i after"
            paragraphs = [make_paragraph(text), make_other_revision_paragraph()]
            source, manifest, templates = write_case(directory, paragraphs, [make_row("safe", text, 1)])
            _job, plan = prepare_application(source, manifest, ["redlined"], templates)
            candidate = apply_application_plan(source, manifest, plan, templates)
            self.assertEqual(len(revision_nodes(candidate, "ins", applicator.SESSION_AUTHOR)), 1)
            self.assertEqual(len(revision_nodes(candidate, "ins", "Other Reviewer")), 1)

            rejected = reject_session_revisions(candidate, plan)
            rejected_paragraph = parse_document(rejected).find(".//w:body/w:p", {"w": W})
            self.assertEqual("".join(rejected_paragraph.itertext()), text)
            self.assertEqual(len(revision_nodes(rejected, "ins", applicator.SESSION_AUTHOR)), 0)
            self.assertEqual(len(revision_nodes(rejected, "ins", "Other Reviewer")), 1)

            clean = accept_session_revisions(candidate, plan)
            clean_paragraph = parse_document(clean).find(".//w:body/w:p", {"w": W})
            self.assertEqual([child.tag for child in clean_paragraph], [q(W, "r"), q(M, "oMath"), q(W, "r")])
            self.assertEqual(len(revision_nodes(clean, "ins", applicator.SESSION_AUTHOR)), 0)
            self.assertEqual(len(revision_nodes(clean, "ins", "Other Reviewer")), 1)

    def test_stage_accounts_for_refusals_and_keeps_source_immutable(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            texts = ["safe x_i", "unsafe x_i"]
            rows = [
                make_row("safe", texts[0], 1),
                make_row("unsafe", texts[1], 2, run_count=2),
            ]
            source, manifest, templates = write_case(directory, [make_paragraph(text) for text in texts], rows)
            job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
            paths = {"redlined": directory / "stage-redlined.docx", "clean": directory / "stage-clean.docx"}
            staged = stage_application(source, manifest, job, plan, templates, paths)
            self.assertEqual(staged.job.status, DeliveryStatus.PARTIAL_REVIEW_OUTPUT.value)
            statuses = {row["id"]: row["status"] for row in staged.job.occurrences}
            self.assertEqual(statuses, {"safe": OccurrenceStatus.APPLIED.value, "unsafe": OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value})
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), staged.source_sha256)
            self.assertTrue(paths["redlined"].is_file())
            self.assertTrue(paths["clean"].is_file())

    def test_finalization_requires_all_artifacts_validated_and_gates_passed(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
            stages = {"redlined": directory / "stage-redlined.docx", "clean": directory / "stage-clean.docx"}
            staged = stage_application(source, manifest, job, plan, templates, stages)
            gated = staged.job
            for artifact in gated.artifacts:
                for gate in artifact["required_gates"]:
                    gated = bind_gate_evidence(gated, artifact["id"], gate, GateState.PASS.value, tool="test", tool_version="1")
            finals = {"redlined": directory / "final-redlined.docx", "clean": directory / "final-clean.docx"}
            with self.assertRaisesRegex(ApplicationError, "VALIDATED"):
                finalize_artifact_set(gated, stages, finals, source=source)
            validated = validate_staged_artifacts(gated, stages)
            self.assertEqual(validated.status, DeliveryStatus.COMPLETE.value)
            with self.assertRaisesRegex(ApplicationError, "source DOCX path"):
                finalize_artifact_set(
                    validated,
                    stages,
                    {"redlined": source, "clean": directory / "final-clean.docx"},
                    source=source,
                )
            self.assertTrue(all(path.is_file() for path in stages.values()))
            finalized = finalize_artifact_set(validated, stages, finals, source=source)
            self.assertEqual(finalized.status, DeliveryStatus.COMPLETE.value)
            self.assertTrue(all(artifact["state"] == "FINALIZED" for artifact in finalized.artifacts))
            self.assertTrue(all(path.is_file() for path in finals.values()))
            self.assertTrue(all(not path.exists() for path in stages.values()))

    def test_missing_native_gate_and_stale_evidence_cannot_finalize(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
            stages = {"redlined": directory / "stage-redlined.docx", "clean": directory / "stage-clean.docx"}
            staged = stage_application(source, manifest, job, plan, templates, stages)
            partial = staged.job
            redlined_artifact = next(item for item in partial.artifacts if item["logical_id"] == "redlined")
            partial = bind_gate_evidence(partial, redlined_artifact["id"], "STRUCTURAL_AUDIT", GateState.PASS.value, tool="test", tool_version="1")
            partial = bind_gate_evidence(partial, redlined_artifact["id"], "NATIVE_WORD", GateState.PASS.value, tool="test", tool_version="1")
            clean_artifact = next(item for item in partial.artifacts if item["logical_id"] == "clean")
            partial = bind_gate_evidence(partial, clean_artifact["id"], "STRUCTURAL_AUDIT", GateState.PASS.value, tool="test", tool_version="1")
            finals = {"redlined": directory / "final-redlined.docx", "clean": directory / "final-clean.docx"}
            with self.assertRaisesRegex(ApplicationError, "COMPLETE"):
                finalize_artifact_set(partial, stages, finals, source=source)

            complete = complete_job(staged)
            stale = set_artifact_content(complete, redlined_artifact["id"], "f" * 64)
            with self.assertRaisesRegex(ApplicationError, "COMPLETE"):
                finalize_artifact_set(stale, stages, finals, source=source)

    def test_atomic_two_artifact_promotion_restores_previous_set_on_failure(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
            stages = {"redlined": directory / "stage-redlined.docx", "clean": directory / "stage-clean.docx"}
            staged = stage_application(source, manifest, job, plan, templates, stages)
            validated = complete_job(staged)
            finals = {"redlined": directory / "final-redlined.docx", "clean": directory / "final-clean.docx"}
            finals["redlined"].write_bytes(b"old-redlined")
            finals["clean"].write_bytes(b"old-clean")
            redlined_stage_bytes = stages["redlined"].read_bytes()
            real_replace = applicator.os.replace

            def fail_second(source_path, target_path):
                if Path(source_path) == stages["clean"] and Path(target_path) == finals["clean"]:
                    raise OSError("simulated second promotion failure")
                return real_replace(source_path, target_path)

            with mock.patch.object(applicator.os, "replace", side_effect=fail_second):
                with self.assertRaisesRegex(ApplicationError, "prior final set was restored"):
                    finalize_artifact_set(validated, stages, finals, source=source)
            self.assertEqual(finals["redlined"].read_bytes(), b"old-redlined")
            self.assertEqual(finals["clean"].read_bytes(), b"old-clean")
            self.assertEqual(stages["redlined"].read_bytes(), redlined_stage_bytes)
            self.assertTrue(stages["redlined"].is_file())
            self.assertTrue(stages["clean"].is_file())

    def test_staged_hash_binding_rejects_mutated_candidate(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined"], templates)
            path = directory / "stage.docx"
            staged = stage_application(source, manifest, job, plan, templates, {"redlined": path})
            path.write_bytes(path.read_bytes() + b"mutation")
            with self.assertRaisesRegex(ApplicationError, "hash"):
                validate_staged_artifacts(staged.job, staged.artifact_paths)

    def test_artifact_path_set_is_exact_and_aliasing_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            text = "x_i"
            source, manifest, templates = write_case(directory, [make_paragraph(text)], [make_row("safe", text, 1)])
            job, plan = prepare_application(source, manifest, ["redlined", "clean"], templates)
            with self.assertRaisesRegex(ApplicationError, "exactly the frozen artifact set"):
                stage_application(source, manifest, job, plan, templates, {"redlined": directory / "one.docx"})

            shared = directory / "shared.docx"
            with self.assertRaisesRegex(ApplicationError, "paths must be unique"):
                stage_application(
                    source,
                    manifest,
                    job,
                    plan,
                    templates,
                    {"redlined": shared, "clean": shared},
                )


if __name__ == "__main__":
    unittest.main()
