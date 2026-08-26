# TODO — V1 Word Formula Recovery & Native Equation Remediation

## 0. Status and execution rule

This file is the implementation authority for the next major evolution of `word-formula-omml`.

The repository is moving from a **safe LaTeX/plain-text-to-OMML conversion skill** to a **Word formula recovery and native-equation remediation engine** that can recover damaged or heterogeneous mathematical notation, preserve mathematical meaning, adapt to Word context styles, and prove that unrelated document content was not changed.

Implementation must proceed through focused GitHub Issues and PRs. Each implementation PR must be reviewed against its actual diff, tests, and acceptance evidence. A PR may merge only when its issue-level acceptance criteria pass and the diff introduces no unresolved semantic, OOXML, revision-preservation, compatibility, or workflow risk.

This TODO is intentionally stricter than a feature wishlist. Any implementation that violates a non-negotiable invariant below is incomplete even if its happy-path conversion appears correct.

---

## 1. Problem statement

Real DOCX formula failures are not limited to valid LaTeX. Inputs may include:

- valid or nearly valid LaTeX;
- partial or malformed LaTeX;
- LaTeX with lost escapes or delimiters;
- plain-text mathematics such as `x_i`, `>=`, `+/-`, `10^-3`;
- Unicode math such as `α`, `≤`, `±`, superscript characters, or mixed Unicode/text notation;
- mojibake or encoding corruption such as broken inequality or plus/minus symbols;
- formulas fragmented across Word runs because of styling, revisions, bookmarks, fields, or editing history;
- already-native OMML that should be preserved or diagnosed rather than reconverted;
- legacy Word equation forms such as EQ fields or embedded equation objects;
- repeated formulas whose correct color, emphasis, layout, or revision ancestry differs by occurrence.

The desired output is not merely “text that looks mathematical.” The system must produce **native editable Word equations with the intended mathematical semantics**, preserve or deliberately resolve the surrounding Word style context, and provide evidence that unrelated package content and pre-existing revisions remain intact.

---

## 2. V1 product definition

V1 is defined as the following controlled pipeline:

```text
DOCX
  -> package/story inventory
  -> formula candidate inventory
  -> source-type classification
  -> evidence-backed recovery and normalization
  -> canonical formula representation
  -> trusted OMML generation
  -> context style resolution
  -> fail-closed OOXML application
  -> structural/revision/semantic audit
  -> native Word validation evidence
  -> redlined and/or clean deliverables
```

The engine must support automation where safety can be proven and explicitly stop where safety or mathematical intent cannot be proven.

### 2.1 Fidelity levels

V1 must distinguish three different claims:

1. **Semantic fidelity — required.** Operators, grouping, indices, exponents, roots, fractions, delimiters, matrices, accents, vectors, limits, intervals, scientific notation, and other mathematical structure must mean the same thing as the approved source.
2. **Word-native layout fidelity — required.** Inline/display behavior, baseline, clipping, formula size, contextual color, and semantic emphasis must be appropriate in Microsoft Word.
3. **Pixel-identical TeX rendering — not guaranteed.** Native OMML uses the Office math layout engine and math fonts, so pixel identity with TeX is not a V1 acceptance criterion. Where comparison is available, it is validation evidence, not a promise of identical glyph metrics.

### 2.2 Delivery completeness levels

Every run must distinguish:

- `COMPLETE` — every authorized occurrence was converted or intentionally preserved under an approved rule and all gates passed;
- `PARTIAL_REVIEW_OUTPUT` — a useful artifact was produced but one or more authorized occurrences remain unresolved/refused;
- `FAILED` — required integrity, semantic, package, revision, or validation gates did not pass.

A partial artifact must never be named, reported, or delivered as a complete corrected document. Final clean/redlined deliverables are `COMPLETE` only when all authorized occurrences are accounted for, including explicitly approved exclusions.

---

## 3. Non-negotiable invariants

