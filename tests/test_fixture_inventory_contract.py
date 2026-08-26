from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.fixtures.docx_fixture import load_expectations, write_fixture
from word_formula_omml.inventory import classify_formula_text, inventory_docx


class FixtureInventoryContractTests(unittest.TestCase):
    def test_w1_interval_expectation_matches_production_inventory(self):
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "adversarial-v1.docx"
            write_fixture(source)
            before = source.read_bytes()
            manifest = inventory_docx(source)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(hashlib.sha256(before).hexdigest(), manifest.source_sha256)

            expected = next(
                row for row in load_expectations()["formulas"] if row["id"] == "interval"
            )
            matches = [
                row
                for row in manifest.formulas
                if row.get("package_part") == "word/document.xml"
                and row.get("story") == "main"
                and row.get("raw_source") == expected["source"]
                and row.get("source_type") == expected["source_type"]
            ]
            self.assertEqual(1, len(matches), matches)
            actual = matches[0]
            self.assertEqual(expected["paragraph"], actual["paragraph"])
            self.assertTrue((actual.get("anchor_before") or "").endswith(expected["anchor_before"]))
            self.assertEqual("current", actual["extensions"]["inventory"]["source_view"])
            self.assertEqual(1, actual["run_boundaries"]["run_count"])
            self.assertEqual("DISCOVERED", actual["status"])

    def test_interval_detector_stays_conservative_for_prose(self):
        self.assertEqual("PLAIN_MATH", classify_formula_text("(0, 1]"))
        self.assertIsNone(classify_formula_text("(first, second)"))
        self.assertIsNone(classify_formula_text("The values were 0, 1 in order."))


if __name__ == "__main__":
    unittest.main()
