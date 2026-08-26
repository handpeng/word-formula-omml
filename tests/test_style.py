from __future__ import annotations

import copy
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.fixtures.docx_fixture import build_fixture_package
from word_formula_omml.contract import load_manifest
from word_formula_omml.inventory import inventory_docx
from word_formula_omml.style import (
    MATH_FONT,
    StyleStatus,
    resolve_manifest_styles,
    resolve_style,
    snapshot_paragraph_style,
    snapshot_run_style,
    style_catalog,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def q(local: str) -> str:
    return f"{{{W}}}{local}"


def write_package(path: Path, package: dict[str, bytes]) -> None:
    with path.open("wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(package.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)


class StyleResolverTests(unittest.TestCase):
    def occurrence(self, **changes: object) -> dict:
        row = {
            "id": "F-001",
            "latex": "x_i",
            "layout": "inline",
            "style_snapshot": {},
        }
        row.update(changes)
        return row

    def test_repeated_inventory_occurrences_resolve_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            write_package(source, build_fixture_package())
            manifest = inventory_docx(source)
            rows = [
                row
                for row in manifest.formulas
                if row.get("raw_source") == "x_i"
                and row.get("status") == "DISCOVERED"
                and row.get("package_part") == "word/document.xml"
            ]

            resolutions = [resolve_style(row) for row in rows]

        self.assertEqual(3, len(resolutions))
        self.assertEqual({"000000", "0000FF", "FF0000"}, {item.style["color"] for item in resolutions})
        self.assertEqual({"20", "24", "28"}, {item.style["size"] for item in resolutions})
        red = next(item for item in resolutions if item.style["color"] == "FF0000")
        self.assertEqual("italic", red.style["math_style"])
        self.assertTrue(all(item.auto_eligible for item in resolutions))

    def test_precedence_is_per_field_and_records_provenance(self):
        row = self.occurrence(
            layout="display",
            paragraph_style="Para",
            style_snapshot={
                "color": "010101",
                "paragraph": {"alignment": "center", "spacing_before": "100"},
            },
        )
        result = resolve_style(
            row,
            occurrence_override={"color": "AABBCC", "math_style": "bold"},
            semantic_block={"size": "22", "paragraph": {"spacing_after": "120"}},
            character_style="Char",
            styles={
                "character": {"Char": {"highlight": "yellow"}},
                "paragraph": {"Para": {"underline": "double", "line": "240"}},
            },
            document_default={"line_rule": "exact"},
        )

        self.assertEqual(StyleStatus.RESOLVED.value, result.status)
        self.assertEqual(
            {
                "math_font": MATH_FONT,
                "math_font_policy": "CAMBRIA_MATH",
                "color": "AABBCC",
                "size": "22",
                "highlight": "yellow",
                "underline": "double",
                "math_style": "bold",
                "paragraph": {
                    "alignment": "center",
                    "spacing_before": "100",
                    "spacing_after": "120",
                    "line": "240",
                    "line_rule": "exact",
                },
            },
            result.style,
        )
        self.assertEqual("occurrence_override", result.provenance["color"])
        self.assertEqual("semantic_block", result.provenance["size"])
        self.assertEqual("character_style", result.provenance["highlight"])
        self.assertEqual("paragraph_style", result.provenance["underline"])
        self.assertEqual("document_default", result.provenance["paragraph.line_rule"])
        self.assertEqual("source_run", result.provenance["paragraph.alignment"])

    def test_manifest_color_is_an_explicit_occurrence_override(self):
        result = resolve_style(
            self.occurrence(
                color="FF0000",
                style_snapshot={"color": "0000FF"},
            )
        )

        self.assertEqual(StyleStatus.RESOLVED.value, result.status)
        self.assertEqual("FF0000", result.style["color"])
        self.assertEqual("occurrence_override", result.provenance["color"])

    def test_character_and_paragraph_style_inheritance_is_resolved(self):
        result = resolve_style(
            self.occurrence(
                style_snapshot={"character_style": "Child"},
                paragraph_style="Display",
                layout="display",
            ),
            styles={
                "character": {
                    "Base": {"color": "112233", "size": "18"},
                    "Child": {"based_on": "Base", "size": "24"},
                },
                "paragraph": {
                    "BaseParagraph": {"alignment": "left", "spacing_before": "80"},
                    "Display": {"based_on": "BaseParagraph", "alignment": "center"},
                },
            },
        )

        self.assertEqual(StyleStatus.RESOLVED.value, result.status)
        self.assertEqual("112233", result.style["color"])
        self.assertEqual("24", result.style["size"])
        self.assertEqual(
            {"alignment": "center", "spacing_before": "80"},
            result.style["paragraph"],
        )
        self.assertEqual("character_style", result.provenance["color"])
        self.assertEqual("paragraph_style", result.provenance["paragraph.spacing_before"])

    def test_xml_style_catalog_and_snapshots_preserve_word_context(self):
        run = ET.fromstring(
            f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="Child"/><w:b/><w:color w:val="123456"/><w:sz w:val="22"/></w:rPr><w:t>x_i</w:t></w:r>'
        )
        paragraph = ET.fromstring(
            f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="Display"/><w:jc w:val="center"/><w:spacing w:before="100" w:after="40"/></w:pPr></w:p>'
        )
        self.assertEqual(
            {
                "character_style": "Child",
                "bold": True,
                "color": "123456",
                "size": "22",
            },
            snapshot_run_style(run),
        )
        self.assertEqual(
            {
                "paragraph_style": "Display",
                "alignment": "center",
                "spacing_before": "100",
                "spacing_after": "40",
            },
            snapshot_paragraph_style(paragraph),
        )

        styles = ET.fromstring(
            f'<w:styles xmlns:w="{W}">'
            '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri"/></w:rPr></w:rPrDefault></w:docDefaults>'
            '<w:style w:type="character" w:styleId="Base"><w:rPr><w:color w:val="112233"/></w:rPr></w:style>'
            '<w:style w:type="character" w:styleId="Child"><w:basedOn w:val="Base"/><w:rPr><w:sz w:val="24"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Display"><w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
            '</w:styles>'
        )
        catalog = style_catalog(styles)
        self.assertEqual("112233", catalog["character"]["Base"]["color"])
        self.assertEqual("center", catalog["paragraph"]["Display"]["alignment"])
        result = resolve_style(
            self.occurrence(
                layout="display",
                style_snapshot={"character_style": "Child"},
                paragraph_style="Display",
            ),
            styles=styles,
        )
        self.assertEqual(StyleStatus.RESOLVED.value, result.status)
        self.assertEqual("112233", result.style["color"])
        self.assertEqual("24", result.style["size"])
        self.assertEqual("center", result.style["paragraph"]["alignment"])

    def test_inventory_captures_direct_display_paragraph_formatting(self):
        package = build_fixture_package()
        document = ET.fromstring(package["word/document.xml"])
        paragraph = next(
            node
            for node in document.findall(".//w:p", NS)
            if "Repeated: x_i" in "".join(node.itertext())
            and node.find(".//w:rPr/w:color[@w:val='0000FF']", NS) is not None
        )
        properties = ET.Element(q("pPr"))
        ET.SubElement(properties, q("jc"), {q("val"): "center"})
        ET.SubElement(
            properties,
            q("spacing"),
            {q("before"): "100", q("after"): "40", q("line"): "240", q("lineRule"): "exact"},
        )
        paragraph.insert(0, properties)
        package["word/document.xml"] = ET.tostring(document, encoding="UTF-8", xml_declaration=True)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paragraph-style.docx"
            write_package(source, package)
            manifest = inventory_docx(source)
            row = next(
                item
                for item in manifest.formulas
                if item.get("raw_source") == "x_i"
                and item.get("style_snapshot", {}).get("color") == "0000FF"
            )
            row["target_layout"] = "display"
            result = resolve_style(row)

        self.assertEqual(
            {
                "alignment": "center",
                "spacing_before": "100",
                "spacing_after": "40",
                "line": "240",
                "line_rule": "exact",
            },
            row["style_snapshot"]["paragraph"],
        )
        self.assertEqual(StyleStatus.RESOLVED.value, result.status)
        self.assertEqual(row["style_snapshot"]["paragraph"], result.style["paragraph"])

    def test_math_emphasis_is_explicit_and_prose_fonts_do_not_leak(self):
        bold = resolve_style(
            self.occurrence(style_snapshot={"bold": True, "fonts": {"ascii": "Calibri"}})
        )
        prose_italic = resolve_style(
            self.occurrence(style_snapshot={"italic": True, "semantic_emphasis": "prose"})
        )
        vector = resolve_style(self.occurrence(style_snapshot={"vector": True}))

        self.assertEqual(StyleStatus.RESOLVED.value, bold.status)
        self.assertEqual("bold", bold.style["math_style"])
        self.assertNotIn("fonts", bold.style)
        self.assertEqual(MATH_FONT, bold.style["math_font"])
        self.assertIn("prose_fonts_not_copied:source_run", bold.warnings)
        self.assertEqual(StyleStatus.RESOLVED.value, prose_italic.status)
        self.assertNotIn("math_style", prose_italic.style)
        self.assertEqual(StyleStatus.NEEDS_REVIEW.value, vector.status)
        self.assertIn("vector_semantics_requires_formula_ir", " ".join(vector.conflicts))

    def test_existing_omml_is_not_restyled_by_default(self):
        result = resolve_style(
            self.occurrence(
                source_type="EXISTING_OMML",
                style_snapshot={"color": "FF0000", "size": "30"},
            )
        )

        self.assertEqual(StyleStatus.UNSUPPORTED.value, result.status)
        self.assertFalse(result.auto_eligible)
        self.assertEqual("existing_omml_not_restyled", result.reason)

    def test_display_layout_is_applied_only_to_display_equations(self):
        context = {"alignment": "right", "spacing_before": "120", "spacing_after": "80"}
        display = resolve_style(
            self.occurrence(layout="display"),
            paragraph_style=context,
        )
        inline = resolve_style(self.occurrence(), paragraph_style=context)

        self.assertEqual(StyleStatus.RESOLVED.value, display.status)
        self.assertEqual(context, display.style["paragraph"])
        self.assertEqual(StyleStatus.RESOLVED.value, inline.status)
        self.assertNotIn("paragraph", inline.style)
        self.assertIn("inline_paragraph_context_not_applied", inline.warnings)

    def test_conflicts_and_invalid_inputs_fail_closed(self):
        conflict = resolve_style(
            self.occurrence(
                style_snapshot={
                    "conflict": True,
                    "runs": [{"color": "000000"}, {"color": "FF0000"}],
                }
            )
        )
        invalid = resolve_style(self.occurrence(style_snapshot={"color": "not-a-color"}))
        unknown = resolve_style(
            self.occurrence(style_snapshot={"character_style": "Missing"}),
            styles={"character": {}},
        )
        cyclic = resolve_style(
            self.occurrence(style_snapshot={"character_style": "A"}),
            styles={"character": {"A": {"based_on": "B"}, "B": {"based_on": "A"}}},
        )
        bad_math_font = resolve_style(self.occurrence(style_snapshot={"math_font": "Arial"}))
        malformed_catalog = resolve_style(
            self.occurrence(),
            styles={"character": []},
        )

        self.assertEqual(StyleStatus.NEEDS_REVIEW.value, conflict.status)
        self.assertIn("source_run:color", conflict.conflicts)
        for result in (invalid, unknown, cyclic, malformed_catalog):
            self.assertEqual(StyleStatus.UNSUPPORTED.value, result.status)
            self.assertEqual(MATH_FONT, result.style["math_font"])
        self.assertEqual(StyleStatus.NEEDS_REVIEW.value, bad_math_font.status)
        self.assertNotIn("Arial", bad_math_font.style.values())

    def test_higher_precedence_value_can_cover_lower_conflict_but_not_higher_conflict(self):
        lower_conflict = resolve_style(
            self.occurrence(style_snapshot={"color": "0000FF"}),
            character_style={
                "conflict": True,
                "runs": [{"color": "00FF00"}, {"color": "FF0000"}],
            },
        )
        higher_conflict = resolve_style(
            self.occurrence(style_snapshot={"color": "0000FF"}),
            occurrence_override={
                "conflict": True,
                "runs": [{"color": "000000"}, {"color": "FFFFFF"}],
            },
        )

        self.assertEqual(StyleStatus.RESOLVED.value, lower_conflict.status)
        self.assertEqual("0000FF", lower_conflict.style["color"])
        self.assertIn("higher_precedence_conflict_overrode:character_style:color", lower_conflict.warnings)
        self.assertEqual(StyleStatus.NEEDS_REVIEW.value, higher_conflict.status)
        self.assertIn("occurrence_override:color", higher_conflict.conflicts)

    def test_manifest_resolution_is_identity_consistent_and_does_not_mutate_input(self):
        raw = {
            "schema_version": 1,
            "formulas": [
                self.occurrence(id="F-001", style_snapshot={"color": "0000FF"}),
                self.occurrence(
                    id="F-002",
                    style_snapshot={"conflict": True, "runs": [{"color": "000000"}, {"color": "FF0000"}]},
                ),
            ],
        }
        before = copy.deepcopy(raw)
        manifest = load_manifest(raw)
        resolved = resolve_manifest_styles(
            manifest,
            contexts={"F-001": {"occurrence_override": {"color": "FF0000"}}},
        )

        self.assertEqual(before, raw)
        self.assertEqual(
            "FF0000",
            resolved.formulas[0]["resolved_style"]["style"]["color"],
        )
        self.assertEqual("NEEDS_REVIEW", resolved.formulas[1]["status"])
        self.assertEqual(resolved.manifest_id, load_manifest(resolved.to_dict()).manifest_id)
        self.assertEqual(
            ["F-001", "F-002"],
            [row["id"] for row in resolved.formulas],
        )

    def test_manifest_resolution_preserves_explicit_exclusions(self):
        manifest = load_manifest(
            {
                "schema_version": 1,
                "formulas": [
                    self.occurrence(
                        id="F-001",
                        status="EXCLUDED",
                        exclusion={"approved": True, "reason": "outside remediation scope"},
                        style_snapshot={"color": "not-a-color"},
                    )
                ],
            }
        )
        resolved = resolve_manifest_styles(manifest)

        self.assertEqual("EXCLUDED", resolved.formulas[0]["status"])
        self.assertEqual("UNSUPPORTED", resolved.formulas[0]["resolved_style"]["status"])


if __name__ == "__main__":
    unittest.main()
