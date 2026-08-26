from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.fixtures.docx_fixture import write_fixture
from word_formula_omml.canonical import CanonicalError, canonical_equal, canonicalize_formula
from word_formula_omml.contract import OccurrenceStatus
from word_formula_omml.inventory import inventory_docx
from word_formula_omml.recovery import recover_formula, recover_manifest, recovery_fingerprint


EXPECTATIONS = json.loads((Path(__file__).parent / "fixtures" / "expectations.json").read_text(encoding="utf-8"))


class RecoveryTests(unittest.TestCase):
    def find_row(self, manifest, raw_source: str, *, anchor: str | None = None, part: str = "word/document.xml"):
        rows = [
            row
            for row in manifest.formulas
            if row.get("package_part") == part
            and row.get("raw_source") == raw_source
            and (anchor is None or anchor in (row.get("anchor_before") or ""))
        ]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def test_fixture_supported_families_emit_expected_ir_and_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            write_fixture(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            recovered = recover_manifest(inventory_docx(source))
            self.assertEqual(original_hash, hashlib.sha256(source.read_bytes()).hexdigest())

        expected = EXPECTATIONS["extensions"]["canonical_semantics"]
        cases = (
            (r"\frac{x_i^2}{\sqrt{y}}", None, "raw-latex"),
            ("x_i^2", "Plain scripts", "plain-scripts"),
            ("x >= y +/- 10^-3", None, "plain-operators"),
            ("α ≤ β ± γ", None, "unicode-operators"),
            ("x_i^2", "Semantic trap A", "semantic-trap-a"),
            ("x_{i^2}", "Semantic trap B", "semantic-trap-b"),
            ("(0, 1]", "Interval", "interval"),
        )
        for raw_source, anchor, expectation_id in cases:
            row = self.find_row(recovered, raw_source, anchor=anchor)
            if expectation_id == "semantic-trap-a":
                # The same source spelling is intentionally represented by
                # the ordinary script IR; its dangerous near-match is proven
                # by the distinct grouped_exponent form below.
                self.assertEqual("script", row["canonical"]["kind"])
            else:
                self.assertTrue(canonical_equal(row["canonical"], expected[expectation_id]), row)
            self.assertEqual("RECOVERED", row["status"])
            self.assertTrue(row["extensions"]["recovery"]["auto_eligible"])
            self.assertEqual(row["target_layout"], row["layout"])
    def test_safe_normalization_records_each_change_in_order(self):
        result = recover_formula("x >= y +/- 10^-3", source_type="PLAIN_MATH")
        self.assertEqual("x \\ge y \\pm 10^{-3}", result.normalized_latex)
        self.assertEqual("HIGH", result.confidence)
        self.assertEqual("RECOVERED", result.status)
        self.assertEqual(3, len(result.transformations))
        self.assertEqual("x >= y +/- 10^-3", result.transformations[0]["before"])
        self.assertEqual(result.normalized_latex, result.transformations[-1]["after"])
        self.assertTrue(all(item["reason"] and item["evidence_source"] for item in result.transformations))

    def test_script_grouping_and_operator_direction_are_distinct(self):
        plain = recover_formula("x_i^2", source_type="PLAIN_MATH")
        grouped = recover_formula(r"x_{i^2}", source_type="RAW_LATEX")
        self.assertNotEqual(plain.canonical, grouped.canonical)
        self.assertEqual("script", plain.canonical["kind"])
        self.assertEqual("grouped_exponent", grouped.canonical["kind"])

        greater = recover_formula("x >= y +/- 10^-3", source_type="PLAIN_MATH")
        lesser = recover_formula("x <= y +/- 10^-3", source_type="PLAIN_MATH")
        self.assertNotEqual(greater.canonical, lesser.canonical)
        self.assertEqual([">=", "+/-"], greater.canonical["operators"])
        self.assertEqual(["<=", "+/-"], lesser.canonical["operators"])

        self.assertEqual({"kind": "unary_minus", "operand": "x"}, canonicalize_formula("-x"))
        self.assertEqual(
            {"kind": "subtraction", "left": "x", "right": "y"},
            canonicalize_formula("x-y"),
        )
        self.assertEqual(
            {"kind": "function_call", "name": "f", "argument": "x"},
            canonicalize_formula("f(x)"),
        )
        self.assertEqual(
            {"kind": "script", "base": {"kind": "styled", "style": "bold", "value": "x"}, "subscript": "i"},
            canonicalize_formula(r"\mathbf{x}_i"),
        )

    def test_context_gated_lost_escape_and_corruption(self):
        lost = recover_formula("frac{x}{y}", source_type="PARTIAL_LATEX")
        self.assertEqual("NEEDS_REVIEW", lost.status)
        self.assertIsNone(lost.canonical)
        self.assertIn("repair_requires_authoritative_evidence", lost.ambiguity)

        repaired = recover_formula(
            "frac{x}{y}",
            source_type="PARTIAL_LATEX",
            context={"math_intent": True, "allow_lost_escape": True},
        )
        self.assertEqual(r"\frac{x}{y}", repaired.normalized_latex)
        self.assertEqual("HIGH", repaired.confidence)
        self.assertEqual("RECOVERED", repaired.status)

        corrupted = recover_formula("x â‰¤ y", source_type="CORRUPTED_TEXT")
        self.assertEqual("NEEDS_REVIEW", corrupted.status)
        corrected = recover_formula(
            "x â‰¤ y",
            source_type="CORRUPTED_TEXT",
            evidence={"source": "original_latex", "latex": r"x \le y"},
        )
        self.assertEqual(r"x \le y", corrected.normalized_latex)
        self.assertEqual("HIGH", corrected.confidence)
        self.assertEqual("RECOVERED", corrected.status)
        self.assertEqual("adopt_evidence_representation", corrected.transformations[0]["rule"])

    def test_evidence_precedence_conflict_is_explicit_and_fail_closed(self):
        result = recover_formula(
            "x_i^2",
            source_type="PLAIN_MATH",
            evidence={"source": "author_approved", "latex": r"x_{i^2}", "approved": True},
        )
        self.assertEqual("NEEDS_REVIEW", result.status)
        self.assertEqual("REVIEW_REQUIRED", result.confidence)
        self.assertIn("evidence_conflict", result.ambiguity)
        self.assertEqual("grouped_exponent", result.canonical["kind"])
        selected = [item for item in result.evidence if item["selected"]]
        self.assertEqual(1, len(selected))
        self.assertEqual("author_approved", selected[0]["source"])

        same = recover_formula(
            "x_i^2",
            source_type="PLAIN_MATH",
            evidence={"source": "author_approved", "latex": "x_i^2", "approved": True},
        )
        self.assertEqual("APPROVED", same.status)
        self.assertEqual("AUTHORITATIVE", same.confidence)

    def test_prose_and_unsupported_syntax_do_not_enter_automatic_path(self):
        prose = recover_formula("alpha")
        self.assertEqual("UNRECOVERABLE", prose.confidence)
        self.assertEqual("REFUSED", prose.status)
        self.assertIsNone(prose.canonical)

        with self.assertRaises(CanonicalError):
            canonicalize_formula(r"\unknowncommand{x}")
        unsupported = recover_formula(r"\unknowncommand{x}", source_type="RAW_LATEX")
        self.assertEqual("NEEDS_REVIEW", unsupported.status)
        self.assertFalse(unsupported.auto_eligible)

        approved_unknown = recover_formula(
            "u ∈ v",
            source_type="UNKNOWN_FORMULA",
            evidence={"source": "author_approved", "latex": r"u \in v", "approved": True},
        )
        self.assertEqual("APPROVED", approved_unknown.status)
        self.assertEqual("AUTHORITATIVE", approved_unknown.confidence)

        unusable_high = recover_formula(
            "x_i^2",
            source_type="PLAIN_MATH",
            evidence={"source": "original_latex", "latex": r"\unknown{x}"},
        )
        self.assertEqual("NEEDS_REVIEW", unusable_high.status)
        self.assertIn("higher_ranked_evidence_unusable", unusable_high.ambiguity)

    def test_manifest_recovery_preserves_structural_refusals_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            write_fixture(source)
            manifest = inventory_docx(source)
            first = recover_manifest(manifest)
            second = recover_manifest(copy.deepcopy(first.to_dict()))

        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            recovery_fingerprint(recover_formula("x_i^2", source_type="PLAIN_MATH")),
            recovery_fingerprint(recover_formula("x_i^2", source_type="PLAIN_MATH")),
        )
        protected = self.find_row(first, "x_i^2", anchor="Multi-run")
        self.assertEqual(OccurrenceStatus.NEEDS_SPECIAL_HANDLER.value, protected["status"])
        self.assertNotIn("canonical", protected)
        native = self.find_row(first, "x", anchor=None)
        self.assertEqual(OccurrenceStatus.PRESERVED.value, native["status"])


if __name__ == "__main__":
    unittest.main()
