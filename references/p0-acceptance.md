# P0 Acceptance Boundary

W7 has two separate checks: portable processing capability and native Word
delivery validation. A passing portable preflight does not make a DOCX a final
deliverable.

## Portable preflight

Run this before inventory, generation, application, or staging:

```bash
python3 scripts/preflight.py --json preflight.json
```

The portable profile supports Python 3.10 through 3.12 and Pandoc 3.x with a
working Markdown-to-JSON capability probe. A missing or incompatible Pandoc
installation exits non-zero with an actionable check result. The report may
show the companion `docx` skill and Microsoft Word as deferred or unavailable;
those are explicit boundaries, not successful delivery gates.

The supported Python range is a product compatibility contract, not a mandate
to run a version matrix on every pull request. The normal GitHub CI samples the
baseline environment documented below; broader compatibility may be checked
locally or as a targeted release/maintenance verification when it is actually
needed.

### Pin the reviewed companion guidance

File existence alone is not proof that the companion `docx` guidance is the
reviewed capability. First inspect the supplied companion and record the
fingerprint reported from the exact `SKILL.md` and `ooxml.md` bytes:

```bash
python3 scripts/preflight.py \
  --companion-root /path/to/reviewed/docx-skill \
  --json preflight-companion-discovery.json
```

A present but unpinned companion reports `UNPINNED` and keeps
`mutation_ready=false`. After reviewing those exact files, rerun preflight with
the recorded fingerprint:

```bash
python3 scripts/preflight.py \
  --companion-root /path/to/reviewed/docx-skill \
  --companion-fingerprint <reviewed-sha256> \
  --require-companion \
  --json preflight-mutation.json
```

Changing either companion file changes the combined fingerprint and fails the
required preflight. No undeclared private path is searched automatically.

## Candidate gate order

The candidate lifecycle is:

```text
freeze manifest/job/plan
  -> stage redlined and clean candidates
  -> bind W6 structural audit evidence per candidate
  -> freeze representative acceptance request and candidate hashes
  -> inspect the exact pair in Microsoft Word
  -> bind native Word evidence per candidate
  -> validate every candidate and mark VALIDATED
  -> derive COMPLETE from the shared lifecycle contract
  -> issue representative W7 acceptance receipt
  -> atomically promote the requested set
```

Use `word_formula_omml.gates.bind_structural_audit_evidence` for ordinary W6
evidence and `finalize_p0_artifact_set` for the P0 promotion boundary. The
low-level `finalize_artifact_set` primitive remains available to its existing
callers; P0 delivery must pass through the policy wrapper.

## Native Word evidence

Native evidence is candidate-bound through the exact artifact SHA-256 already
stored in the frozen job. A PASS observation requires all of the following:

- Microsoft Word opened the exact candidate without a repair prompt;
- risk-based visual inspection passed;
- the Word version, validation mode, environment, and observation are recorded;
- the artifact hash still matches the frozen candidate.

The repository must not fabricate this evidence. On a Linux runner without
Word, record `NATIVE_WORD: NOT_RUN` and keep the job out of `COMPLETE`; a
Windows or controlled manual run must record the real candidate hash and
environment before binding PASS evidence.

The generic `bind_native_word_evidence` API remains intentionally reusable. A
**repository-level W7 exit**, however, additionally requires the representative
acceptance request/receipt below. Synthetic `16.0-test` observations are policy
tests only and can never be cited as production acceptance evidence.

## Representative W7 acceptance request

Issue #10 is not closed by opening one trivial `x_i` candidate in Word. The
representative job must request both `redlined` and `clean`, already have
candidate-bound W6 PASS evidence, and map the current Skill visual-risk families
to successful occurrences:

- `inline`
- `display`
- `subscript`
- `superscript`
- `fraction`
- `inequality`
- `greek`
- `interval`
- `scientific_notation`