These apply to every issue and implementation PR.

### 3.1 Source and package safety

- Never overwrite the source DOCX.
- Record the source SHA-256 before editing and prove the source file is unchanged at delivery.
- Never perform blind global replacement over serialized OOXML or flattened paragraph text.
- Never silently relocate a manifest occurrence after the source hash or frozen structural anchors have changed.
- Preserve unrelated package parts, relationships, media, sections, comments, fields, bookmarks, hyperlinks, drawings, content controls, and custom XML unless explicitly in scope.

### 3.2 Revision safety

- Never accept, reject, rewrite, re-author, or structurally normalize another author’s tracked changes merely to simplify formula replacement.
- Pre-existing `w:ins`, `w:del`, `w:pPrChange`, and `w:rPrChange` nodes must be fingerprinted/canonicalized and verified, not checked only by count.
- Creating a clean output may accept only the dedicated revision author for the current remediation session.
- Rejecting the current session’s formula revisions must reconstruct the expected source-visible text for every affected location.

### 3.3 Mathematical and generation safety

- Detection is candidate generation, not proof of mathematical intent.
- Malformed, ambiguous, or corrupted notation must not be guessed solely to keep the pipeline moving.
- Recovery must record its evidence source and confidence.
- Ambiguous cases must enter a review queue unless an authoritative source resolves them.
- Existing native OMML must not be reconverted by default.
- A generated OMML node is not accepted merely because Pandoc produced exactly one `m:oMath` node. For supported formula families, generated OMML must be parsed/normalized and checked against the approved canonical mathematical representation.
- Unsupported LaTeX commands, Pandoc conversion diagnostics that may alter meaning, or structures that cannot be semantically validated must fail closed or require explicit review.

### 3.4 Style safety

- Mathematical glyphs must use a Word-compatible math font strategy; contextual fidelity must not be implemented by forcing an ordinary prose font into OMML when that breaks math rendering.
- Color, size, highlight, revision context, inline/display role, and mathematically meaningful emphasis must be resolved per occurrence.
- Repeated source strings may have different style outcomes and therefore remain occurrence-level entities.

### 3.5 Fail-closed rule

Any automatic applicator must refuse a case it cannot prove safe. Unsupported or risky occurrences must be emitted as explicit statuses such as `NEEDS_REVIEW` or `NEEDS_SPECIAL_HANDLER`; they must never be silently skipped, coerced, or partially replaced.

### 3.6 Batch and transactional safety

- Build and freeze an application plan before modifying OOXML; record the exact source hash, approved occurrence IDs, expected match counts, and intended action for every occurrence.
- Every occurrence must end in a terminal accounted status; no occurrence may disappear between inventory, recovery, application, and audit.
- Per-occurrence replacement may be transactional, but batch completion is separate: any unresolved authorized occurrence downgrades the batch to `PARTIAL_REVIEW_OUTPUT` unless the user explicitly approves exclusion.
- Temporary/intermediate files may exist during processing, but validated final paths must be written atomically and only after the corresponding delivery gate passes.

---

## 4. Evidence precedence and ambiguity policy

Formula recovery must use evidence in this order when available:

1. author-approved formula manifest or explicit user instruction;
2. original TeX/LaTeX source corresponding to the document location;
3. authoritative compiled PDF or another author-approved rendered source;
4. an earlier/later trusted manuscript version with stable location mapping;
5. local Word context and mathematical syntax;
6. heuristic normalization or corruption repair.

A lower-ranked source must not silently override a conflicting higher-ranked source.

Each recovered occurrence must record at least:

- original source text;
- source type;
- normalized/recovered mathematical representation;
- evidence source(s);
- confidence category;
- unresolved ambiguity list;
- occurrence anchors and package/story location;
- style-resolution inputs;
- application eligibility/status.

Suggested confidence categories:

