---
name: word-formula-omml
description: Convert LaTeX or plain-text mathematical notation embedded in existing DOCX files into native editable Word OMML equations while preserving surrounding formatting, tracked changes, media, and package integrity. Use when Word formulas remain as raw text such as x_i, Greek commands, comparison operators, or delimited LaTeX; not for general prose editing or equation conversion outside Word.
---

# Word Formula OMML

Convert only formula occurrences the user has authorized. Treat the source DOCX as immutable and write results to new files.

## Required Companion Guidance

Use this skill with the `docx` skill. Before editing an existing DOCX, read the complete `docx/SKILL.md` and `docx/ooxml.md`, then use its unpack, pack, validation, and tracked-change conventions. Formula replacement requires direct OOXML work because native equations are `m:oMath` or `m:oMathPara` nodes, not ordinary text runs.

## Choose The Deliverables

- For another person's academic, legal, business, or submission document, produce a redlined DOCX and a clean DOCX by default.
- For a personal draft where the user explicitly requests direct cleanup, a clean DOCX alone is acceptable.
- If the request is only to inspect or assess feasibility, do not modify files.

Never overwrite the source. Record its SHA-256 before editing and confirm it is unchanged at delivery.

## Workflow

1. Inventory candidate formulas and capture package, revision, style, drawing, relationship, and media baselines.
2. Build an occurrence-level manifest. Every row needs an exact source span, normalized LaTeX, stable paragraph context, expected match count, layout, style/color source, and protected-container status.
3. Have the user or an authoritative source resolve ambiguous notation. Do not infer mathematical meaning from malformed text.
4. Generate reference OMML with `scripts/generate_omml_library.py`; use Pandoc's math writer instead of hand-building complex equation trees.
5. Apply replacements in a task-specific OOXML script. Preserve unchanged run fragments, paragraph properties, pre-existing revisions, bookmarks, fields, hyperlinks, drawings, and content controls.
6. Create the clean version by accepting only this session's insertion/deletion pairs. Never accept or reject another author's revisions.
7. Repack to temporary paths, validate, and atomically deliver new DOCX files only after every check passes.

Read [references/workflow.md](references/workflow.md) before implementing a conversion. Read [references/manifest.md](references/manifest.md) when building or reviewing the occurrence manifest.

## Non-Negotiable Checks

- A formula occurrence must resolve to exactly one intended location.
- Rejecting this session's revisions must reproduce the source's visible text for every affected paragraph.
- The clean document must contain native OMML and no targeted raw formula syntax.
- All new math runs must use Cambria Math while retaining the surrounding semantic color and emphasis.
- Media hashes, drawing counts, relationships, sections, comments, and unrelated package parts must remain unchanged unless the user explicitly authorized otherwise.
- Both outputs must pass strict OOXML/package validation and open in Microsoft Word without a repair prompt.
- Visually inspect representative inline, display, subscript, superscript, fraction, inequality, Greek, interval, and scientific-notation cases in Word.

Use `scripts/audit_docx_formulas.py` for a repeatable package/formula audit. It supplements, but does not replace, the DOCX skill validator and native Word inspection.

## Stop Conditions

Stop and report the affected occurrence instead of guessing when a formula crosses unsafe run boundaries, appears inside another author's revision, or intersects a hyperlink, bookmark, field, drawing, or content control without an explicit preservation plan. Also stop when exact-match counts, source hashes, revision counts, or protected package parts drift from the baseline.
