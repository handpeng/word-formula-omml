from __future__ import annotations

import hashlib
import json
import posixpath
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from word_formula_omml.contract import freeze_job, load_manifest

from tests.fixtures.docx_fixture import (
    EXPECTATIONS_PATH,
    CT,
    M,
    W,
    build_fixture_package,
    load_expectations,
    write_fixture,
)


NS = {"w": W, "m": M, "ct": CT}
SOURCE_SHA = "a" * 64


class FixtureCorpusTests(unittest.TestCase):
    def test_package_is_deterministic_and_crc_clean(self):
        first = build_fixture_package()
        second = build_fixture_package()
        self.assertEqual(first, second)
        expectations = load_expectations()
        for part in expectations["extensions"]["required_parts"]:
            self.assertIn(part, first)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adversarial.docx"
            first_sha = write_fixture(path)
            second_path = Path(directory) / "adversarial-copy.docx"
            second_sha = write_fixture(second_path)
            self.assertEqual(first_sha, second_sha)
            self.assertEqual(path.read_bytes(), second_path.read_bytes())
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(sorted(archive.namelist()), sorted(first))

    def test_all_xml_parts_parse_and_expected_structures_exist(self):
        package = build_fixture_package()
        fixture_expectations = load_expectations()["extensions"]
        for name, data in package.items():
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(data)
        content_types = ET.fromstring(package["[Content_Types].xml"])
        for override in content_types.findall("ct:Override", NS):
            self.assertIn(override.get("PartName", "").lstrip("/"), package)
        document = ET.fromstring(package["word/document.xml"])
        expectations = fixture_expectations["package_invariants"]
        self.assertEqual(len(document.findall(".//w:drawing", NS)), expectations["drawings"])
        self.assertEqual(len(document.findall(".//w:tbl", NS)), expectations["tables"])
        self.assertEqual(len(document.findall(".//w:bookmarkStart", NS)), expectations["bookmarks"])
        self.assertEqual(len(document.findall(".//w:hyperlink", NS)), expectations["hyperlinks"])
        self.assertEqual(len(document.findall(".//w:fldSimple", NS)), expectations["fields"])
        self.assertEqual(len(document.findall(".//w:sdt", NS)), expectations["content_controls"])
        self.assertEqual(len(document.findall(".//m:oMath", NS)), expectations["omath"])
        self.assertEqual(len(document.findall(".//m:oMathPara", NS)), expectations["omath_para"])
        self.assertEqual(len(document.findall(".//m:f", NS)), expectations["fractions"])
        self.assertEqual(len(document.findall(".//w:ins", NS)), expectations["other_author_insertions"])
        self.assertEqual(len(document.findall(".//w:del", NS)), expectations["other_author_deletions"])
        self.assertEqual(sum(name.startswith("customXml/") for name in package), expectations["custom_xml_parts"])
        self.assertIn("word/media/image1.png", package)
        self.assertIn("word/embeddings/oleObject1.bin", package)
        self.assertEqual(len(document.findall(".//w:pPrChange", NS)), expectations["paragraph_property_changes"])
        self.assertEqual(len(document.findall(".//w:rPrChange", NS)), expectations["run_property_changes"])

    def test_display_omml_is_a_body_level_paragraph(self):
        package = build_fixture_package()
        document = ET.fromstring(package["word/document.xml"])
        body = document.find("w:body", NS)
        self.assertIsNotNone(body)
        display_nodes = [child for child in body if child.tag == f"{{{M}}}oMathPara"]
        self.assertEqual(len(display_nodes), 1)
        self.assertEqual(display_nodes[0][0].tag, f"{{{M}}}oMath")
        self.assertEqual(len(display_nodes[0].findall(".//m:f", NS)), 1)

    def test_relationships_and_protected_parts_are_explicit(self):
        package = build_fixture_package()
        relationships = ET.fromstring(package["word/_rels/document.xml.rels"])
        targets = {node.get("Id"): node.get("Target") for node in relationships}
        self.assertEqual(targets["rIdImage"], "media/image1.png")
        self.assertEqual(targets["rIdOle"], "embeddings/oleObject1.bin")
        self.assertEqual(targets["rIdHyperlink"], "https://example.invalid/synthetic")
        self.assertEqual(targets["rIdHeader"], "header1.xml")
        self.assertEqual(targets["rIdComments"], "comments.xml")
        self.assertEqual(
            hashlib.sha256(package["word/media/image1.png"]).hexdigest(),
            hashlib.sha256(build_fixture_package()["word/media/image1.png"]).hexdigest(),
        )
        self.assertEqual(sum(name.startswith("word/embeddings/") for name in package), 1)
        self.assertEqual(sum(name.startswith("customXml/") for name in package), 1)

        for relationship_part in ("_rels/.rels", "word/_rels/document.xml.rels"):
            root = ET.fromstring(package[relationship_part])
            ids = [node.get("Id") for node in root]
            self.assertEqual(len(ids), len(set(ids)))
            if relationship_part == "_rels/.rels":
                owner = ""
            else:
                owner = relationship_part.replace("/_rels/", "/")[:-5]
            for node in root:
                if node.get("TargetMode") == "External":
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(owner), node.get("Target", "")))
                self.assertIn(target, package, f"unresolved relationship {relationship_part}:{node.get('Id')}")

        story_roots = {
            "word/header1.xml": "hdr",
            "word/footer1.xml": "ftr",
            "word/footnotes.xml": "footnotes",
            "word/endnotes.xml": "endnotes",
            "word/comments.xml": "comments",
        }
        for part, local_name in story_roots.items():
            self.assertEqual(ET.fromstring(package[part]).tag, f"{{{W}}}{local_name}")

    def test_expected_candidate_sources_are_present_in_the_declared_story(self):
        package = build_fixture_package()
        expectations = load_expectations()
        story_text = {}
        for name, data in package.items():
            if not name.endswith(".xml"):
                continue
            root = ET.fromstring(data)
            story_text[name] = "".join(root.itertext())
        for row in load_manifest(expectations).formulas:
            part = row.get("package_part", "word/document.xml")
            source = row["source"]
            if row["id"] == "display-omml":
                document = ET.fromstring(package["word/document.xml"])
                self.assertEqual(len(document.findall(".//m:oMathPara/m:oMath/m:f", NS)), 1)
            else:
                self.assertIn(source, story_text[part], row["id"])

    def test_revision_metadata_matches_machine_readable_expectations(self):
        document = ET.fromstring(build_fixture_package()["word/document.xml"])
        revision = load_expectations()["extensions"]["revision_expectations"]
        self.assertEqual(
            sorted(node.get(f"{{{W}}}id") for node in document.findall(".//w:ins", NS)),
            revision["insertion_ids"],
        )
        self.assertEqual(
            sorted(node.get(f"{{{W}}}id") for node in document.findall(".//w:del", NS)),
            revision["deletion_ids"],
        )
        self.assertEqual(
            sorted(node.get(f"{{{W}}}id") for node in document.findall(".//w:pPrChange", NS)),
            revision["paragraph_property_change_ids"],
        )
        self.assertEqual(
            sorted(node.get(f"{{{W}}}id") for node in document.findall(".//w:rPrChange", NS)),
            revision["run_property_change_ids"],
        )

    def test_same_count_revision_mutation_case_is_materialized(self):
        package = build_fixture_package()
        case = load_expectations()["extensions"]["revision_mutation_case"]
        document = ET.fromstring(package[case["part"]])
        nodes = document.findall(f".//w:{case['node']}", NS)
        target = next(node for node in nodes if node.get(f"{{{W}}}id") == case["id"])
        self.assertEqual("".join(target.itertext()), case["original_text"])

        text_node = target.find(".//w:t", NS)
        self.assertIsNotNone(text_node)
        text_node.text = case["mutated_text"]
        mutated_nodes = document.findall(f".//w:{case['node']}", NS)
        self.assertTrue(case["same_node_count"])
        self.assertEqual(len(nodes), len(mutated_nodes))
        self.assertEqual(
            "".join(
                text
                for node in mutated_nodes
                if node.get(f"{{{W}}}id") == case["id"]
                for text in node.itertext()
            ),
            case["mutated_text"],
        )

    def test_expectations_are_v1_schema_and_job_compatible(self):
        raw = load_expectations()
        manifest = load_manifest(raw)
        expected = raw["extensions"]["expected_outcomes"]
        self.assertEqual(set(expected), {row["id"] for row in manifest.formulas})
        self.assertEqual(len(manifest.formulas), len(expected))
        self.assertEqual(manifest.extensions["fixture_id"], "adversarial-v1")
        job = freeze_job(manifest, SOURCE_SHA, ["clean"])
        self.assertEqual(len(job.occurrences), len(expected))
        self.assertEqual(job.status, "PARTIAL_REVIEW_OUTPUT")

    def test_expectations_have_machine_readable_refusal_and_trap_paths(self):
        extensions = load_expectations()["extensions"]
        outcomes = extensions["expected_outcomes"]
        refusal_results = {"needs_review", "needs_special_handler", "deleted_surface_only"}
        self.assertGreaterEqual(sum(item["result"] in refusal_results for item in outcomes.values()), 10)
        self.assertEqual(outcomes["semantic-trap-a"]["canonical_kind"], "script_then_exponent")
        self.assertEqual(outcomes["semantic-trap-b"]["canonical_kind"], "grouped_exponent")
        self.assertEqual(outcomes["repeat-red"]["style"]["color"], "FF0000")
        self.assertEqual(extensions["canonical_semantics"]["interval"]["right"], "closed")
        self.assertEqual(len(extensions["semantic_mismatch_cases"]), 2)
        self.assertEqual(
            extensions["batch_cases"]["mixed-safe-and-refused"]["expected_delivery_status"],
            "PARTIAL_REVIEW_OUTPUT",
        )

    def test_semantic_mismatch_cases_have_distinct_approved_and_altered_forms(self):
        extensions = load_expectations()["extensions"]
        canonical = extensions["canonical_semantics"]
        for case in extensions["semantic_mismatch_cases"]:
            self.assertIn(case["approved"], canonical)
            self.assertIn("altered_canonical", case)
            self.assertNotEqual(canonical[case["approved"]], case["altered_canonical"])
            self.assertTrue(case["reason"])

    def test_revision_and_source_views_remain_separate(self):
        document = ET.fromstring(build_fixture_package()["word/document.xml"])
        accepted_text = "".join(node.text or "" for node in document.findall(".//w:t", NS))
        deleted_text = "".join(node.text or "" for node in document.findall(".//w:delText", NS))
        self.assertIn("Other revision x_i", accepted_text)
        self.assertNotIn("Deleted y_i", accepted_text)
        self.assertIn("Deleted y_i", deleted_text)
        self.assertNotEqual(accepted_text, accepted_text + deleted_text)

    def test_fixture_source_is_never_overwritten_by_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            write_fixture(source)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            output = Path(directory) / "output.docx"
            write_fixture(output)
            self.assertEqual(original, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotEqual(source, output)

    def test_expectation_json_has_no_private_or_runtime_paths(self):
        text = EXPECTATIONS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/home/", text)
        self.assertNotIn("/srv/", text)
        json.loads(text)


if __name__ == "__main__":
    unittest.main()