- `AUTHORITATIVE` — directly backed by approved source;
- `HIGH` — unambiguous normalization with strong structural/context evidence;
- `REVIEW_REQUIRED` — multiple plausible mathematical interpretations or unsafe structure;
- `UNRECOVERABLE` — insufficient evidence to produce a trustworthy equation.

Only `AUTHORITATIVE` and policy-approved `HIGH` cases may enter fully automatic application.

---

## 5. Canonical formula representation and manifest evolution

The current occurrence manifest is retained as the safety backbone, but V1 must version and extend it rather than replacing it with ad hoc per-script dictionaries.

### 5.1 Schema requirements

Introduce a versioned manifest schema, for example `schema_version: 1`, with backward-compatible loading of the current `id`/`latex`/`layout` subset used by `generate_omml_library.py`.

The V1 occurrence record should be able to represent:

- package part/story (`document`, header, footer, footnote, endnote, comment where supported);
- paragraph and stable anchors;
- source run boundaries and revision ancestry;
- source type classification;
- exact raw source;
- normalized LaTeX where available;
- canonical formula IR/AST or another deterministic semantic representation;
- evidence and confidence;
- ambiguity notes;
- protected-container flags;
- contextual style snapshot and resolved math style;
- target layout;
- expected match count;
- application status;
- OMML template identity/hash;
- semantic-validation outcome;
- audit outcome.

### 5.2 Canonical representation goals

The representation must distinguish semantically different structures that may look textually similar, including at minimum:

- `x_i^2` vs. `x_{i^2}`;
- unary minus vs. subtraction;
- exponent grouping;
- multi-character subscripts and roman labels;
- implicit products vs. function calls where determinable;
- `<=`/`>=` direction;
- open/closed intervals;
- scalar vs. bold/vector semantics;
- inline vs. display layout.

Do not require a full computer-algebra system for V1. The representation only needs to be rich enough for deterministic recovery, comparison, generation, and audit of supported formula families.

### 5.3 OMML semantic round-trip goal

For formula families declared supported for automatic application, the system must be able to derive a deterministic semantic representation from generated OMML and compare it with the approved canonical representation. Formatting-only properties may differ, but grouping, operators, scripts, fraction/root/matrix structure, delimiters, accents, and other supported semantic structure must match.

If a formula can be generated but cannot be semantically checked under the current support matrix, it must not be promoted to high-confidence automatic conversion merely because generation succeeded.

---

## 6. Planned workstreams

### W0 — Versioned manifest schema, compatibility layer, and status model (P0)

Deliverables:

- define and document the V1 schema and enums;
- add schema validation;
- load current minimal manifests without breaking `generate_omml_library.py`;
- define deterministic IDs and retry stability;
- define application/recovery statuses, delivery completeness states, and confidence categories;
- add migration/compatibility tests.

Acceptance:

- existing documented generator examples still work;
- invalid/unknown required fields fail clearly;
- schema round trips deterministically;
- a frozen manifest cannot be reused against a changed source hash without explicit rebuild;
- every occurrence and every batch has an explicit lifecycle/completion status.

Dependencies: none.

### W1 — Risk corpus, fixtures, and test harness (P0)

Build a small but deliberately adversarial DOCX corpus. It must include synthetic/minimal fixtures and, where licensing/privacy permits, a sanitized structure derived from a real conversion case.

Coverage must include at least:

- valid LaTeX and plain-text math;
- Unicode operators and Greek symbols;
- malformed/lost-escape notation;
- representative mojibake/corruption patterns;
- single-run and multi-run formulas;
- different colors/sizes/emphasis for repeated formula text;
- existing tracked changes from another author;
- formulas near/in tables, bookmarks, hyperlinks, fields, drawings, and content controls;
- existing OMML;
- inline and display equations;
- subscripts, superscripts, fractions, roots, inequalities, Greek, intervals, scientific notation;
- negative cases that must fail closed;
- semantically dangerous near-matches such as script/grouping changes;
- batch cases containing both safe and refused occurrences.

