"""Build a deterministic, synthetic DOCX risk corpus without third parties."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
V = "urn:schemas-microsoft-com:vml"
O = "urn:schemas-microsoft-com:office:office"
XML = "http://www.w3.org/XML/1998/namespace"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

NS = {
    "w": W,
    "r": R,
    "m": M,
    "wp": WP,
    "a": A,
    "pic": PIC,
    "v": V,
    "o": O,
    "cp": CP,
    "dc": DC,
    "dcterms": DCTERMS,
    "xsi": XSI,
}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

EXPECTATIONS_PATH = Path(__file__).with_name("expectations.json")
FIXTURE_NAME = "adversarial-v1"
FIXED_DATE = "2026-01-01T00:00:00Z"


def qn(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def element(namespace: str, local: str, **attributes: str) -> ET.Element:
    return ET.Element(qn(namespace, local), attributes)


def append(parent: ET.Element, child: ET.Element) -> ET.Element:
    parent.append(child)
    return child


def run(
    text: str,
    *,
    color: str | None = None,
    size: int | None = None,
    bold: bool = False,
    italic: bool = False,
    style: str | None = None,
    rsid: str | None = None,
    deleted: bool = False,
    rpr_change: bool = False,
) -> ET.Element:
    value = element(W, "r")
    if rsid:
        value.set(qn(W, "rsidR"), rsid)
    properties = element(W, "rPr")
    if style:
        append(properties, element(W, "rStyle", **{qn(W, "val"): style}))
    if bold:
        append(properties, element(W, "b"))
    if italic:
        append(properties, element(W, "i"))
    if color:
        append(properties, element(W, "color", **{qn(W, "val"): color}))
    if size:
        append(properties, element(W, "sz", **{qn(W, "val"): str(size)}))
        append(properties, element(W, "szCs", **{qn(W, "val"): str(size)}))
    if rpr_change:
        append(
            properties,
            element(
                W,
                "rPrChange",
                **{
                    qn(W, "id"): "201",
                    qn(W, "author"): "Other Reviewer",
                    qn(W, "date"): FIXED_DATE,
                },
            ),
        )
    if len(properties):
        append(value, properties)
    text_node = element(W, "delText" if deleted else "t")
    if text.startswith((" ", "\t")) or text.endswith((" ", "\t")):
        text_node.set(qn(XML, "space"), "preserve")
    text_node.text = text
    append(value, text_node)
    return value


def paragraph(*children: ET.Element, style: str | None = None, ppr_change: bool = False) -> ET.Element:
    value = element(W, "p")
    if style or ppr_change:
        properties = element(W, "pPr")
        if style:
            append(properties, element(W, "pStyle", **{qn(W, "val"): style}))
        if ppr_change:
            append(
                properties,
                element(
                    W,
                    "pPrChange",
                    **{
                        qn(W, "id"): "200",
                        qn(W, "author"): "Other Reviewer",
                        qn(W, "date"): FIXED_DATE,
                    },
                ),
            )
        append(value, properties)
    for child in children:
        append(value, child)
    return value


def text_paragraph(text: str, **kwargs: object) -> ET.Element:
    run_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in {"color", "size", "bold", "italic", "style", "rsid"}
    }
    paragraph_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in {"style", "ppr_change"}
    }
    return paragraph(run(text, **run_kwargs), **paragraph_kwargs)


def revision_wrapper(local: str, revision_id: str, author: str, child: ET.Element) -> ET.Element:
    wrapper = element(
        W,
        local,
        **{
            qn(W, "id"): revision_id,
            qn(W, "author"): author,
            qn(W, "date"): FIXED_DATE,
        },
    )
    append(wrapper, child)
    return wrapper


def comment_range(paragraph_value: ET.Element, comment_id: str, child: ET.Element) -> None:
    paragraph_value.append(element(W, "commentRangeStart", **{qn(W, "id"): comment_id}))
    paragraph_value.append(child)
    paragraph_value.append(element(W, "commentRangeEnd", **{qn(W, "id"): comment_id}))
    reference = element(W, "r")
    append(reference, element(W, "commentReference", **{qn(W, "id"): comment_id}))
    paragraph_value.append(reference)


def native_omath(text: str, *, display: bool = False) -> ET.Element:
    math = element(M, "oMath")
    if text == r"\frac{a}{b}":
        fraction = element(M, "f")
        numerator = element(M, "num")
        append(numerator, math_run("a"))
        denominator = element(M, "den")
        append(denominator, math_run("b"))
        append(fraction, numerator)
        append(fraction, denominator)
        append(math, fraction)
    else:
        append(math, math_run(text))
    if not display:
        return math
    math_para = element(M, "oMathPara")
    append(math_para, math)
    return math_para


def math_run(text: str) -> ET.Element:
    value = element(M, "r")
    run_properties = element(M, "rPr")
    append(run_properties, element(M, "sty", **{qn(M, "val"): "p"}))
    append(value, run_properties)
    math_text = element(M, "t")
    math_text.text = text
    append(value, math_text)
    return value


def drawing() -> ET.Element:
    value = element(W, "drawing")
    inline = element(WP, "inline", distT="0", distB="0", distL="0", distR="0")
    append(inline, element(WP, "extent", cx="914400", cy="914400"))
    append(inline, element(WP, "docPr", id="1", name="Synthetic figure"))
    frame_properties = element(WP, "cNvGraphicFramePr")
    append(frame_properties, element(A, "graphicFrameLocks", noChangeAspect="1"))
    append(inline, frame_properties)
    graphic = element(A, "graphic")
    graphic_data = element(A, "graphicData", **{"uri": PIC})
    picture = element(PIC, "pic")
    non_visual = element(PIC, "nvPicPr")
    append(non_visual, element(PIC, "cNvPr", id="0", name="image1.png"))
    append(non_visual, element(PIC, "cNvPicPr"))
    append(picture, non_visual)
    fill = element(PIC, "blipFill")
    append(fill, element(A, "blip", **{qn(R, "embed"): "rIdImage"}))
    append(fill, element(A, "stretch"))
    append(picture, fill)
    shape = element(PIC, "spPr")
    transform = element(A, "xfrm")
    append(transform, element(A, "off", x="0", y="0"))
    append(transform, element(A, "ext", cx="914400", cy="914400"))
    append(shape, transform)
    append(shape, element(A, "prstGeom", prst="rect"))
    append(picture, shape)
    append(graphic_data, picture)
    append(graphic, graphic_data)
    append(inline, graphic)
    append(value, inline)
    return value


def relationship(rel_id: str, rel_type: str, target: str, *, external: bool = False) -> ET.Element:
    attributes = {"Id": rel_id, "Type": rel_type, "Target": target}
    if external:
        attributes["TargetMode"] = "External"
    return element(PKG, "Relationship", **attributes)


def rels_xml(items: Iterable[ET.Element]) -> bytes:
    root = element(PKG, "Relationships")
    for item in items:
        append(root, item)
    return xml_bytes(root)


def xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True, short_empty_elements=True)


def document_xml() -> bytes:
    root = element(W, "document")
    body = append(root, element(W, "body"))
    title = text_paragraph("Synthetic adversarial formula corpus")
    note_references = element(W, "r")
    append(note_references, element(W, "footnoteReference", **{qn(W, "id"): "1"}))
    append(note_references, element(W, "endnoteReference", **{qn(W, "id"): "1"}))
    title.append(note_references)
    append(body, title)
    append(body, text_paragraph("Raw source: "))
    body[-1].append(run(r"\frac{x_i^2}{\sqrt{y}}", color="000000", size=22))
    append(body, text_paragraph("Plain scripts: x_i^2"))
    append(body, text_paragraph("Plain operators: x >= y +/- 10^-3"))
    append(body, text_paragraph("Unicode operators: \u03b1 \u2264 \u03b2 \u00b1 \u03b3"))
    append(body, text_paragraph(r"Partial source: \frac{x}{y"))
    append(body, text_paragraph("Lost escape: frac{x}{y}"))
    append(body, text_paragraph("Corrupted source: x \u00e2\u2030\u00a4 y"))
    append(body, paragraph(run("Multi-run: x_"), run("i^2", color="008000")))
    append(body, paragraph(run("Repeated: x_i", color="000000", size=20)))
    append(body, paragraph(run("Repeated: x_i", color="0000FF", size=24, rpr_change=True)))
    append(body, paragraph(run("Repeated: x_i", color="FF0000", size=28, italic=True)))
    append(
        body,
        paragraph(
            revision_wrapper("ins", "101", "Other Reviewer", run("Other revision x_i", color="7030A0")),
            revision_wrapper("del", "102", "Other Reviewer", run("Deleted y_i", deleted=True)),
        ),
    )
    table = element(W, "tbl")
    table_properties = element(W, "tblPr")
    append(table_properties, element(W, "tblStyle", **{qn(W, "val"): "TableGrid"}))
    append(table, table_properties)
    table_grid = element(W, "tblGrid")
    append(table_grid, element(W, "gridCol", **{qn(W, "w"): "9000"}))
    append(table, table_grid)
    row_value = element(W, "tr")
    cell = element(W, "tc")
    append(cell, paragraph(run(r"Table: \alpha + \beta")))
    append(row_value, cell)
    append(table, row_value)
    append(body, table)
    bookmark_paragraph = paragraph(run("Bookmark: "))
    bookmark_paragraph.append(element(W, "bookmarkStart", **{qn(W, "id"): "10", qn(W, "name"): "FormulaBookmark"}))
    bookmark_paragraph.append(run("z_i"))
    bookmark_paragraph.append(element(W, "bookmarkEnd", **{qn(W, "id"): "10"}))
    append(body, bookmark_paragraph)
    hyperlink_paragraph = paragraph(run("Link: "))
    hyperlink = element(W, "hyperlink", **{qn(R, "id"): "rIdHyperlink"})
    append(hyperlink, run("a^2", color="0563C1", style="Hyperlink"))
    hyperlink_paragraph.append(hyperlink)
    append(body, hyperlink_paragraph)
    field_paragraph = paragraph(run("Field: "))
    field = element(W, "fldSimple", **{qn(W, "instr"): " PAGE "})
    append(field, run("x_i"))
    field_paragraph.append(field)
    append(body, field_paragraph)
    eq_field_paragraph = paragraph(run("EQ field: "))
    eq_field = element(W, "fldSimple", **{qn(W, "instr"): r" EQ x \s\up 2 "})
    append(eq_field, run("x2"))
    eq_field_paragraph.append(eq_field)
    append(body, eq_field_paragraph)
    drawing_paragraph = paragraph(run("Drawing-adjacent q_i "), drawing())
    append(body, drawing_paragraph)
    sdt = element(W, "sdt")
    append(sdt, element(W, "sdtPr"))
    content = element(W, "sdtContent")
    append(content, paragraph(run("Content-control r_i")))
    append(sdt, content)
    append(body, sdt)
    append(body, paragraph(native_omath("x")))
    append(body, native_omath(r"\frac{a}{b}", display=True))
    legacy_paragraph = paragraph(run("Embedded legacy equation object"))
    object_node = element(W, "object")
    shape = element(V, "shape", id="_x0000_i1025", type="#_x0000_t75")
    append(shape, element(V, "imagedata", **{qn(R, "id"): "rIdOlePreview"}))
    append(object_node, shape)
    append(
        object_node,
        element(
            O,
            "OLEObject",
            **{
                qn(R, "id"): "rIdOle",
                "Type": "Embed",
                "ProgID": "Equation.3",
                "DrawAspect": "Content",
                "ObjectID": "_12345678",
            },
        ),
    )
    legacy_paragraph.append(object_node)
    append(body, legacy_paragraph)
    append(body, text_paragraph("Semantic trap A: x_i^2"))
    append(body, text_paragraph(r"Semantic trap B: x_{i^2}"))
    append(body, text_paragraph(r"\frac{a}{b}", style="DisplayFormula", ppr_change=True))
    comment_paragraph = paragraph(run("Comment story reference"))
    comment_range(comment_paragraph, "0", run("c_i"))
    append(body, comment_paragraph)
    append(body, text_paragraph("Interval: (0, 1]"))
    sect_pr = element(W, "sectPr")
    append(sect_pr, element(W, "headerReference", **{qn(W, "type"): "default", qn(R, "id"): "rIdHeader"}))
    append(sect_pr, element(W, "footerReference", **{qn(W, "type"): "default", qn(R, "id"): "rIdFooter"}))
    append(body, sect_pr)
    return xml_bytes(root)


def story_xml(story: str, text: str) -> bytes:
    root = element(W, story)
    if story in {"footnotes", "endnotes"}:
        separator_id = "-1"
        note_local = "footnote" if story == "footnotes" else "endnote"
        separator = element(W, note_local, **{qn(W, "id"): separator_id})
        append(separator, paragraph(run("separator")))
        append(root, separator)
        note = element(W, note_local, **{qn(W, "id"): "1"})
        append(note, paragraph(run(text)))
        append(root, note)
    elif story == "comments":
        comment = element(W, "comment", **{qn(W, "id"): "0", qn(W, "author"): "Other Reviewer", qn(W, "date"): FIXED_DATE})
        append(comment, paragraph(run(text)))
        append(root, comment)
    else:
        append(root, paragraph(run(text)))
    return xml_bytes(root)


def styles_xml() -> bytes:
    root = element(W, "styles")
    defaults = element(W, "docDefaults")
    run_defaults = element(W, "rPrDefault")
    run_properties = element(W, "rPr")
    append(run_properties, element(W, "rFonts", **{qn(W, "ascii"): "Calibri", qn(W, "hAnsi"): "Calibri"}))
    append(run_defaults, run_properties)
    append(defaults, run_defaults)
    append(root, defaults)
    for style_id, name in (("Normal", "Normal"), ("DisplayFormula", "Display Formula"), ("ResponseText", "Response Text"), ("Hyperlink", "Hyperlink"), ("TableGrid", "Table Grid")):
        style_type = "character" if style_id == "Hyperlink" else "table" if style_id == "TableGrid" else "paragraph"
        style = element(W, "style", **{qn(W, "type"): style_type, qn(W, "styleId"): style_id})
        append(style, element(W, "name", **{qn(W, "val"): name}))
        append(root, style)
    return xml_bytes(root)


def settings_xml() -> bytes:
    root = element(W, "settings")
    append(root, element(W, "trackRevisions"))
    return xml_bytes(root)


def content_types_xml() -> bytes:
    root = element(CT, "Types")
    append(root, element(CT, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml"))
    append(root, element(CT, "Default", Extension="xml", ContentType="application/xml"))
    append(root, element(CT, "Default", Extension="png", ContentType="image/png"))
    append(root, element(CT, "Default", Extension="bin", ContentType="application/vnd.openxmlformats-officedocument.oleObject"))
    overrides = {
        "/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "/word/styles.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
        "/word/settings.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
        "/word/header1.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
        "/word/footer1.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
        "/word/footnotes.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
        "/word/endnotes.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
        "/word/comments.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        "/word/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml",
        "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
        "/docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    }
    for part, content_type in overrides.items():
        append(root, element(CT, "Override", PartName=part, ContentType=content_type))
    return xml_bytes(root)


def core_properties_xml() -> bytes:
    root = element(CP, "coreProperties")
    title = ET.SubElement(root, qn(DC, "title"))
    title.text = "Synthetic adversarial formula corpus"
    creator = ET.SubElement(root, qn(DC, "creator"))
    creator.text = "word-formula-omml tests"
    modified = ET.SubElement(root, qn(DCTERMS, "modified"), {qn(XSI, "type"): "dcterms:W3CDTF"})
    modified.text = FIXED_DATE
    return xml_bytes(root)


def app_properties_xml() -> bytes:
    return b"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><Properties xmlns=\"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties\"><Application>word-formula-omml fixture builder</Application></Properties>"


def root_rels() -> bytes:
    rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    core_type = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
    app_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties"
    custom_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
    return rels_xml(
        [
            relationship("rIdOfficeDocument", rel_type, "word/document.xml"),
            relationship("rIdCore", core_type, "docProps/core.xml"),
            relationship("rIdApp", app_type, "docProps/app.xml"),
            relationship("rIdCustom", custom_type, "customXml/item1.xml"),
        ]
    )


def document_rels() -> bytes:
    base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    return rels_xml(
        [
            relationship("rIdStyles", base + "styles", "styles.xml"),
            relationship("rIdSettings", base + "settings", "settings.xml"),
            relationship("rIdHeader", base + "header", "header1.xml"),
            relationship("rIdFooter", base + "footer", "footer1.xml"),
            relationship("rIdFootnotes", base + "footnotes", "footnotes.xml"),
            relationship("rIdEndnotes", base + "endnotes", "endnotes.xml"),
            relationship("rIdComments", base + "comments", "comments.xml"),
            relationship("rIdImage", base + "image", "media/image1.png"),
            relationship("rIdHyperlink", base + "hyperlink", "https://example.invalid/synthetic", external=True),
            relationship("rIdOle", base + "oleObject", "embeddings/oleObject1.bin"),
            relationship("rIdOlePreview", base + "image", "media/image1.png"),
        ]
    )


def png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


def build_fixture_package() -> dict[str, bytes]:
    """Return package parts in sorted-name-independent form."""

    return {
        "[Content_Types].xml": content_types_xml(),
        "_rels/.rels": root_rels(),
        "customXml/item1.xml": b"<?xml version=\"1.0\" encoding=\"UTF-8\"?><fixture xmlns=\"urn:word-formula-omml:test\">protected</fixture>",
        "docProps/app.xml": app_properties_xml(),
        "docProps/core.xml": core_properties_xml(),
        "word/_rels/document.xml.rels": document_rels(),
        "word/comments.xml": story_xml("comments", "Comment formula c_i"),
        "word/document.xml": document_xml(),
        "word/embeddings/oleObject1.bin": b"synthetic-legacy-equation-object-v1\n",
        "word/endnotes.xml": story_xml("endnotes", "Endnote formula e_i"),
        "word/footer1.xml": story_xml("ftr", "Footer formula f_i"),
        "word/footnotes.xml": story_xml("footnotes", "Footnote formula n_i"),
        "word/header1.xml": story_xml("hdr", "Header formula h_i"),
        "word/media/image1.png": png_bytes(),
        "word/settings.xml": settings_xml(),
        "word/styles.xml": styles_xml(),
    }


def package_sha256(package: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(package):
        encoded_name = name.encode("utf-8")
        data = package[name]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def write_fixture(path: str | Path) -> str:
    """Write a reproducible DOCX and return its file SHA-256."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    package = build_fixture_package()
    with target.open("wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(package):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, package[name])
    return hashlib.sha256(target.read_bytes()).hexdigest()


def load_expectations() -> dict:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
