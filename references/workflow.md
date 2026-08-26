# Conversion And Validation Workflow

## 1. Preflight

1. Confirm Word is closed or that the source is otherwise not being edited.
2. Hash the source DOCX and copy it to a temporary working directory.
3. Unpack the working copy with the DOCX skill's unpacker.
4. Capture a baseline containing:
   - package part names and hashes;
   - relationships and content types;
   - media names and hashes;
   - paragraph, equation, drawing, section, comment, field, bookmark, hyperlink, and content-control counts;
   - all revision counts grouped by author;
   - canonical copies of pre-existing `w:ins`, `w:del`, `w:pPrChange`, and `w:rPrChange` nodes;
   - paragraph/run text, properties, and revision ancestry.
5. Extract current text from `w:t` and review deleted text separately from `w:delText`. Do not flatten the two views together when locating edits.

## 2. Inventory And Normalize

Search broadly for explicit LaTeX delimiters and likely plain-text notation, but treat detection as candidate generation only. Common signals include backslash commands, `_`, `^`, `+/-`, inequalities, Greek names, scientific notation, bracketed vectors/intervals, and assignment expressions.

Build and review the occurrence manifest described in [manifest.md](manifest.md). Resolve ambiguous cases from the manuscript, PDF, author instructions, or another authoritative source. Preserve identifiers that are code or variable names as styled text when they are not mathematical expressions.

## 3. Generate Trusted OMML Templates

Create a manifest containing the approved `id`, `latex`, and `layout` fields, then run:

```bash
python3 scripts/generate_omml_library.py formula-manifest.json omml-library.docx
```

The script sends a Pandoc JSON AST to Pandoc and writes one marker plus one equation paragraph per formula. It also writes `omml-library.index.json`. Parse the library DOCX and deep-copy the corresponding `m:oMath` or `m:oMathPara` node into the target document.

Do not copy paragraph properties, styles, numbering, or relationships from the library. It is an equation-node source only. Reject any entry that does not yield exactly one native equation.

## 4. Apply Minimal Tracked Replacements

Use a dedicated revision author such as `Codex Formula Remediation`. Allocate unique revision IDs higher than every existing `w:id` and enable revision tracking without disturbing existing settings.

For a safe occurrence wholly contained in one ordinary `w:r`:

1. Clone the original run for unchanged prefix and suffix text, retaining its `w:rPr`, RSID, whitespace behavior, and other valid attributes.
2. Put only the replaced source characters in a new `w:del` with `w:delText`.
3. Put a deep copy of the native OMML node in a new `w:ins` owned by this session.
4. Apply Cambria Math to math runs and preserve contextual color. Preserve bold/italic only when mathematically intended.
5. Keep unchanged text outside both revision wrappers.

When an occurrence spans runs, first prove that the intervening structure is semantically mergeable and contains no protected nodes. Otherwise stop. Never globally replace serialized XML or flatten a paragraph to reconstruct it.

For formulas already inside another author's insertion/deletion, follow the DOCX skill's nested revision rules. Do not rewrite the other author's node or transfer its authorship.

## 5. Named Styles

Add named styles only when the user requested style remediation or when the document already uses a coherent style scheme that needs completion. Use collision-resistant IDs and readable names. Preserve direct formatting that carries reviewer-response semantics, especially color.

Keep style changes separate from formula replacement in the manifest and audit. Equation formatting belongs on OMML math runs; paragraph and character styles should not force a non-math font onto equations.

## 6. Produce Redlined And Clean Outputs

Pack and validate the redlined version first. Make the clean version from a copy of the validated redlined package by accepting only revisions whose `w:author` exactly equals this session's author:

- remove this author's `w:del` nodes;
- unwrap this author's `w:ins` nodes in place;
- preserve every revision from every other author;
- preserve all `w:pPrChange` and `w:rPrChange` nodes unless explicitly in scope.

Check the expected insertion/deletion count before accepting anything. A mismatch is a hard failure.

## 7. Structural Audit

Run the supplied audit, for example:

```bash
python3 scripts/audit_docx_formulas.py corrected.docx \
  --baseline source.docx \
  --expected-formulas 95 \
  --require-cambria-math \
  --residual 'L_Total|gamma_|\\+/-|>=|10\\^-'
```

Also run the DOCX skill's strict pack/unpack validator. Verify these invariants independently:

- source hash unchanged;
- expected formula and session revision counts;
- no targeted raw notation in current clean text;
- rejecting session revisions reconstructs the source paragraph text;
- pre-existing revisions are canonically unchanged;
- media hashes and relationship targets unchanged;
- no lost drawings, sections, comments, bookmarks, fields, hyperlinks, or content controls;
- only authorized package parts differ;
- every manifest row maps to one expected OMML template.

## 8. Native Word Validation

Open both packed outputs in Microsoft Word through normal UI or COM automation and confirm there is no repair prompt. Check equation count, named styles, images, and revision authors.

Visually inspect a risk-based sample covering every formula family, semantic color, layout, and document region. Include the longest formula and formulas near images, tables, page breaks, or tracked content. Check clipping, overlap, baseline alignment, superscripts/subscripts, fractions, delimiters, operator direction, font, and color leakage.

Prefer Word PDF export for page-level review. If export hangs, terminate only the automation instance after confirming it is safe, reopen Word cleanly, and use Word-native range rendering such as `Range.EnhMetaFileBits` for representative paragraphs. Record the fallback rather than claiming a full-page PDF review.

## 9. Delivery

Move validated temporary packages to the final output names atomically. Report paths, hashes, formula counts, revision behavior, style/media preservation, validator results, native Word results, and any visual-validation fallback. Keep the source and temporary audit artifacts until the user accepts the outputs.