Acceptance:

- fixtures are deterministic and contain no sensitive/private manuscript content;
- expected package invariants and formula outcomes are machine-readable;
- expected canonical semantics and expected refusal reasons are machine-readable for supported cases;
- tests can demonstrate success, semantic mismatch detection, and deliberate refusal paths.

Dependencies: W0 may evolve in parallel but fixture expectations must use the final schema before closure.

### W2 — Package/story-aware formula inventory and source-type classifier (P0)

Add read-only inventory tooling that discovers candidates without editing the document.

Classify at least:

- `RAW_LATEX`;
- `PARTIAL_LATEX`;
- `PLAIN_MATH`;
- `UNICODE_MATH`;
- `CORRUPTED_TEXT`;
- `EXISTING_OMML`;
- `EQ_FIELD` where detectable;
- `EMBEDDED_EQUATION_OBJECT`/legacy object where detectable;
- `UNKNOWN_FORMULA`.

Inventory must retain run boundaries, accepted/deleted text separation, revision ancestry, protected ancestors/adjacency, story/part, anchors, and contextual style evidence.

Acceptance:

- inventory is strictly read-only;
- no candidate is presented as approved mathematics merely because it matched a detector;
- repeated occurrences remain distinct;
- current text and deleted text are not flattened into one matching surface;
- unsupported parts or objects are reported explicitly.

Dependencies: W0, W1 fixtures.

### W3 — Evidence-backed recovery, corruption normalization, canonical formula IR, and generator semantic contract (P0)

Implement deterministic normalization/recovery for supported families, including safe normalization of common operator spellings, Unicode forms, lost LaTeX delimiters/escapes where context proves math intent, and selected corruption patterns.

Requirements:

- corruption rules are context-gated; ordinary prose tokens such as “alpha” must not be globally converted;
- every transformation is traceable from raw source to normalized form;
- conflicting evidence produces review-required status;
- supported recovered formulas produce canonical IR plus normalized LaTeX;
- ambiguous grouping or mathematical meaning fails closed;
- `generate_omml_library.py` or its replacement must surface relevant Pandoc errors/diagnostics instead of treating “one OMML node generated” as semantic success;
- define the supported formula families for which OMML semantic round-trip validation is implemented.

Acceptance:

- positive and negative cases are covered by W1 fixtures;
- semantic distinctions listed in section 5.2 are preserved;
- no heuristic rule can silently override authoritative evidence;
- tests prove that a deliberately altered script/group/operator structure is detected as a semantic mismatch;
- unsupported/unknown LaTeX constructs do not silently enter the high-confidence path.

Dependencies: W0, W1, W2.

### W4 — Context Style Resolver (P0)

Build an explicit per-occurrence style-resolution layer instead of copying one run property blindly.

It must reason about:

- direct run formatting;
- character style;
- paragraph style;
- semantic block/revision context where detectable;
- color;
- font size;
- highlight/underline when relevant;
- mathematically meaningful bold/italic;
- inline/display role;
- paragraph alignment/spacing for display equations;
- math-font compatibility.

Use a documented precedence model such as occurrence explicit style -> source run context -> semantic block -> character style -> paragraph style -> document default, with conflict reporting.

Acceptance:

- repeated identical formulas can resolve to different valid styles;
- Word-compatible math font behavior is preserved;
- color/size/emphasis do not leak into surrounding text;
- unresolved style conflicts fail closed or enter review rather than choosing arbitrarily.

Dependencies: W0, W1, W2.

### W5 — Fail-closed safe OOXML applicator and frozen application plan (P0)

Create a reusable applicator for provably safe cases while preserving the current rule that complex structures may require task-specific handlers.

Automatic V1 eligibility should initially be limited to cases whose exact occurrence, structural boundaries, semantics, and preservation plan are proven. At minimum, the default fast path should support an ordinary single-run occurrence with exact anchors and no unsafe protected intersection.

