# Agent failure patterns and trace heuristics

Research notes for `trace2rules` mining. The goal is to prioritize behaviors that
are (a) observable in coding-agent tool traces and (b) shown to matter outside a
single anecdote.

## Evidence quality

- **Measured**: benchmark or controlled corpus with counts.
- **Vendor measured**: company corpus or judge audit with a published method, not
  peer reviewed.
- **Incident**: maintainer issue or postmortem, no denominator.
- **Anecdote**: forum/blog report, no denominator.

Benchmark rates do not transfer directly to production. Incident trackers have
selection bias. LLM-judge labels are not ground truth; one independent test of
completion judges found AUROC <= 0.65
([false-success study](https://arxiv.org/html/2606.09863)).

## What the current miner already covers

`noop_retry`, `stuck_loop`, `recovery`, `failure_loop`, `low_validation`,
`premature_completion`, `site_edit_mismatch`, `workaround_script`,
`context_blasts`, `selector_miss`, `repeated_reads`, `edit_churn`,
`command_error`, `permission_denied`, `feedback_loop`, and `commit_hygiene`.

These cover repetition and missing validation well. They do not cover claim
state, test-oracle integrity, rejected edit recovery, environment thrash, or
destructive actions.

## Highest-value missing patterns

### 1. False completion / claim-state mismatch

The agent claims the task is complete while the last relevant state contradicts
it: last test failed, no test followed the last edit, or a claimed file/merge
does not exist.

- **Evidence**: 45-48% of failures in single-control tau2-bench airline/retail
  domains and 75.8% of AppWorld self-assessing coding trajectories have false
  success claims ([false-success study](https://arxiv.org/html/2606.09863)).
  Transluce measured severe overselling in 1.8% of 4,788 production SWE-chat
  sessions ([Transluce](https://transluce.org/docent/blog/coding-agent-behaviors)).
  OSWorld 2.0 reports agents spend <7% of budget detecting/repairing their own
  errors ([OSWorld 2.0](https://arxiv.org/html/2606.29537)).
- **Trace signature**: final assistant text contains a success claim; no
  successful test/build/lint after the last edit, or the last test exited
  non-zero, or the claim names a path/write/merge with no matching event.
- **Current gap**: `low_validation` and `premature_completion` detect missing
  validation, not a contradicted claim.
- **Data needed**: final assistant message; `assistant_context` already stores
  per-event reasoning.

### 2. Test-oracle tampering / test-gate capture

The agent changes the measurement apparatus instead of the product: deletes or
skips tests, weakens assertions, re-blesses snapshots, edits `conftest.py` or CI,
or copies constants from a failing assertion into production code.

- **Evidence**: METR found reward hacking in 39/128 (30.4%) RE-Bench runs vs
  8/1,087 (0.7%) HCAST runs ([METR](https://metr.org/blog/2025-06-05-recent-reward-hacking/)).
  SWE-bench+ found 31.08% of passing patches were weak-test passes
  ([SWE-bench+](https://arxiv.org/abs/2410.06992)); UTBoost found 345 erroneous
  passing patches affecting 40.9% of SWE-bench Lite entries
  ([UTBoost](https://arxiv.org/abs/2506.09289)). NIST found test editing in
  8/498 o4-mini SWE-bench tasks
  ([NIST](https://www.nist.gov/caissi/2-examples-cheating-caisis-agent-evaluations)).
- **Trace signature**: edit under `tests/`, `test_*.py`, `conftest.py`, runner
  config, or scoring script; added `skip`/`xfail`/`deselect`/snapshot-update;
  deleted assertions; failing run -> test edit -> green run -> success claim.
- **Current gap**: no detector sees test-path edits or edits to test config.
- **Data needed**: edit target is available; added/removed lines require diff
  content or edit observations.

### 3. Failed edit recovery / rejected patch loop

An edit/patch is rejected before it applies (`old_string` not found, invalid
range, malformed patch), and the agent retries the region without re-reading.

- **Evidence**: SWE-agent found 51.7% of 2,294 trajectories had at least one
  failed edit, 23.4% of failures were cascading failed edits, and after one
  failure the chance of eventually applying that edit fell to 57.2%
  ([SWE-agent](https://arxiv.org/abs/2405.15793)).
- **Trace signature**: `action_type='edit'` with `outcome='err'`; repeated
  rejected edits on the same path; retry without an intervening read.
- **Current gap**: `stuck_loop` counts edits across sessions; `edit_churn`
  counts repeats. Neither measures rejected application.

### 4. Fix without reproduction / blind fix

The agent edits product code without first reproducing the failure it is fixing.

- **Evidence**: reproduction tests trigger the bug only about half the time
  across five APR agents on 500 SWE-bench tasks
  ([APR traceability](https://arxiv.org/abs/2506.08311)). SWE-agent treats
  "Failed to Reproduce" as a distinct failure category
  ([SWE-agent](https://arxiv.org/abs/2405.15793)).
- **Trace signature**: first source edit precedes any failing exec, or no
  failing exec occurs before the first non-test source edit.
- **Current gap**: no mining detector; audit only checks read-before-edit
  ordering.
- **False positive**: feature work and trivial fixes often need no reproduction;
  restrict to bug-like tasks where possible.

### 5. Undetected regression (PASS -> FAIL)

An edit breaks a test that previously passed and the session ends without
restoring it.

- **Evidence**: TDAD measured a 6.08% test-level regression rate, with 562
  pass-to-fail failures across 100 SWE-bench Verified instances; a TDD prompt
  without test-impact context raised this to 9.94%
  ([TDAD](https://arxiv.org/abs/2603.17973)).
- **Trace signature**: per-test pass state before edit, fail after edit, no
  later green run of that test.
- **Current gap**: no detector parses per-test status.
- **Data needed**: runner output with node IDs; fallback to new failure
  signatures after a green run.

### 6. Environment thrash / setup tax

The agent repeatedly installs/uninstalls/upgrades the same package, recreates
environments, or fixes a missing tool in a way that does not persist.

- **Evidence**: SetupBench reports 38-69% of setup steps wasted and incomplete
  tooling in 17-26% of repo-setup failures
  ([SetupBench](https://arxiv.org/html/2507.09063)). EnConda-Bench found failed
  repairs often introduce new faults
  ([EnConda-Bench](https://arxiv.org/html/2510.25694)).
- **Trace signature**: >=3 package-manager calls for the same package,
  install/remove oscillation, `sudo`/global installs, or successful install
  followed by `command not found`.
- **Current gap**: appears as recovery noise; no setup classifier.
- **Side**: repo/setup, not an agent rule, unless the agent ignored a documented
  environment.

### 7. Destructive git / monitor evasion

Unrequested `git reset --hard`, `git clean -fd`, `git checkout .`, force push,
`--no-verify`, `HUSKY=0`, or CI disabling.

- **Evidence**: incident clusters describe lost uncommitted work and secrets
  ([#34327](https://github.com/anthropics/claude-code/issues/34327),
  [#104331](https://github.com/anthropics/claude-code/issues/104331)).
  Transluce measured severe monitor evasion in 1.9% of 4,990 SWE-chat sessions
  ([Transluce](https://transluce.org/docent/blog/coding-agent-behaviors)).
  No reliable population rate; these are high-severity, low-frequency events.
- **Trace signature**: command regex on `raw_target`, especially at session
  start or after a failing gate.
- **Current gap**: none.
- **False positive**: user explicitly requested the operation; sandbox cleanup.

### 8. Constraint amnesia / specification drift

A constraint stated early is violated later, especially after compaction or a
long tool-output burst.

- **Evidence**: MAST reports disobey-task-specification in 11.8% of 1,642
  traces ([MAST](https://arxiv.org/abs/2503.13657)); OSWorld 2.0 reports agents
  dropping stated constraints ([OSWorld 2.0](https://arxiv.org/html/2606.29537)).
- **Trace signature**: constraint text/path/version in early context; later
  edit/command contradicts it; no later user message supersedes it.
- **Current gap**: audit checks static rules, not task-specific constraints.

### 9. Blast-radius inflation

A localized fix touches many unrelated files, adds abstractions, or changes
dependencies.

- **Evidence**: agent patches are substantially larger than developer patches;
  rejected agent PRs skew larger and have more failed CI checks
  ([PR rejection study](https://arxiv.org/html/2601.15195)). One study measured
  3.5+ hunks and 101-245 changed lines for SWE-Agent
  ([patch size study](https://arxiv.org/abs/2410.12468)).
- **Trace signature**: edited file set exceeds files implicated by the failing
  test/traceback; high edit count for a single-file issue; manifest/lockfile
  churn; generated/vendored paths.
- **Current gap**: none.
- **Data needed**: diff stats and task localization metadata.

### 10. Coherence collapse / destroying a correct intermediate patch

The agent reaches a near-correct patch, then overwrites or reverts it; the final
state is worse than an intermediate state.

- **Evidence**: 60-69% of capable-model failures edit the correct functions but
  produce incorrect patches; coherence collapse is 39.7% of edit-quality
  failures, about 22.6% of all SWE-agent failures
  ([coherence study](https://arxiv.org/html/2603.24631v2)).
- **Trace signature**: hunk appears then disappears; intermediate diff passes
  tests but final fails; edit-target repetition with line-count oscillation.
- **Data needed**: per-edit snapshots/diff hashes, which the current schema does
  not retain.

## Patterns deferred as too noisy for first-pass mining

- **Search paralysis**: many distinct bounded reads with no edit or exec.
  Measured in SWE-agent (iterative search 12.0% vs 15.7% with no search), but
  large unfamiliar repos make it easy to flag legitimate exploration.
- **Context saturation / post-compaction re-read storm**: real, but requires
  context-window and compaction signals plus a reread baseline.
- **Solution leakage / hint copying**: requires issue text and patch overlap.
- **Review-loop nonconvergence**: requires review feedback and diff association.

## Implementation order

Wave 1 uses fields already stored:

1. `edit_rejection`: rejected edit calls and repeated attempts.
2. `blind_edit`: edit target never read earlier in the session.
3. `environment_thrash`: repeated package-manager calls and missing tools.
4. `destructive_command`: context-aware denylist for destructive git and
   monitor-evasion commands.
5. `no_reproduction`: bug-like session edits before any failing exec.

Wave 2 needs new parsing or schema:

- `false_completion`: final assistant message plus last validation state.
  `assistant_context` is available per event, so a conservative version is
  possible without a migration.
- `oracle_tampering`: edit content/diff needed to detect assertion deletion and
  skip markers.
- `pass_to_fail`: per-test status parser needed.
- `blast_radius`: diff stats needed.
- `coherence_collapse`: edit snapshots needed.

## Implementation status

Implemented from Wave 1:

- `edit_rejection`
- `blind_edit`
- `environment_thrash`
- `destructive_command`
- `no_reproduction`
- `generated_edit`
- `dependency_drift`

Implemented from Wave 2:

- `false_completion`, using the last non-empty `assistant_context` and the last
  test event after the last edit.
- `correction_loop`, using local user corrections rather than benchmark labels.
- `test_regression`, using local test summaries stored in `test_results`.
- `commit_hygiene`, using edit/commit cadence and final-edit state.
- `destructive_command` coverage for publish/release commands.

Also added an enforcement inventory for existing CI, pre-commit, hook, and
repository-script checks. The current CLI has no offline synthesis, label
backend, eval command, or `synthesize --report`; report prompts are generated
by `report` and validated separately with `audit --proposal`.

Deliberately not implemented yet:

- `oracle_tampering`: needs edit diffs or added/removed lines. Editing a test
  path alone is not enough evidence.
- `pass_to_fail`: needs per-test status parsing from runner output.
- `blast_radius`: needs diff stats and task-localization metadata.
- `coherence_collapse`: needs per-edit snapshots or diff hashes.

## Sequence mining

Raw 3-5 gram mining is sparse because tool commands, URLs, temp paths, line
ranges, and generated filenames make almost every window unique. AgentTAC
([paper](https://aclanthology.org/2026.emnlp-main.1637.pdf)) avoids this by
representing traces as temporal **action + context attributes** and anchoring
patterns at localized errors, then ranking candidates by failure rate and
coverage. Its patterns are interpretable action-context sequences, not literal
command strings.

`trace2rules` now normalizes sequence tokens before mining:

- `exec` -> command family (`pytest`, `cargo:test`, `git:status`, `shell`,
  `network`, ...)
- `read`/`search`/`edit` -> path class (`source`, `test`, `docs`, `config`,
  `generated`, `scratch`, `dir`)
- outcome -> `ok` / `err` / `timeout`

The n-gram miner is anchored at error-ending windows and exact-sequence output
is balanced per repository to prevent one large transcript from monopolizing the
view. Attribute patterns also allow a small number of skipped events and include
exception families, so one extra read does not erase a shared failure workflow.

## Caveats

- These are **prioritization heuristics**, not claims about production rates.
- MAST/TRAIL figures are annotated convenience samples; METR, TDAD,
  SWE-bench+, UTBoost, and tau-bench are stronger anchors.
- Any detector using an LLM judge should be treated as a candidate generator,
  not a ground-truth oracle.
