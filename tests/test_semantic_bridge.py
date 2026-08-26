from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from word_formula_omml.canonical import canonicalize_formula
from word_formula_omml.semantic import (
    SemanticStatus,
    UnsupportedOMML,
    compare_omml_to_canonical,
    parse_omml_semantics,
    semantic_fingerprint,
)


M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def math_element(local: str, **attributes: str) -> ET.Element:
    return ET.Element(q(M, local), {q(M, key): value for key, value in attributes.items()})


def math_run(text: str, *, style: str | None = None, literal: bool = False) -> ET.Element:
    run = math_element("r")
    if style is not None or literal:
        properties = math_element("rPr")
        if style is not None:
            ET.SubElement(properties, q(M, "sty"), {q(M, "val"): style})
        if literal:
            ET.SubElement(properties, q(M, "lit"), {q(M, "val"): "1"})
        run.append(properties)
    text_node = ET.SubElement(run, q(M, "t"))
    text_node.text = text
    return run


def slot(name: str, *children: ET.Element) -> ET.Element:
    value = math_element(name)
    value.extend(children)
    return value


def omath(*children: ET.Element) -> ET.Element:
    value = math_element("oMath")
    value.extend(children)
    return value


def script(base: ET.Element, *, sub: ET.Element | None = None, sup: ET.Element | None = None) -> ET.Element:
    if sub is not None and sup is not None:
        local = "sSubSup"
    elif sub is not None:
        local = "sSub"
    elif sup is not None:
        local = "sSup"
    else:
        raise AssertionError("a script needs a subscript or superscript")
    value = math_element(local)
    value.append(slot("e", base))
    if sub is not None:
        value.append(slot("sub", sub))
    if sup is not None:
        value.append(slot("sup", sup))
    return value


def fraction(numerator: ET.Element, denominator: ET.Element) -> ET.Element:
    value = math_element("f")
    value.extend((slot("num", numerator), slot("den", denominator)))
    return value


def root(radicand: ET.Element, degree: ET.Element | None = None) -> ET.Element:
    value = math_element("rad")
    if degree is not None:
        value.append(slot("deg", degree))
    value.append(slot("e", radicand))
    return value


def delimiter(left: str, right: str, *body: ET.Element) -> ET.Element:
    value = math_element("d")
    properties = math_element("dPr")
    ET.SubElement(properties, q(M, "begChr"), {q(M, "val"): left})
    ET.SubElement(properties, q(M, "endChr"), {q(M, "val"): right})
    value.extend((properties, slot("e", *body)))
    return value


