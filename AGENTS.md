# AGENTS.md — Codex Execution Governance

This repository is developed through dependency-aware GitHub Issues and reviewed implementation PRs. This file defines **how Codex must operate in the repository**. It does not replace or weaken the product/safety requirements in `TODO.md` or the scope and acceptance criteria in the current GitHub Issue.

This protocol applies only within the execution scope the user has authorized. Automatic continuation means continuing through that authorized milestone; it never means starting unrelated work merely because another Issue exists.

## 1. Authority and source of truth

Before making any change, recover the current state from the repository and GitHub. Do not rely on prior chat history, cached assumptions, or an earlier local plan.

Different sources are authoritative for different questions:

- `AGENTS.md` governs **execution process**: state recovery, Issue selection, branching, review, merge, rollback, blocker handling, and continuation.
- `TODO.md` governs the **V1 product and safety contract**: non-negotiable invariants, workstreams, dependency graph, P0/V1 gates, and release boundaries.
- The current dependency-ready GitHub Issue governs the **active implementation unit**: concrete scope, dependencies, tests, compatibility requirements, fail-closed behavior, and Definition of Done. It must remain consistent with `TODO.md`.
- Current `main` code and tests are the factual implementation baseline. Existing behavior is not automatically normative if it contradicts an explicit Issue/TODO contract.
- `SKILL.md`, `README.md`, and `references/` are user-facing/operating documentation and must match, not override, the tested implementation and stricter safety contracts.

No process rule in `AGENTS.md` may be used to override a non-negotiable safety/product invariant in `TODO.md`. No Issue may silently weaken `TODO.md` merely because its body is more recent.

If sources appear inconsistent, do not silently choose the convenient interpretation. Determine whether the problem is stale documentation, stale GitHub metadata, an implementation defect, or a real upstream contract conflict. Fix stale downstream material when that fix is in scope. If satisfying the current Issue requires weakening a TODO invariant or changing a frozen upstream interface/contract, handle it as an upstream-contract problem under sections 4 and 11 rather than adding a local workaround.

## 2. Repository-state recovery

At the start of every execution cycle and immediately after every merge:

- read the current `main` versions of `AGENTS.md`, `TODO.md`, and the files relevant to the next Issue;
- inspect open/closed GitHub Issues and PRs rather than assuming their state;
- inspect the active Issue body, dependencies, comments, linked PRs, and acceptance criteria;
- verify the working branch is based on the current `main` or update it safely before implementation;
- verify there is no conflicting implementation PR already active for the same Issue;
- treat merged code and current GitHub state as authoritative over remembered status.

Do not continue an old branch blindly after `main` has materially changed. Before final review/merge, compare the PR against the then-current `main`. If `main` advanced in a way that can affect the PR, update the branch safely, rerun affected tests, and review the resulting actual diff again.

## 3. Selecting the next Issue

Work on **one implementation Issue at a time** unless the repository explicitly authorizes parallel work and the relevant interfaces are already frozen.

A dependency-ready Issue is an open implementation Issue for which:

- every stated hard dependency is demonstrably complete in current `main` and its GitHub Issue/PR state accurately reflects that completion, or stale metadata can be corrected without changing the implementation contract;
- no prerequisite P0/V1 gate forbids starting or merging it;
- there is no unresolved upstream contract defect that makes its acceptance criteria unsafe or contradictory;
- there is no active superseding PR/Issue that makes the work obsolete.

Do not treat an Issue being closed by itself as proof that its dependency was successfully implemented; verify the required result exists in `main`. Likewise, do not treat a stale open dependency Issue as an implementation blocker if the required reviewed code is already merged and the metadata can be corrected safely.

Choose the highest-priority dependency-ready Issue according to `TODO.md` and the explicit Issue dependency graph, not simply the lowest numeric Issue number. P0 work precedes dependent P1 work; P2 research must not block V1 unless the governing contracts explicitly change. If several Issues of equal priority are ready and no explicit order is mandated, prefer the order in TODO's issue-slicing/recommended dependency sequence, then the work that unlocks the most downstream dependencies. Do not invent a hidden dependency merely to force numeric ordering.