Requirements:

- build a dry-run/frozen application plan before writes;
- minimal tracked replacement;
- unique revision IDs above existing IDs;
- dedicated revision author;
- unchanged prefix/suffix run fragments preserved;
- native OMML inserted only from semantically validated trusted templates;
- resolved style applied without rewriting unrelated run/paragraph properties;
- atomic output to new files;
- structured refusal reason for every ineligible occurrence;
- terminal status for every planned occurrence.

Acceptance:

- no global replace path exists;
- multi-run/protected/revision-intersecting cases are handled only when a preservation algorithm is explicitly implemented and tested; otherwise they refuse;
- rejecting session revisions reconstructs source-visible text at affected locations;
- clean output accepts only the session author’s revisions;
- a batch with unresolved authorized occurrences cannot be reported as `COMPLETE`;
- a crash/failure before validation cannot replace a validated final artifact.

Dependencies: W0, W1, W3, W4.

### W6 — Audit hardening, invariant fingerprints, and semantic OMML verification (P0)

Extend `audit_docx_formulas.py` from count-based structural checks to stronger invariants.

Required additions:

- canonical fingerprints of pre-existing `w:ins`, `w:del`, `w:pPrChange`, and `w:rPrChange` grouped by identity/author/location;
- affected-paragraph reconstruction check after rejecting the current remediation revisions;
- manifest-to-OMML one-to-one verification;
- OMML-to-canonical semantic comparison for supported formula families;
- package part allowlist/diff report with explicit reasons;
- style outcome checks for expected occurrence color/size/emphasis where represented in the manifest;
- source hash and frozen-manifest/application-plan consistency checks;
- story/part-aware formula counts as support expands;
- complete accounting of occurrence terminal statuses;
- machine-readable JSON report suitable for CI.

Acceptance:

- tests demonstrate detection of a changed pre-existing revision even when revision counts remain equal;
- tests demonstrate detection of unrelated package drift;
- tests demonstrate successful reconstruction of source-visible affected text;
- tests demonstrate detection of generated OMML whose mathematical structure differs from the approved canonical structure;
- tests demonstrate that an unaccounted occurrence prevents a `COMPLETE` result;
- current documented audit usage remains supported or receives a documented compatible migration.

Dependencies: W0, W1; integration with W3 and W5 before P0 completion.

### W7 — CI and reproducible P0 acceptance gates (P0)

Add automated tests for schema, inventory, recovery, canonical semantics, OMML generation/semantic verification, style resolution, application, audit, and negative fail-closed cases.

CI should run on a supported Python matrix and a pinned/declared Pandoc compatibility range. Native Microsoft Word validation may remain a Windows/manual or controlled acceptance gate if it cannot run reliably in ordinary CI, but the boundary must be explicit.

Acceptance:

- CI is deterministic from a clean checkout;
- required dependencies and versions are documented;
- no test requires private documents;
- failure output identifies the violated invariant rather than only returning a generic non-zero status;
- semantic mismatch and partial-batch cases are first-class regression tests.

Dependencies: W0-W6 sufficient to exercise end-to-end P0.

### W8 — Existing/legacy equation adapters and broader Word stories (P1)

Expand beyond raw text in `word/document.xml`.

Scope:

- preserve/diagnose existing OMML;
- detect and, where safe and specified, convert EQ fields;
- identify embedded/legacy equation objects and report supported/unsupported status;
- extend inventory/audit/application to supported headers, footers, footnotes, endnotes, and comments without assuming every part is editable by the same algorithm.

Acceptance:

- existing OMML is not needlessly regenerated;
- unsupported embedded equation technologies are reported without destructive fallback;
- each newly supported story has fixtures and package-invariant tests.

Dependencies: P0 complete.

### W9 — TeX/authoritative-source alignment and render comparison (P1)

