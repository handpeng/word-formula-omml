# P0 Acceptance Boundary

W7 has two separate checks: portable processing capability and native Word
delivery validation. A passing portable preflight does not make a DOCX a final
deliverable.

## Portable preflight

Run this before inventory, generation, application, or staging:

```bash
python3 scripts/preflight.py --json preflight.json
```

The portable profile requires Python 3.10 through 3.12 and Pandoc 3.x with a
working Markdown-to-JSON capability probe. A missing or incompatible Pandoc
installation exits non-zero with an actionable check result. The report may
show the companion `docx` skill and Microsoft Word as deferred or unavailable;
those are explicit boundaries, not successful delivery gates.

When the reviewed companion skill is available, require it before mutation:

```bash
python3 scripts/preflight.py \
  --companion-root /path/to/reviewed/docx-skill \
  --require-companion \
  --json preflight-mutation.json
```

The companion directory must contain the reviewed `SKILL.md` and `ooxml.md`.
No undeclared private path is searched automatically.

## Candidate gate order

The candidate lifecycle is:

```text
freeze manifest/job/plan
  -> stage redlined and clean candidates
  -> bind W6 structural audit evidence per candidate
  -> bind native Word evidence per candidate
  -> validate every candidate and mark VALIDATED
  -> derive COMPLETE from the shared lifecycle contract
  -> atomically promote the requested set
```

Use `word_formula_omml.gates.bind_structural_audit_evidence` for a passing W6
report and `bind_native_word_evidence` for native validation. Use
`finalize_p0_artifact_set` for the P0 promotion boundary. The low-level
`finalize_artifact_set` primitive remains available to its existing callers;
P0 delivery must pass through the policy wrapper.

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

Example observation data passed to `bind_native_word_evidence`:

```json
{
  "state": "PASS",
  "open_no_repair": true,
  "visual_inspection": "PASS",
  "word_version": "16.0.17328.20162",
  "validation_mode": "manual",
  "environment": {
    "os": "Windows 11",
    "word_build": "16.0.17328.20162"
  }
}
```

`FAIL` or `NOT_RUN` is not downgraded to a best-effort complete result.
Candidate mutation stales prior evidence and requires a new audit and native
validation run.

## CI boundary

The GitHub Actions job runs the clean-checkout portable matrix and all
repository fixtures. Native Microsoft Word open/no-repair and visual
inspection remain a controlled Windows/manual acceptance gate. Portable ZIP,
XML, LibreOffice, or Pandoc checks cannot substitute for that native gate.