If no Issue is dependency-ready, determine whether this is a true blocker, an unfinished dependency, or stale GitHub metadata before stopping.

## 4. Issue scope discipline

The active Issue is the unit of implementation and review.

- Implement all requirements needed for its Definition of Done.
- Do not implement downstream Issue features merely because they are convenient.
- Small prerequisite infrastructure needed to make the current Issue testable/reproducible is allowed when it is genuinely necessary and does not freeze a competing downstream design.
- If the actual diff grows to combine independent safety contracts, split the work before merge in accordance with `TODO.md`.
- Do not broaden support matrices, accepted syntax, editable Word stories, or automatic-conversion eligibility without explicit fixtures, tests, and Issue authority.
- Do not weaken fail-closed behavior to increase conversion success or to make tests pass.

When a downstream task reveals a defect in a frozen upstream contract, repair the upstream contract first through an explicit upstream Issue/PR change (or amend the governing Issue if that is the repository's established mechanism) and re-evaluate dependent work. Do not hide contract changes inside downstream implementation or add local compatibility hacks solely to avoid correcting the source contract.

## 5. Branch and PR protocol

Never implement directly on `main`.

For each implementation Issue:

1. Start from current `main` and create a focused branch.
2. Implement only the Issue scope plus necessary supporting changes.
3. Create or update one implementation PR whose body links the Issue and uses the repository's normal closing mechanism (for example `Closes #N`) when appropriate. Summarize actual scope, tests, compatibility impact, refusal behavior, and known limitations.
4. Keep the PR open while implementation or review findings remain unresolved.
5. Do not treat the PR description, commit messages, or green happy-path tests as proof of correctness.

Do not create duplicate implementation PRs for the same Issue unless the existing PR is explicitly superseded. If superseding a PR, record that disposition clearly and avoid leaving ambiguous competing implementations.

Governance/documentation maintenance that is intentionally outside an implementation Issue may use a focused PR without inventing a fake Issue, but it must still follow the actual-diff review and merge gates in this file.

## 6. Required implementation evidence

Before considering an implementation complete, collect evidence appropriate to the Issue. At minimum where applicable:

- run the Issue-specific tests and fixtures;
- run relevant repository regression tests;
- exercise required positive cases;
- exercise required negative, semantic-mismatch, and fail-closed cases;
- verify compatibility requirements stated by the Issue/TODO;
- verify source immutability and package/revision invariants whenever DOCX mutation is involved;
- verify every authorized occurrence/artifact is accounted for whenever the lifecycle/completeness model applies;
- verify dependency/preflight behavior whenever new tooling or runtime requirements are introduced.

If the repository does not yet contain a test harness and the active Issue requires executable proof, create the minimum in-scope test infrastructure necessary rather than replacing tests with prose assertions.

Applicable passing tests are necessary but not sufficient for merge. For a pure governance/documentation PR where no executable test applies, state that explicitly and rely on contract comparison plus repeated actual-diff review rather than fabricating meaningless tests.

## 7. Actual-diff review loop

Every PR must be reviewed against the **actual PR diff** and current files, not only against the Issue/PR description or intended design.

Perform this loop until it converges:

1. Re-read the active Issue when one exists and the relevant `TODO.md` contracts.
2. Review every changed file in the actual PR diff.
3. Check for omitted acceptance criteria, unintended scope expansion, duplicated logic, unsafe fallback, stale assumptions, compatibility regressions, dependency mistakes, and new workflow risks.
4. For formula/recovery/generation changes, check semantic correctness and support-matrix boundaries.
5. For OOXML changes, check source/package/revision/protected-structure preservation and reconstruction requirements.
6. For lifecycle/delivery changes, check terminal accounting, stale-evidence invalidation, per-artifact gates, partial-vs-complete semantics, and atomic finalization requirements.
7. Run or inspect the required positive and negative tests after every substantive fix.
8. Modify the PR for every valid finding.
9. Re-fetch and re-review the new actual diff after the modification; do not rely on the previous review.
10. Repeat until no unresolved omission, contradiction, unsafe fallback, compatibility regression, or new process risk remains.

Conflict resolution, rebases/merges from `main`, generated-fixture changes, and review fixes can all change the effective diff. Treat them as substantive when they can affect behavior or contracts and repeat the relevant tests/review.

Do not approve or merge a PR while any substantive review finding remains open. A Codex self-review does not satisfy an independent-review requirement if repository protection/rules require another reviewer.

## 8. Merge gate

A PR may merge only when all of the following are true:

- the active Issue's Definition of Done and acceptance criteria are satisfied when the PR implements an Issue;
- all prerequisite dependencies and gates that must already be complete are satisfied; when the current PR itself is intended to satisfy a P0/V1 exit gate, every acceptance criterion and required evidence for that gate must be present before merge, but the gate is not treated as a circular prerequisite to itself;
- required tests and negative/fail-closed cases pass where applicable;
- the latest actual diff has been reviewed after the latest substantive change;
- no unresolved semantic, style, OOXML, revision-preservation, package-integrity, compatibility, completeness, dependency, or workflow risk remains;
- documentation changed by the PR does not claim support beyond tested behavior;
- there is no known stale evidence or candidate/artifact hash mismatch relevant to delivery;
- the PR is still based on a compatible current `main` state;
- repository/GitHub merge requirements, required status checks, and required reviews permit the merge.

Do not use an admin/bypass path to defeat branch protection, required checks, or required independent review unless the user explicitly authorizes that exact exception and it does not violate the repository's safety contract.

Merge only the reviewed head commit. Use an expected-head check when the available tooling supports it. If the PR head changes after final review, re-fetch and re-review the resulting actual diff before merge.

After merge, return to current `main`, refresh GitHub state, and — **only if the user's authorized execution scope includes further Issues/milestones** — select the next dependency-ready Issue automatically. Do not wait for routine user confirmation between successfully completed Issues when continuous execution was explicitly requested, but do not extend the user's requested scope on your own.

## 9. Snapshots, failed approaches, and rollback

Keep the branch recoverable throughout implementation.

- Before a risky refactor, destructive migration, or experimental approach, preserve a known-good checkpoint in version control when practical.
- Prefer small, reviewable commits that make it possible to return to the last known-good state.
- If an approach proves incorrect or no longer has implementation value, remove the dead code, temporary compatibility layers, generated junk, abandoned fixtures, and obsolete configuration introduced only for that approach.
- Do not accumulate failed experiments in the final PR merely to preserve history; Git already preserves committed history.
- Do not use destructive force-push/reset operations on shared or reviewed branches unless explicitly authorized and safe. Prefer new corrective commits or a clean replacement branch when necessary.
- Never overwrite the immutable source DOCX or replace a previously validated final artifact with an unvalidated/partial artifact.

Before merge, ensure the PR does not contain accidental local artifacts, temporary debugging output, unrelated generated files, or obsolete code left by failed approaches.

A rollback must restore repository correctness, not merely make tests green.

## 10. What Codex must solve without stopping

The following are **not true blockers** by themselves and should normally be resolved autonomously inside the current Issue/PR:

- failing tests;
- implementation bugs;
- the need to refactor current-Issue code;
- the need to add or repair fixtures/tests required by the Issue;
- ordinary merge conflicts that can be resolved without changing product/safety intent;
- missing small helper modules or project scaffolding needed by the current Issue;
- review findings in the current PR;
- a failed implementation experiment that can be rolled back safely;
- the need to re-run tests, regenerate deterministic fixtures, or re-review the diff;
- a downstream bug caused by the current PR when it can be fixed without violating upstream contracts.

Do not stop merely because implementation requires judgment among multiple technically valid approaches. Choose the approach that best satisfies the governing contracts with the smallest safe scope and strongest executable evidence.

## 11. True blockers

Stop and report a blocker only after reasonable repository-local remediation has been exhausted and continuing would require guessing, violating a contract, or fabricating evidence.

True blockers include:

- missing user-owned/private input that is required for the active Issue and cannot be reconstructed from repository-controlled fixtures or authoritative sources;
- missing repository/GitHub permission required to create/update/merge the necessary branch or PR;
- a required external capability that is genuinely unavailable and cannot be substituted without weakening the acceptance contract;
- mathematically ambiguous source content whose intended semantics require author/user approval under the evidence policy;
- an irreconcilable contradiction between the active Issue and a `TODO.md` non-negotiable invariant or frozen upstream interface;
- a required native Microsoft Word validation gate that cannot be executed and for which no valid candidate-bound evidence can be obtained;
- a safety/integrity condition where continuing would require modifying another author's revisions, silently changing protected OOXML/package content, guessing unsupported semantics, or misreporting partial output as complete.

When blocked:

1. finish every safe, independent part of the active Issue that does not prejudice the unresolved decision;
2. keep the PR unmerged and clearly non-complete;
3. record the exact blocker, affected requirement, evidence already collected, and what external decision/capability is needed;
4. do not weaken tests, invariants, or completion policy merely to proceed.

## 12. Microsoft Word native-validation boundary

Native Microsoft Word validation is a real acceptance gate where `TODO.md`/the active Issue requires it.

- Portable CI/package validation does not substitute for a required Word open/no-repair check.
- Structural audit success does not substitute for native Word validation.
- Validation evidence must be bound to the exact candidate artifact/content hash according to the repository's lifecycle/evidence model once implemented.
- If Word validation is unavailable, Codex may complete portable implementation and produce clearly labeled staging/review evidence, but it must not mark the relevant job, P0 gate, V1 gate, or final deliverable `COMPLETE`.
- Do not fabricate screenshots, visual-inspection results, repair-prompt status, Word version information, or candidate-bound evidence.

If the active Issue's Definition of Done requires native Word validation and that gate cannot be executed, this is a true blocker to merging/closing that Issue unless the governing contract explicitly permits a manual evidence handoff. Any manual handoff evidence must still satisfy the same candidate identity/hash binding and validation requirements; "someone opened it" is not sufficient evidence by itself.

## 13. Safety invariants that automation may never bypass

Codex must preserve the non-negotiable invariants in `TODO.md`, including in particular:

- source DOCX immutability;
- no blind global replacement over flattened/serialized Word content;
- no silent relocation of frozen occurrences after source/anchor drift;
- no rewriting another author's tracked changes for convenience;
- detection is not proof of mathematical intent;
- ambiguous/corrupted formulas are not guessed simply to keep the pipeline moving;
- existing native OMML is not reconverted by default;
- Pandoc output alone is not proof of semantic correctness where semantic validation is required;
- occurrence-specific style resolution and Word-compatible math-font behavior;
- protected package/relationship/media/content preservation;
- every authorized occurrence and requested artifact receives terminal accounting;
- partial or unresolved batches are never reported or delivered as complete;
- final artifacts are promoted only after the required candidate-bound gates pass.

Any code path, fallback, test shortcut, or manual procedure that bypasses these invariants is a defect, not a productivity optimization.

## 14. Completion and automatic continuation

For each implementation Issue, completion means the required implementation is merged to `main`, the Issue is closed or otherwise accurately reflects completion, and there is no unresolved superseded/competing PR state left behind.

After a successful merge, when the user's authorized scope includes continued execution:

1. refresh current `main` and GitHub Issue/PR state;
2. verify the just-merged change did not create a newly visible blocker for downstream contracts;
3. identify the next dependency-ready Issue;
4. continue automatically using this same protocol.

Stop only when:

- the user-authorized milestone (for example P0 or V1) is actually complete;
- no dependency-ready Issue remains because the remaining work is intentionally outside that authorized scope; or
- a true blocker from section 11 is reached.

When reporting completion, distinguish clearly between repository implementation completion, P0/V1 gate completion, and any document-level `COMPLETE` status. Never infer one from another.