The repository-controlled example coverage file is
`tests/fixtures/p0_acceptance_coverage.json`. It uses the W1 corpus to show the
intended representative set. In particular, `display` may be covered by a
**preserved existing native OMML display equation**; do not expand the W5 P0
applicator beyond its tested inline fast path merely to satisfy acceptance.

After staging and binding the structural audit reports, freeze the exact Word
handoff:

```bash
python3 scripts/prepare_p0_acceptance.py \
  --job staging/job.structural.json \
  --candidate redlined=staging/redlined.docx \
  --candidate clean=staging/clean.docx \
  --coverage tests/fixtures/p0_acceptance_coverage.json \
  --output staging/p0-acceptance-request.json \
  --observations-output staging/p0-word-observations.json
```

The request contains the source/manifest/application-plan identities, both
candidate SHA-256 values, the exact occurrence coverage, and a deterministic
`acceptance_request_id`. The generated observation file contains only
`NOT_RUN`/blank placeholders; it is not evidence.

## Controlled Microsoft Word handoff

Move the **exact candidate bytes plus the immutable acceptance request** to a
controlled Windows environment. For each candidate:

1. verify its SHA-256 against the request before opening it;
2. open it in Microsoft Word and fail if Word offers repair/recovery;
3. inspect every mapped risk family in both redlined and clean outputs;
4. record the actual Word build/version, OS/environment, validation mode, and
   timezone-qualified observation time;
5. set each visual family to `PASS` only after inspecting the mapped occurrence;
6. leave any failed or unrun family as non-PASS and do not issue a successful
   W7 receipt.

A valid observation object is candidate-specific and includes the full visual
matrix, for example:

```json
{
  "candidate_sha256": "<exact hash from request>",
  "open_no_repair": true,
  "word_version": "16.0.17328.20162",
  "validation_mode": "manual",
  "environment": {
    "os": "Windows 11",
    "word_build": "16.0.17328.20162"
  },
  "recorded_at": "2026-08-26T14:30:00-07:00",
  "visual_checks": {
    "inline": "PASS",
    "display": "PASS",
    "subscript": "PASS",
    "superscript": "PASS",
    "fraction": "PASS",
    "inequality": "PASS",
    "greek": "PASS",
    "interval": "PASS",
    "scientific_notation": "PASS"
  },
  "notes": "manual representative inspection"
}
```

Record the completed observations through the repository policy rather than
calling the low-level binder by hand:

```bash
python3 scripts/record_p0_acceptance.py \
  --job staging/job.structural.json \
  --candidate redlined=staging/redlined.docx \
  --candidate clean=staging/clean.docx \
  --request staging/p0-acceptance-request.json \
  --observations staging/p0-word-observations.json \
  --output-job staging/job.validated.json \
  --receipt staging/p0-acceptance-receipt.json
```

The recorder rejects stale/tampered requests, changed candidate bytes, missing
W6 evidence, missing/failed visual families, missing Word/environment data, and
ambiguous timestamps. On success it binds `NATIVE_WORD` evidence for both exact
candidates, validates the staged set, requires `P0_PRODUCTION_PILOT=PASS` with
`delivery_status=COMPLETE`, and emits a deterministic candidate-bound receipt.

For Issue #10 closure, retain the receipt in the PR/repository evidence and
record the exact reviewed PR head. Do not commit private candidate DOCX files.
If the PR head changes in a way that changes candidate bytes or acceptance
logic, regenerate/revalidate the affected evidence before merge.

## CI boundary

GitHub Actions is deliberately a minimal portable smoke/regression check, not
the acceptance or delivery engine. The normal PR workflow has one Ubuntu job,
uses Python 3.11 and pinned Pandoc 3.1.11, and runs portable preflight, repository
regressions, and source compilation. It does not run a Python matrix, duplicate
the same work on branch pushes, publish artifacts, finalize candidates, deploy
releases, or simulate Microsoft Word.

Native Microsoft Word open/no-repair and visual inspection remain a controlled
Windows/manual acceptance gate. Portable ZIP, XML, LibreOffice, Pandoc, or
synthetic Word observations cannot substitute for that native gate.