Add optional source alignment and validation evidence when original TeX or an authoritative PDF is available.

Scope:

- map approved TeX expressions to Word occurrences through explicit anchors/evidence, not fuzzy replacement alone;
- render normalized TeX for reference where practical;
- render/export Word equations through native Word automation where available;
- compare canonical semantics deterministically and use visual comparison as supporting evidence;
- report that native OMML is not guaranteed to be pixel-identical to TeX.

Acceptance:

- alignment conflicts stop rather than silently selecting a nearby formula;
- semantic comparison is primary;
- visual comparison thresholds, limitations, and fallback paths are documented;
- Word repair prompts or export failures are hard failures for high-confidence delivery.

Dependencies: P0 complete; W8 independent unless legacy inputs are involved.

### W10 — Orchestrator, review queue, reporting, and batch ergonomics (P1)

Provide a coherent task workflow over the individual tools without hiding safety decisions.

Scope:

- preflight command/workflow;
- candidate inventory and manifest draft;
- review/ambiguity queue;
- approved recovery/generation/application;
- audit and delivery report;
- resume/retry using frozen hashes and stable IDs;
- per-occurrence status reporting for batches;
- explicit `COMPLETE` / `PARTIAL_REVIEW_OUTPUT` / `FAILED` result classification.

Acceptance:

- partial batch success never masquerades as full success;
- every refused/skipped occurrence has an explicit reason;
- every authorized occurrence is accounted for in the final report;
- retries are idempotent against unchanged inputs;
- changed inputs require rebuild/re-approval rather than silent relocation;
- partial artifacts use unmistakable non-final naming/reporting and are never substituted for validated complete deliverables.

Dependencies: P0, W8/W9 integrations as applicable.

### W11 — Documentation and V1 release contract (P1)

Update `README.md`, `SKILL.md`, and references only after the implementation contract is stable.

Documentation must state:

- supported input classes;
- confidence/review behavior;
- semantic vs. visual fidelity guarantees;
- OMML semantic-validation support matrix;
- style inheritance rules;
- fail-closed boundaries;
- batch completeness semantics;
- required dependencies;
- native Word validation expectations;
- what remains unsupported;
- examples for simple conversion, damaged formula recovery, and a deliberately refused ambiguous case.

Acceptance:

- docs match actual executable behavior and tests;
- no README claim exceeds the tested support matrix;
- old examples either remain valid or include an explicit migration path.

Dependencies: W0-W10 relevant functionality complete.

### W12 — Optional image formula recovery research (P2, not a V1 blocker)

Image/OCR recovery is intentionally outside the V1 completion gate because it has materially different error modes.

A future research issue may evaluate formula images when no trustworthy text or source exists. Any such path must default to review-required and must not be advertised as high-confidence automatic remediation without independent semantic evidence.

Dependencies: V1 stable.

---

## 7. Dependency and execution order

Recommended issue order:

```text
W0 schema/status model
   |\
   | +--> W1 fixtures/test harness
   |          |\
   |          | +--> W2 inventory/classifier
   |          |          |\
   |          |          | +--> W3 recovery/IR/semantic contract
   |          |          | +--> W4 style resolver
   |          |          |          \
   |          |          +------------> W5 safe applicator
   |          |                         |
   |          +-----------------------> W6 audit/semantic verification
   |                                    |
   +----------------------------------> W7 CI/P0 gate
                                         |
                           +-------------+-------------+
                           |                           |
                          W8                          W9
                           \                           /
                            +-----------> W10 <-------+
                                           |
                                          W11

W12 is post-V1 research and does not block W11.
```

Parallel work is allowed only when interfaces are frozen enough to prevent divergent manifest or status models. If a downstream issue discovers that an upstream contract is unsafe, fix the upstream contract first rather than adding compatibility hacks downstream.

---

## 8. P0 completion gate — “safe production pilot”

P0 is complete only when all of the following are true:

