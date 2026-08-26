from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.fixtures.docx_fixture import build_fixture_package, write_fixture
from word_formula_omml.contract import dump_manifest, load_manifest
from word_formula_omml.inventory import (
    InventoryError,
    classify_formula_text,
    inventory_docx,
    write_inventory,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def write_package(path: Path, package: dict[str, bytes], *, duplicate: tuple[str, bytes] | None = None) -> None:
    with path.open("wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            entries = sorted(package.items())
            if duplicate is not None:
                entries.append(duplicate)
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)


class InventoryTests(unittest.TestCase):
    def make_source(self, directory: str) -> Path:
        source = Path(directory) / "source.docx"
        write_fixture(source)
        return source

    def find_row(
        self,
        manifest,
        raw_source: str,
        *,
        package_part: str = "word/document.xml",
        anchor_before: str | None = None,
        source_view: str | None = None,
    ) -> dict:
        matches = [
            row
            for row in manifest.formulas
            if row.get("package_part") == package_part
            and row.get("raw_source") == raw_source
            and (anchor_before is None or anchor_before in (row.get("anchor_before") or ""))
            and (
                source_view is None
                or row.get("extensions", {}).get("inventory", {}).get("source_view") == source_view
            )
        ]
        self.assertEqual(1, len(matches), f"expected one row for {raw_source!r}, got {matches}")
        return matches[0]

    def test_fixture_inventory_is_read_only_and_package_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(directory)
            before = source.read_bytes()
            before_sha256 = hashlib.sha256(before).hexdigest()
            manifest = inventory_docx(source)
            after = source.read_bytes()

            self.assertEqual(before, after)
            self.assertEqual(before_sha256, manifest.source_sha256)
            inventory = manifest.extensions["inventory"]
            self.assertEqual(len(manifest.formulas), inventory["candidate_count"])
            self.assertEqual({"current": 30, "deleted": 1, "omml": 2}, inventory["source_views"])
            with zipfile.ZipFile(source) as archive:
                self.assertEqual(
                    inventory["part_sha256"]["word/media/image1.png"],
                    hashlib.sha256(archive.read("word/media/image1.png")).hexdigest(),
                )
                self.assertIsNone(archive.testzip())
            relationships = inventory["relationships"]["word/document.xml"]
            self.assertEqual("media/image1.png", relationships["rIdImage"]["target"])
            self.assertTrue(relationships["rIdHyperlink"]["external"])

    def test_inventory_ids_and_manifest_round_trip_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(directory)
            first = inventory_docx(source)
            second = inventory_docx(source)
            self.assertEqual(
                [row["id"] for row in first.formulas],
                [row["id"] for row in second.formulas],
            )
            self.assertEqual(first.manifest_id, second.manifest_id)

            path = Path(directory) / "inventory.json"
            dump_manifest(first, path)
            restored = load_manifest(path)
            self.assertEqual(first.to_dict(), restored.to_dict())
            self.assertEqual(first.manifest_sha256, restored.manifest_sha256)

    def test_source_classes_and_conservative_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = inventory_docx(self.make_source(directory))

            self.assertEqual("RAW_LATEX", self.find_row(manifest, r"\frac{x_i^2}{\sqrt{y}}")["source_type"])
            self.assertEqual("PLAIN_MATH", self.find_row(manifest, "x_i^2", anchor_before="Plain scripts")["source_type"])
            self.assertEqual(
                "PLAIN_MATH",
                self.find_row(manifest, "x >= y +/- 10^-3")["source_type"],
            )
            self.assertEqual("UNICODE_MATH", self.find_row(manifest, "α ≤ β ± γ")["source_type"])
            self.assertEqual("PARTIAL_LATEX", self.find_row(manifest, r"\frac{x}{y")["source_type"])
            self.assertEqual("PARTIAL_LATEX", self.find_row(manifest, "frac{x}{y}")["source_type"])
            self.assertEqual("CORRUPTED_TEXT", self.find_row(manifest, "x â‰¤ y")["source_type"])
            self.assertEqual(
                "PLAIN_MATH",
                self.find_row(manifest, "x_i^2", anchor_before="Multi-run")["source_type"],
            )
            self.assertEqual("EQ_FIELD", self.find_row(manifest, "x2")["source_type"])
            self.assertEqual(
                "EMBEDDED_EQUATION_OBJECT",
                self.find_row(manifest, "Embedded legacy equation object")["source_type"],
            )
            object_row = self.find_row(manifest, "Embedded legacy equation object")
            self.assertIn("Embedded legacy equation object", object_row["anchor_before"])
            self.assertEqual({"rIdOle", "rIdOlePreview"}, set(object_row["extensions"]["inventory"]["relationship_ids"]))
            hyperlink_row = self.find_row(manifest, "a^2", anchor_before="Link")
            self.assertEqual("Hyperlink", hyperlink_row["style_snapshot"]["character_style"])
            self.assertEqual(["rIdHyperlink"], hyperlink_row["extensions"]["inventory"]["relationship_ids"])
            self.assertEqual(2, sum(row["source_type"] == "EXISTING_OMML" for row in manifest.formulas))
            self.assertEqual(
                "display",
                next(row for row in manifest.formulas if row["source_type"] == "EXISTING_OMML" and row["layout"] == "display")[
                    "layout"
                ],
            )

            review_types = {"PARTIAL_LATEX", "CORRUPTED_TEXT"}
            for row in manifest.formulas:
                self.assertEqual("REVIEW_REQUIRED", row["confidence"])
                if row["source_type"] in review_types:
                    self.assertEqual("NEEDS_REVIEW", row["status"])
            self.assertTrue(all(row["status"] != "APPROVED" for row in manifest.formulas))

    def test_eq_field_result_is_one_specialized_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            package = build_fixture_package()
            document = ET.fromstring(package["word/document.xml"])
            field = next(
                node
                for node in document.findall(".//w:fldSimple", {"w": W})
                if "EQ" in (node.get(f"{{{W}}}instr") or "").split()
            )
            result = field.find(".//w:t", {"w": W})
            self.assertIsNotNone(result)
            result.text = "x_i"
            package["word/document.xml"] = ET.tostring(document, encoding="UTF-8", xml_declaration=True)

            source = Path(directory) / "eq-result.docx"
            write_package(source, package)
            manifest = inventory_docx(source)
            matches = [
                row
                for row in manifest.formulas
                if row.get("package_part") == "word/document.xml"
                and row.get("anchor_before") == "EQ field: "
                and row.get("raw_source") == "x_i"
            ]
            self.assertEqual(1, len(matches))
            self.assertEqual("EQ_FIELD", matches[0]["source_type"])
            self.assertEqual("NEEDS_SPECIAL_HANDLER", matches[0]["status"])

    def test_current_and_deleted_surfaces_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = inventory_docx(self.make_source(directory))
            current = self.find_row(manifest, "x_i", anchor_before="Other revision", source_view="current")
            deleted = self.find_row(manifest, "y_i", anchor_before="Deleted", source_view="deleted")

            self.assertTrue(current["inside_existing_revision"])
            self.assertEqual({"kind": "ins", "id": "101", "author": "Other Reviewer", "deleted": False}, current["revision_ancestry"][0])
            self.assertTrue(deleted["inside_existing_revision"])
            self.assertEqual({"kind": "del", "id": "102", "author": "Other Reviewer", "deleted": True}, deleted["revision_ancestry"][0])
            self.assertNotIn("y_i", [row["raw_source"] for row in manifest.formulas if row["extensions"]["inventory"]["source_view"] == "current"])
            self.assertNotEqual(current["extensions"]["inventory"]["source_view"], deleted["extensions"]["inventory"]["source_view"])

    def test_candidate_crossing_hidden_revision_is_refused_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            package = build_fixture_package()
            document = ET.fromstring(package["word/document.xml"])
            body = document.find(f"{{{W}}}body")
            self.assertIsNotNone(body)
            paragraph = ET.SubElement(body, f"{{{W}}}p")
            first_run = ET.SubElement(paragraph, f"{{{W}}}r")
            first_text = ET.SubElement(first_run, f"{{{W}}}t")
            first_text.text = "Cross x_"
            deletion = ET.SubElement(
                paragraph,
                f"{{{W}}}del",
                {f"{{{W}}}id": "501", f"{{{W}}}author": "Other Reviewer"},
            )
            deleted_run = ET.SubElement(deletion, f"{{{W}}}r")
            deleted_text = ET.SubElement(deleted_run, f"{{{W}}}delText")
            deleted_text.text = "z"
            last_run = ET.SubElement(paragraph, f"{{{W}}}r")
            last_text = ET.SubElement(last_run, f"{{{W}}}t")
            last_text.text = "i"
            package["word/document.xml"] = ET.tostring(document, encoding="UTF-8", xml_declaration=True)

            source = Path(directory) / "cross-revision.docx"
            write_package(source, package)
            manifest = inventory_docx(source)
            row = self.find_row(manifest, "x_i", anchor_before="Cross ")

            self.assertEqual("NEEDS_SPECIAL_HANDLER", row["status"])
            self.assertTrue(row["inside_existing_revision"])
            self.assertEqual(
                {
                    "kind": "del",
                    "id": "501",
                    "author": "Other Reviewer",
                    "deleted": True,
                    "omitted": True,
                },
                row["revision_ancestry"][0],
            )
            self.assertTrue(row["extensions"]["inventory"]["crosses_omitted_revision"])

    def test_candidate_crossing_non_text_run_boundary_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            package = build_fixture_package()
            document = ET.fromstring(package["word/document.xml"])
            body = document.find(f"{{{W}}}body")
            self.assertIsNotNone(body)
            paragraph = ET.SubElement(body, f"{{{W}}}p")
            run = ET.SubElement(paragraph, f"{{{W}}}r")
            first_text = ET.SubElement(run, f"{{{W}}}t")
            first_text.text = "Inline x_"
            ET.SubElement(run, f"{{{W}}}tab")
            last_text = ET.SubElement(run, f"{{{W}}}t")
            last_text.text = "i"
            package["word/document.xml"] = ET.tostring(document, encoding="UTF-8", xml_declaration=True)

            source = Path(directory) / "cross-boundary.docx"
            write_package(source, package)
            manifest = inventory_docx(source)
            row = self.find_row(manifest, "x_i", anchor_before="Inline ")

            self.assertEqual("NEEDS_SPECIAL_HANDLER", row["status"])
            self.assertTrue(row["extensions"]["inventory"]["crosses_non_text_boundary"])
            self.assertEqual(1, len(row["extensions"]["inventory"]["boundary_node_paths"]))

    def test_repeated_occurrences_preserve_style_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = inventory_docx(self.make_source(directory))
            repeated = [
                row
                for row in manifest.formulas
                if row.get("raw_source") == "x_i"
                and row.get("status") == "DISCOVERED"
                and row.get("package_part") == "word/document.xml"
            ]
            self.assertEqual(3, len(repeated))
            self.assertEqual({"000000", "0000FF", "FF0000"}, {row["style_snapshot"]["color"] for row in repeated})
            self.assertEqual({"20", "24", "28"}, {row["style_snapshot"]["size"] for row in repeated})
            self.assertEqual(3, len({row["id"] for row in repeated}))
            self.assertTrue(all(row["run_boundaries"]["run_count"] == 1 for row in repeated))

    def test_protected_context_and_story_metadata_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = inventory_docx(self.make_source(directory))
            protected_cases = (
                (r"\alpha + \beta", "table"),
                ("z_i", "bookmark"),
                ("a^2", "hyperlink"),
                ("x_i", "field"),
                ("q_i", "drawing"),
                ("r_i", "content_control"),
                ("c_i", "comment_range"),
            )
            anchors = {
                r"\alpha + \beta": None,
                "z_i": "Bookmark",
                "a^2": "Link",
                "x_i": "Field",
                "q_i": "Drawing-adjacent",
                "r_i": "Content-control",
                "c_i": "Comment story reference",
            }
            for raw_source, protected_key in protected_cases:
                row = self.find_row(manifest, raw_source, anchor_before=anchors[raw_source])
                self.assertTrue(row["protected_containers"][protected_key], row)
                self.assertEqual("NEEDS_SPECIAL_HANDLER", row["status"])

            multi_run = self.find_row(manifest, "x_i^2", anchor_before="Multi-run")
            self.assertEqual(2, multi_run["run_boundaries"]["run_count"])
            self.assertEqual("NEEDS_SPECIAL_HANDLER", multi_run["status"])

            comment_story = self.find_row(manifest, "c_i", package_part="word/comments.xml")
            self.assertEqual("comment", comment_story["story"])
            self.assertEqual("main", self.find_row(manifest, "c_i", anchor_before="Comment story reference")["story"])
            for row in manifest.formulas:
                if row["package_part"] in {
                    "word/header1.xml",
                    "word/footer1.xml",
                    "word/footnotes.xml",
                    "word/endnotes.xml",
                    "word/comments.xml",
                }:
                    self.assertEqual(row["story"], {"word/header1.xml": "header", "word/footer1.xml": "footer", "word/footnotes.xml": "footnote", "word/endnotes.xml": "endnote", "word/comments.xml": "comment"}[row["package_part"]])

    def test_classifier_does_not_convert_prose_and_unknown_formula_is_explicit(self):
        self.assertIsNone(classify_formula_text("The alpha coefficient is stable."))
        self.assertIsNone(classify_formula_text("Use alpha as a prose label."))
        self.assertEqual("UNKNOWN_FORMULA", classify_formula_text("u ∈ v"))

    def test_unknown_story_is_reported_and_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            package = build_fixture_package()
            package["word/unknown.xml"] = (
                f'<w:unknownStory xmlns:w="{W}"><w:p><w:r><w:t>u ∈ v</w:t></w:r></w:p></w:unknownStory>'.encode()
            )
            source = Path(directory) / "unknown-story.docx"
            write_package(source, package)
            manifest = inventory_docx(source)

            row = self.find_row(manifest, "u ∈ v", package_part="word/unknown.xml")
            self.assertEqual("unknown", row["story"])
            self.assertEqual("UNKNOWN_FORMULA", row["source_type"])
            self.assertEqual("NEEDS_SPECIAL_HANDLER", row["status"])
            self.assertTrue(row["extensions"]["inventory"]["unknown_story"])
            self.assertIn("word/unknown.xml", manifest.extensions["inventory"]["unsupported_parts"])

    def test_package_corruption_and_unsafe_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bad_zip = Path(directory) / "bad-zip.docx"
            bad_zip.write_bytes(b"not a zip package")
            with self.assertRaisesRegex(InventoryError, "readable DOCX ZIP"):
                inventory_docx(bad_zip)

            malformed = build_fixture_package()
            malformed["word/document.xml"] = b"<w:document xmlns:w=\"urn:broken\">"
            malformed_path = Path(directory) / "malformed-xml.docx"
            write_package(malformed_path, malformed)
            with self.assertRaisesRegex(InventoryError, "cannot parse XML package part word/document.xml"):
                inventory_docx(malformed_path)

            duplicate_path = Path(directory) / "duplicate.docx"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                write_package(duplicate_path, build_fixture_package(), duplicate=("word/document.xml", b"duplicate"))
            with self.assertRaisesRegex(InventoryError, "duplicate package part names"):
                inventory_docx(duplicate_path)

            unsafe_path = Path(directory) / "unsafe.docx"
            write_package(unsafe_path, {"../escape.xml": b"<root />"})
            with self.assertRaisesRegex(InventoryError, "unsafe package part name"):
                inventory_docx(unsafe_path)

            missing_target = build_fixture_package()
            rels = ET.fromstring(missing_target["word/_rels/document.xml.rels"])
            relation = next(node for node in rels if node.get("Id") == "rIdImage")
            relation.set("Target", "missing.png")
            missing_target["word/_rels/document.xml.rels"] = ET.tostring(rels, encoding="UTF-8", xml_declaration=True)
            missing_path = Path(directory) / "missing-target.docx"
            write_package(missing_path, missing_target)
            with self.assertRaisesRegex(InventoryError, "targets missing part"):
                inventory_docx(missing_path)

    def test_output_cannot_overwrite_source_or_a_hardlink_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(directory)
            with self.assertRaisesRegex(InventoryError, "must not overwrite"):
                write_inventory(source, source)

            hardlink = Path(directory) / "source-hardlink.docx"
            os.link(source, hardlink)
            with self.assertRaisesRegex(InventoryError, "must not overwrite"):
                write_inventory(source, hardlink)

            output = Path(directory) / "inventory.json"
            before = source.read_bytes()
            write_inventory(source, output)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(inventory_docx(source).manifest_id, json.loads(output.read_text())["manifest_id"])


if __name__ == "__main__":
    unittest.main()