def load_script(path: str):
    specification = importlib.util.spec_from_file_location("generator_for_test", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class SemanticBridgeTests(unittest.TestCase):
    def test_supported_structures_round_trip_to_w3a_ir(self):
        plain_script = script(math_run("x"), sub=math_run("i"), sup=math_run("2"))
        self.assertEqual(
            canonicalize_formula("x_i^2"),
            parse_omml_semantics(omath(plain_script)),
        )

        grouped = script(math_run("i"), sup=math_run("2"))
        grouped_base = script(math_run("x"), sub=grouped)
        self.assertEqual(
            canonicalize_formula(r"x_{i^2}"),
            parse_omml_semantics(omath(grouped_base)),
        )

        value = fraction(
            plain_script,
            root(math_run("y")),
        )
        result = compare_omml_to_canonical(
            omath(value),
            canonicalize_formula(r"\frac{x_i^2}{\sqrt{y}}"),
        )
        self.assertEqual(SemanticStatus.PASS.value, result.status)
        self.assertTrue(result.auto_eligible)

    def test_operator_root_delimiter_accent_and_style_structures(self):
        exponent = script(math_run("10"), sup=omath(math_run("-"), math_run("3")))
        operator_formula = omath(
            math_run("x"),
            math_run("\u2265"),
            math_run("y"),
            math_run("\u00b1"),
            exponent,
        )
        result = compare_omml_to_canonical(
            operator_formula,
            canonicalize_formula("x >= y +/- 10^-3"),
            source_latex="x >= y +/- 10^-3",
        )
        self.assertEqual(SemanticStatus.PASS.value, result.status)

        unicode_formula = omath(math_run("\u03b1"), math_run("\u2264"), math_run("\u03b2"), math_run("\u00b1"), math_run("\u03b3"))
        result = compare_omml_to_canonical(
            unicode_formula,
            canonicalize_formula("\u03b1 \u2264 \u03b2 \u00b1 \u03b3"),
            source_latex="\u03b1 \u2264 \u03b2 \u00b1 \u03b3",
        )
        self.assertEqual(SemanticStatus.PASS.value, result.status)

        interval = delimiter("(", "]", math_run("0,1"))
        result = compare_omml_to_canonical(interval, canonicalize_formula("(0,1]"))
        self.assertEqual(SemanticStatus.PASS.value, result.status)

        accent = math_element("acc")
        properties = math_element("accPr")
        ET.SubElement(properties, q(M, "chr"), {q(M, "val"): "^"})
        accent.extend((properties, slot("e", math_run("x"))))
        self.assertEqual(
            {"kind": "accent", "accent": "hat", "base": "x"},
            parse_omml_semantics(omath(accent)),
        )

        self.assertEqual(
            {"kind": "styled", "style": "bold", "value": "x"},
            parse_omml_semantics(omath(math_run("x", style="b"))),
        )
        self.assertEqual(
            {"kind": "roman", "text": "Total"},
            parse_omml_semantics(omath(math_run("Total", literal=True))),
        )

    def test_mutated_semantics_are_rejected(self):
        expected_script = canonicalize_formula("x_i^2")
        grouped = script(math_run("x"), sub=script(math_run("i"), sup=math_run("2")))
        result = compare_omml_to_canonical(omath(grouped), expected_script)
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)
        self.assertFalse(result.auto_eligible)

        altered_operator = omath(math_run("x"), math_run("\u2264"), math_run("y"))
        result = compare_omml_to_canonical(altered_operator, canonicalize_formula("x >= y"))
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)

        altered_operand = omath(math_run("z"), math_run("\u2265"), math_run("y"), math_run("\u00b1"), math_run("1"))
        result = compare_omml_to_canonical(
            altered_operand,
            canonicalize_formula("x >= y +/- 1"),
            source_latex="x >= y +/- 1",
        )
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)

        compact_expected = canonicalize_formula("x >= y +/- 10^-3")
        without_source = compare_omml_to_canonical(altered_operand, compact_expected)
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, without_source.status)

        conflicting_source = compare_omml_to_canonical(
            altered_operand,
            compact_expected,
            source_latex="x <= y +/- 10^-3",
        )
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, conflicting_source.status)

        altered_fraction = fraction(math_run("y"), math_run("x"))
        result = compare_omml_to_canonical(altered_fraction, canonicalize_formula(r"\frac{x}{y}"))
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)

        altered_root = root(math_run("z"))
        result = compare_omml_to_canonical(altered_root, canonicalize_formula(r"\sqrt{y}"))
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)

        altered_delimiter = delimiter("[", "]", math_run("0,1"))
        result = compare_omml_to_canonical(altered_delimiter, canonicalize_formula("(0,1]"))
        self.assertEqual(SemanticStatus.MISMATCH.value, result.status)

    def test_unsupported_and_malformed_structures_fail_closed(self):
        matrix = omath(math_element("m"))
        result = compare_omml_to_canonical(matrix, "x")
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, result.status)
        self.assertFalse(result.auto_eligible)
        self.assertIn("unsupported_omml_structure:m", result.reason)

        with self.assertRaises(UnsupportedOMML):
            parse_omml_semantics(omath(math_element("nary")))

        missing_run_text = math_element("r")
        result = compare_omml_to_canonical(omath(missing_run_text), "x")
        self.assertEqual(SemanticStatus.INVALID.value, result.status)
        self.assertFalse(result.auto_eligible)

        unknown_canonical = compare_omml_to_canonical(omath(math_run("x")), {"kind": "matrix", "rows": []})
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, unknown_canonical.status)

        incomplete_sequence = compare_omml_to_canonical(omath(math_run("x")), {"kind": "operator_sequence"})
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, incomplete_sequence.status)

        conflicting_style = math_run("x", style="b", literal=True)
        result = compare_omml_to_canonical(omath(conflicting_style), "x")
        self.assertEqual(SemanticStatus.UNSUPPORTED.value, result.status)

    def test_comparison_and_fingerprints_are_deterministic(self):
        formula = omath(script(math_run("x"), sub=math_run("i"), sup=math_run("2")))
        expected = canonicalize_formula("x_i^2")
        first = compare_omml_to_canonical(formula, expected)
        second = compare_omml_to_canonical(formula, expected)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(semantic_fingerprint(first), semantic_fingerprint(second))

    def test_minimal_generator_is_compatible_and_semantically_gated(self):
        root_path = Path(__file__).resolve().parents[1]
        generator = load_script(str(root_path / "scripts" / "generate_omml_library.py"))
        diagnostics = [False]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text('[{"id":"F-001","latex":"x_i","layout":"inline"}]', encoding="utf-8")
            self.assertEqual(
                [{"id": "F-001", "latex": "x_i", "layout": "inline"}],
                generator.load_formulas(manifest),
            )
            enriched = generator.load_formulas(manifest, include_semantics=True)
            self.assertEqual(canonicalize_formula("x_i"), enriched[0]["canonical"])

            document = ET.Element(q(W, "document"))
            body = ET.SubElement(document, q(W, "body"))
            marker = ET.SubElement(body, q(W, "p"))
            marker_run = ET.SubElement(marker, q(W, "r"))
            marker_text = ET.SubElement(marker_run, q(W, "t"))
            marker_text.text = "OMML_ID:F-001"
            equation = ET.SubElement(body, q(W, "p"))
            equation.append(omath(script(math_run("x"), sub=math_run("i"))))
            library = Path(directory) / "library.docx"
            with zipfile.ZipFile(library, "w") as archive:
                archive.writestr("word/document.xml", ET.tostring(document, encoding="utf-8"))
            entries = generator.inspect_library(library, enriched)
            self.assertEqual("SEMANTICALLY_VALIDATED", entries[0]["status"])
            self.assertTrue(entries[0]["auto_eligible"])
            self.assertEqual(SemanticStatus.PASS.value, entries[0]["semantic"]["status"])

    def test_audit_recomputes_shared_semantics_and_detects_stale_candidate_hash(self):
        root_path = Path(__file__).resolve().parents[1]
        audit = load_script(str(root_path / "scripts" / "audit_docx_formulas.py"))
        formula = script(math_run("x"), sub=math_run("i"))
        document = ET.Element(q(W, "document"))
        body = ET.SubElement(document, q(W, "body"))
        marker = ET.SubElement(body, q(W, "p"))
        marker_run = ET.SubElement(marker, q(W, "r"))
        marker_text = ET.SubElement(marker_run, q(W, "t"))
        marker_text.text = "OMML_ID:F-001"
        paragraph = ET.SubElement(body, q(W, "p"))
        paragraph.append(omath(formula))
        equation_xml = ET.tostring(paragraph.findall(".//m:oMath", {"m": M})[0], encoding="utf-8")
        expected = canonicalize_formula("x_i")
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "library.index.json"
            index.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "formulas": [
                            {
                                "id": "F-001",
                                "latex": "x_i",
                                "canonical": expected,
                                "marker_paragraph": 1,
                                "equation_paragraph": 2,
                                "omml_sha256": hashlib.sha256(equation_xml).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            results, errors = audit.audit_semantic_index(document, index)
            self.assertEqual([], errors)
            self.assertEqual(SemanticStatus.PASS.value, results[0]["status"])

            equation_run = paragraph.findall(".//m:t", {"m": M})[1]
            equation_run.text = "j"
            results, errors = audit.audit_semantic_index(document, index)
            self.assertEqual(SemanticStatus.MISMATCH.value, results[0]["status"])
            self.assertTrue(any("candidate hash" in error for error in errors))

    def test_audit_rejects_incomplete_semantic_index_identity(self):
        root_path = Path(__file__).resolve().parents[1]
        audit = load_script(str(root_path / "scripts" / "audit_docx_formulas.py"))
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "library.index.json"
            index.write_text(
                json.dumps({"schema_version": True, "formulas": [{"canonical": "x"}]}),
                encoding="utf-8",
            )
            document = ET.Element(q(W, "document"))
            with self.assertRaises(RuntimeError):
                audit.audit_semantic_index(document, index)

            index.write_text(
                json.dumps({"schema_version": 1, "formulas": [{"canonical": "x"}]}),
                encoding="utf-8",
            )
            body = ET.SubElement(document, q(W, "body"))
            marker = ET.SubElement(body, q(W, "p"))
            marker_run = ET.SubElement(marker, q(W, "r"))
            marker_text = ET.SubElement(marker_run, q(W, "t"))
            marker_text.text = "OMML_ID:index-1"
            equation = ET.SubElement(body, q(W, "p"))
            equation.append(omath(math_run("x")))
            equation_xml = ET.tostring(equation.findall(".//m:oMath", {"m": M})[0], encoding="utf-8")
            index.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "formulas": [
                            {
                                "canonical": "x",
                                "marker_paragraph": 1,
                                "equation_paragraph": 2,
                                "omml_sha256": hashlib.sha256(equation_xml).hexdigest(),
                                "latex": "x",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            results, errors = audit.audit_semantic_index(document, index)
            self.assertEqual(SemanticStatus.PASS.value, results[0]["status"])
            self.assertTrue(any("invalid id" in error for error in errors))

    def test_generator_publication_rolls_back_on_sidecar_failure(self):
        root_path = Path(__file__).resolve().parents[1]
        generator = load_script(str(root_path / "scripts" / "generate_omml_library.py"))
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            output = directory_path / "library.docx"
            index = directory_path / "library.index.json"
            staging_output = directory_path / "staging.docx"
            staging_index = directory_path / "staging.json"
            output.write_bytes(b"previous-library")
            index.write_bytes(b"previous-index")
            staging_output.write_bytes(b"new-library")
            staging_index.write_bytes(b"new-index")
            real_replace = generator.os.replace

            def fail_index(source, target):
                if Path(source) == staging_index and Path(target) == index:
                    raise OSError("simulated sidecar publication failure")
                return real_replace(source, target)

            with mock.patch.object(generator.os, "replace", side_effect=fail_index):
                with self.assertRaises(OSError):
                    generator._publish_pair(staging_output, staging_index, output, index)
            self.assertEqual(b"previous-library", output.read_bytes())
            self.assertEqual(b"previous-index", index.read_bytes())

    def test_pandoc_preflight_failure_is_actionable(self):
        root_path = Path(__file__).resolve().parents[1]
        generator = load_script(str(root_path / "scripts" / "generate_omml_library.py"))
        with self.assertRaises(generator.PandocError) as context:
            generator.pandoc_api_version("definitely-not-a-pandoc-executable")
        self.assertIn("Pandoc preflight failed", str(context.exception))

    def test_generator_publishes_only_after_semantic_gate(self):
        root_path = Path(__file__).resolve().parents[1]
        generator = load_script(str(root_path / "scripts" / "generate_omml_library.py"))
        diagnostics = [False]

        def fake_run(command, **kwargs):
            if "--from=markdown" in command:
                return generator.subprocess.CompletedProcess(command, 0, stdout=b'{"pandoc-api-version":[1,23]}', stderr=b"")
            output_argument = next(item for item in command if item.startswith("--output="))
            output = Path(output_argument.split("=", 1)[1])
            document = ET.Element(q(W, "document"))
            body = ET.SubElement(document, q(W, "body"))
            marker = ET.SubElement(body, q(W, "p"))
            marker_run = ET.SubElement(marker, q(W, "r"))
            marker_text = ET.SubElement(marker_run, q(W, "t"))
            marker_text.text = "OMML_ID:F-001"
            equation = ET.SubElement(body, q(W, "p"))
            equation.append(omath(script(math_run("x"), sub=math_run("i"))))
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("word/document.xml", ET.tostring(document, encoding="utf-8"))
            return generator.subprocess.CompletedProcess(
                command,
                0,
                stdout=b"",
                stderr=b"warning: semantic review required" if diagnostics[0] else b"",
            )

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text('[{"id":"F-001","latex":"x_i","layout":"inline"}]', encoding="utf-8")
            output = Path(directory) / "library.docx"
            index = Path(directory) / "library.index.json"
            with mock.patch.object(generator.subprocess, "run", side_effect=fake_run), mock.patch.object(
                sys, "argv", ["generate_omml_library.py", str(manifest), str(output), "--index", str(index), "--pandoc", "fake"]
            ):
                self.assertEqual(0, generator.main())
            self.assertTrue(output.is_file())
            self.assertEqual("SEMANTICALLY_VALIDATED", json.loads(index.read_text(encoding="utf-8"))["formulas"][0]["status"])

            output.write_bytes(b"validated-old-output")
            diagnostics[0] = True
            with mock.patch.object(generator.subprocess, "run", side_effect=fake_run), mock.patch.object(
                sys, "argv", ["generate_omml_library.py", str(manifest), str(output), "--index", str(index), "--pandoc", "fake"]
            ):
                with self.assertRaises(generator.GenerationError):
                    generator.main()
            self.assertEqual(b"validated-old-output", output.read_bytes())


if __name__ == "__main__":
    unittest.main()