- versioned manifest/schema and compatibility tests pass;
- fixture corpus covers supported positive, semantic-mismatch, partial-batch, and fail-closed cases;
- read-only inventory distinguishes candidate detection from approved math;
- supported damaged/plain/Unicode formula families can be normalized with traceable evidence;
- canonical representation preserves the required semantic distinctions;
- supported generated OMML can be semantically compared back to the approved canonical representation;
- unsupported/semantically unvalidated generator output cannot enter the high-confidence automatic path;
- context style resolution is occurrence-specific and tested;
- safe applicator automatically edits only provably safe occurrences and reports all others;
- audit fingerprints prove pre-existing revisions were not altered;
- session-revision rejection reconstructs source-visible affected text;
- unrelated package/media/relationship drift is detected;
- every authorized occurrence is terminally accounted for and incomplete batches cannot claim complete delivery;
- redlined and clean outputs pass strict package validation;
- at least one end-to-end representative DOCX passes native Microsoft Word open/visual inspection without repair;
- CI passes from a clean checkout;
- no known P0 invariant is represented only as prose without executable validation where executable validation is feasible.

P0 may be called **production-pilot ready**, not fully stable, until P1 end-to-end source alignment/reporting and broader support are complete.

---

## 9. V1 completion gate

V1 may be declared stable only when:

- P0 completion gate passes;
- supported existing/legacy equation and Word story behavior is explicitly documented and tested;
- authoritative-source alignment conflicts are fail-closed;
- native Word validation workflow is reproducible and reports limitations honestly;
- batch/orchestration reports are complete, idempotent, and cannot hide unresolved occurrences;
- README/SKILL/reference claims match the tested support matrix;
- all V1 Issues are closed through reviewed implementation PRs;
- a final repository-wide review finds no unresolved semantic, style, OOXML, revision, compatibility, completeness, or workflow risk.

---

## 10. Explicit non-goals for V1

V1 will not:

- promise pixel-identical rendering with TeX;
- guess mathematically ambiguous notation to maximize conversion rate;
- automatically OCR image equations as trusted mathematics;
- flatten Word paragraphs to make replacement easier;
- rewrite another author’s tracked changes;
- silently convert all prose tokens that resemble Greek names or variables;
- claim support for every MathType/OLE/legacy equation technology without fixtures and preservation tests;
- treat successful OMML generation as proof of semantic correctness when semantic validation is unavailable;
- sacrifice editability by replacing recoverable formulas with images merely to match TeX appearance.

---

## 11. Issue contract

Each GitHub Issue created from this TODO must contain:

- objective and user-facing outcome;
- in-scope and out-of-scope behavior;
- upstream dependencies and downstream contract;
- concrete files/modules expected to change where known;
- implementation requirements;
- tests/fixtures required;
- semantic validation requirements where applicable;
- fail-closed/error behavior;
- compatibility requirements;
- Definition of Done;
- PR review checklist.

Issue scope must be small enough for an actual diff to be reviewed meaningfully. If an issue grows to combine independent safety contracts, split it before implementation.

---

## 12. PR review contract

For every implementation PR:

1. Re-read the governing Issue and relevant sections of this TODO.
2. Review the actual diff, not only the PR description.
3. Run/inspect positive tests and negative/fail-closed tests.
4. Check semantic validation whenever formula generation/recovery changes.
5. Check manifest/schema compatibility where applicable.
6. Check source immutability and package/revision invariants.
7. Check occurrence accounting and delivery completeness behavior.
8. Check that new automation does not silently broaden supported scope.
9. Check docs/CLI messages do not overstate guarantees.
10. Re-review after every substantive fix because a correction can introduce a new workflow risk.
11. Merge only when no unresolved omission, contradiction, unsafe fallback, or new process risk remains.

If a true blocker is discovered, record it explicitly in the Issue/PR instead of weakening an invariant to obtain a passing result.
