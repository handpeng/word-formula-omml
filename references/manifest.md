# Formula Manifest

Build the manifest at occurrence level, not at unique-formula level. Repeated formula text can have different colors, styles, revision ancestry, or surrounding content.

## Recommended JSON Shape

```json
{
  "source_sha256": "<sha256>",
  "revision_author": "Codex Formula Remediation",
  "formulas": [
    {
      "id": "F-001",
      "paragraph": 42,
      "sequence_in_paragraph": 1,
      "anchor_before": "The calibrated loss is ",
      "source": "L_Total = 0.5857 +/- 0.0294",
      "anchor_after": " across matched splits.",
      "latex": "\\mathcal{L}_{\\mathrm{Total}}=0.5857\\pm0.0294",
      "layout": "inline",
      "expected_matches": 1,
      "run_index": 3,
      "run_start": 23,
      "run_end": 54,
      "color": "0000FF",
      "paragraph_style": "ResponseText",
      "inside_existing_revision": false,
      "adjacent_bookmark": false,
      "adjacent_field": false,
      "adjacent_hyperlink": false,
      "adjacent_drawing": false,
      "status": "approved"
    }
  ]
}
```

The OMML library generator accepts either this object form or a bare array of formula records. It requires only `id`, `latex`, and optional `layout`; the remaining fields control safe application and audit.

## Versioned contract

New manifests are written with `schema_version: 1`. The shared
`word_formula_omml.contract` module is the only schema/status implementation;
inventory, recovery, generation, application, and audit stages must extend
these records instead of defining parallel dictionaries.

The loader accepts the historical bare array and the historical object without
`schema_version` as a compatibility migration to version 1. An explicitly
unknown schema version, enum value, required field, or top-level/record field
is rejected. Forward-compatible additions belong under an `extensions` object.

The manifest identity is deterministic and derived from its source binding,
occurrences, and immutable metadata. A frozen job additionally binds that
identity to the source SHA-256, occurrence IDs, and an exact non-empty set of
requested deliverables. A string shorthand such as `"clean"` uses the
conservative required gates `STRUCTURAL_AUDIT` and `NATIVE_WORD`; callers may
provide an explicit gate list when a staging-only artifact is intentionally
being modeled. Each requested artifact has its own deterministic ID,
candidate hash, required gates, and candidate-bound evidence. Job delivery
status is derived, never caller-set:

- `COMPLETE` requires successful terminal accounting for every occurrence and
  `PASS` evidence for every required gate on every requested artifact.
- `PARTIAL_REVIEW_OUTPUT` represents unresolved occurrences or missing/stale
  evidence and is never a final deliverable.
- `FAILED` represents an explicit failed occurrence or gate.

Changing the source, frozen manifest, requested artifact set, or candidate
content invalidates the relevant job/evidence and requires a new freeze or
validation cycle. A redlined artifact's evidence cannot satisfy a clean
artifact's gates.

## Manifest Rules

- IDs must be unique and stable across retries.
- Preserve the source exactly, including spaces and operator spelling. Put normalization only in `latex`.
- Use paragraph number plus before/after anchors. Paragraph number alone is not stable enough after edits.
- Record offsets against the current accepted text (`w:t`), not text inside deletions (`w:delText`).
- Detect overlaps longest-first. No two approved occurrences may overlap.
- Map each occurrence to actual run boundaries. A single-run mapping is the safest automatic case.
- Mark every protected ancestor or adjacent structure. A `false` value is an observed fact, not a default assumption.
- Derive color and emphasis from the source run or surrounding semantic block. Do not assign one global formula color.
- Require one match unless the manifest deliberately distinguishes repeated occurrences with anchors and sequence numbers.

## Review Before Application

Confirm that each LaTeX expression expresses the intended mathematics, not merely a syntactic rewrite. Pay particular attention to:

- multi-character subscripts and roman labels;
- unary minus versus subtraction;
- exponent grouping;
- multiplication signs and scientific notation;
- open/closed interval boundaries;
- `<=`/`>=` direction;
- implicit products versus function calls;
- bold vectors and matrices;
- inline versus display layout.

Freeze the reviewed manifest before editing OOXML. If the source document changes afterward, rebuild the manifest from the new source rather than relocating by guesswork.
