# trace2rules

Mine and audit coding-agent sessions into evidence-backed `AGENTS.md` rules.

`trace2rules` is a local-first CLI. It ingests agent trajectories, mines recurring friction, audits existing `AGENTS.md`/`CLAUDE.md` rules against real sessions, and identifies whether each rule is observed, compliant, violated, unobserved, or already enforced by CI/pre-commit.

The output is a proposal, not an automatically applied `AGENTS.md`.

> **Small local corpus?** Results are most useful with about 10+ local sessions. With fewer, ingest a public dataset first; see [Public datasets](#public-datasets).

## Quick start

Paste this into your coding agent to audit the current checkout:

```text
Install trace2rules if missing (`uv tool install trace2rules`). Run `trace2rules audit --prompt > audit.txt`, read audit.txt, and list unobserved, violated, and unenforced rules. Propose concrete enforcement without duplicating existing CI/pre-commit checks.
```

Or run the deterministic audit manually:

```bash
trace2rules audit
```

Paste this into your coding agent to generate rules:

```text
Install trace2rules if missing (`uv tool install trace2rules`). Run `trace2rules mine --prompt --out-dir prompts/` for my local agent sessions. Read the prompt files and synthesize repo-specific AGENTS.md rules yourself. Keep only rules supported by the evidence.
```

Or generate the evidence prompts manually:

```bash
trace2rules mine --prompt --out-dir prompts/
```

See [Auditing AGENTS.md](#auditing-agentsmd) and [Generating rules](#generating-rules) for the full flows.

## Auditing AGENTS.md

`trace2rules audit` is deterministic: it prints counts, coverage, verdicts, enforcement status, and deterministic suggestions.

For a fresh-agent review, emit the evidence prompt:

```bash
trace2rules audit --prompt > audit.txt
```

Give `audit.txt` to your agent. The LLM receives the audit table but not the deterministic suggestions.

After the agent writes proposed rules, validate them:

```bash
trace2rules audit --proposal AGENTS.proposed.md
```

`--proposal` parses the proposed Markdown, audits it against sessions, and prints verdicts before you apply it.

On the Open-SWE `swc-project/swc` split (188 sessions), the current `AGENTS.md` audit finds 65 rules: 10 measured, 1 partial, and 54 unmeasured.

| Rule | Coverage | Followed | Violations | Confidence | Enforcement |
|---|---|---:|---|---|---|
| Commit frequently | measured | 136 | 52/188 (28%) | high | missing |
| MSRV compatibility | measured | 0 | 16/16 (100%) | low | missing |
| No nightly-only rustc features | measured | 0 | 16/16 (100%) | low | missing |
| Use `gh` for GitHub data | measured | 18 | 6/24 (25%) | medium | `.github/workflows/CI.yml` |
| Snapshot update with `UPDATE=1 cargo test` | measured | 23 | 2/25 (8%) | medium | missing |
| `--no-verify` guard | partial | 13 | 1/14 (7%) | low | missing |
| Read the codebase before editing | measured | 123 | 62/185 (34%) | high | guidance only |
| Verify assumptions against source/tests | measured | 123 | 62/185 (34%) | high | guidance only |
| Submodule update before tests | measured | 0 | 32/32 (100%) | medium | guidance only |
| `git commit` only after `git add` | measured | 8 | 6/14 (43%) | low | guidance only |
| `cargo test -p <crate>` | measured | 14 | 0/14 (0%) | low | missing |

`configured` means the command appears in `.github/workflows/CI.yml`; it does not prove the check is required by branch protection. `available` means a Makefile/package target exists but is not wired into CI. `missing` means no mechanism was found.

The two read rules are separate clauses but share the `read_before_edit` detector. Their identical followed/violation counts are expected, not evidence that they are the same rule.

#### LLM review of the swc audit

An `audit --prompt` pass over the same table suggests:

- Delete the four stale binding `test.sh` commands; they are unmeasured and reference paths that no longer exist.
- Enforce commit cadence with a pre-push clean-worktree check and reject `WIP`, `fixup!`, and `squash!` subjects.
- Merge the MSRV and nightly rules: build on the workspace `rust-version` and stable Rust, pin the toolchain, and reject `#![feature(...)]`.
- Reject `curl`/`wget` to GitHub in CI so `gh` remains the access path.
- Run snapshot tests with `UPDATE` unset and fail on uncommitted fixture changes.
- Run the same checks in CI as in hooks so `--no-verify` cannot bypass them.
- Consolidate the two read rules into a PR verification requirement: read the implementation, fixtures, and tests, then list verified paths.
- Move submodule initialization into CI and test wrappers; add explicit staging and `git commit -a` checks.

The review also proposed seven new rules: clean handoff, commit-subject hygiene, no `git commit -a`, no GitHub scraping, no `UPDATE` in CI, submodule wrappers, and an MSRV workflow.

`audit` prints the audited/total session count and defaults to the latest `AGENTS.md` commit cutoff. Pass `--all-sessions` for full history.

## Generating rules

Mining produces evidence, not rules. `trace2rules mine --prompt --out-dir prompts/` writes one prompt per detected repository; give those prompts to your agent. The agent proposes rules, then validate them with `audit --proposal`. Use `--root` when the checkout is not the directory containing `AGENTS.md`, so path and enforcement checks inspect the real repository.

**`swc-project/swc`**

- [guidance] Before changing implementation code, run an existing fixture/test or add a focused failing test and execute it on the unmodified tree; do not use a standalone `/tmp` mock as the only reproduction for changes under `crates/`.
- [guidance] Create one-off reproductions under `/tmp` or as tests/fixtures in the affected crate; do not add a temporary workspace crate or edit workspace `Cargo.toml`/`Cargo.lock` for a reproduction.
- [guidance] When the default toolchain rejects repository rustflags, run cargo with `RUSTUP_TOOLCHAIN=<stable>` and set `RUSTFLAGS` at invocation; do not edit `.cargo/config.toml` to work around the toolchain.
- [guidance] For large source files, read the relevant ranges with `sed -n '<start>,<end>p' <file>` or locate symbols with `rg -n`; do not page whole files until output is clipped.

**`rkkautsar/trace2rules`**

- [guidance] Keep `src/trace2rules/cli.py` limited to Typer wiring and option parsing; put non-CLI logic in a dedicated module under `src/trace2rules/`.
- [guidance] When adding, renaming, or removing a CLI command or option in `src/trace2rules/cli.py`, update the matching row in the `README.md` command table in the same change.

The latest synthesis produced these four rules after the stale-path filter was corrected. Rules are marked `guidance` when no concrete CI, pre-commit, or lint check exists; enforceable rules require a specific check. Validate proposals with `audit --proposal`.

## Scope

`trace2rules` proposes **durable, normative `AGENTS.md` rules**, not memories.

It targets two destinations:

- **Enforceable rules**: a CI check, pre-commit hook, lint rule, or test can observe and enforce them.
- **Stable AGENTS rules**: durable repo or global policy that future agents should follow even though it is not mechanically checkable.

It deliberately does not mine memories, historical decisions, ownership, dates, task state, or "what happened" summaries. Those are contextual and belong in a memory system, not in `AGENTS.md`. If evidence is descriptive rather than prescriptive, the tool should omit it.

## How it works

1. **Ingest** normalizes tool calls into DuckDB (`sessions`, `tool_events`, `feedback_events`, `test_results`).
2. **Mining** finds hotspots, recovery loops, validation gaps, edit rejections, blind edits, environment thrash, destructive commands, generated-path edits, dependency drift, and correction loops.
3. **Sequence mining** normalizes commands and paths, then mines repo-local sequential rules (`antecedent -> specific error`) with support, confidence, lift, and a representative evidence window. Exact sequences use lift and paired-rework evidence; windows never cross a compaction boundary.
4. **Prompt** selects bounded findings and writes one readable Markdown evidence prompt per repo when `mine --prompt` is used.
5. **Audit** checks existing `AGENTS.md` rules against sessions. `holdout` splits sessions by time and audits the same rules on an earlier train slice and later holdout.

Generated verifier scripts and low-support task-specific sequences are filtered before prompting.

## Commands

| Command | Purpose |
|---|---|
| `ingest` | Load sessions and tool events into DuckDB |
| `mine` | Mine friction findings, JSON, or evidence prompts; auto-ingests local sessions |
| `doctor` | Check install, Node, source stores, and database schema |
| `patterns` | Mine repo-local sequential rules (antecedent → specific error) |
| `exact` | Show recurring normalized sequences with evidence |
| `audit` | Audit existing `AGENTS.md` rules against sessions and suggest enforcement |
| `holdout` | Audit rules on an earlier train split and a later time holdout |

Useful options:

```bash
trace2rules ingest                         # ~, all detected local stores
trace2rules ingest --format swe-rebench    # opt into the public dataset
trace2rules ingest --format open-swe --repos owner/name
trace2rules ingest --source PATH --limit 200
trace2rules exact --length 4 --per-repo 2
trace2rules patterns --top 10
trace2rules audit --agents AGENTS.md
trace2rules audit --agents AGENTS.md --commit HEAD --json
trace2rules audit --agents AGENTS.md --root . --prompt
trace2rules holdout --agents AGENTS.md --fraction 0.3
trace2rules mine --prompt --min-support 5
trace2rules mine --prompt --max-rules 5
trace2rules mine --prompt --out-dir prompts/ --max-rules 10
trace2rules mine --json
```

## Local sessions

`--source` is repeatable and defaults to your home directory. It scans:

- `~/.claude/projects`
- `~/.codex/sessions`
- `~/.pi/agent/sessions`
- `~/.omp/agent/sessions`
- `~/.local/share/opencode`

The default cap is 200 sessions per source. `--limit N` selects the newest N files per invocation; re-runs can select a different set when files are added or touched. Use `--limit 0` for the full deterministic set. OpenCode is read directly from its SQLite database; JSON exports are still accepted:

```bash
uv run trace2rules ingest --format agent-trajectory --source ~/.local/share/opencode
```

The repository label is inferred from the git remote (`owner/name`) when available, falling back to the working-directory basename. `mine --prompt` emits one prompt per detected repo:

```bash
trace2rules mine --prompt --out-dir prompts/
```

Local ingest treats each compaction segment as its own session by default; use `--no-split-compactions` to keep the original boundaries. This applies to OMP, Pi, Codex, Claude Code, and OpenCode.

`mine` and `audit` auto-ingest local sessions. They refresh the default database on each run; explicit `--source` or non-default `--format` triggers ingestion into a custom `--db`. `ingest` writes to `$XDG_DATA_HOME/trace2rules/trace2rules.duckdb` by default (`~/.local/share/...`). Set `TRACE2RULES_DB` to override it.

## Public datasets

Use a public dataset when the local corpus is small. Both datasets are public and require no Hugging Face token. The first run downloads the parquet into `~/.cache/trace2rules`; set `TRACE2RULES_CACHE_DIR` to move it, or pass `--refresh-cache` to re-download.

### `swe-rebench`

[nebius/SWE-rebench-openhands-trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories)

```bash
trace2rules ingest --format swe-rebench
trace2rules ingest --format swe-rebench --repos django__django --limit 50
```

The default repositories are `django__django` and `pytest-dev__pytest`. For `swe-rebench`, `--repos` takes instance prefixes such as `django__django`.

### `open-swe`

[nvidia/Open-SWE-Traces](https://huggingface.co/datasets/nvidia/Open-SWE-Traces)

```bash
trace2rules ingest --format open-swe --repos owner/name
```

`--repos` is required for `open-swe` and takes repository labels in `owner/name` form. This downloads six public `train-*.parquet` files.

### `agent-trajectory`

Local Claude Code, Codex, Pi, OMP, and OpenCode SQLite/JSON stores. See [Local sessions](#local-sessions).

## Install

```bash
uv tool install trace2rules
# or
pip install trace2rules
```

For development from a clone:

```bash
uv sync
```

`agent-trajectory` calls its bundled JS bridge through the system Node runtime for Claude Code, Codex, Pi, and OMP. Install Node.js 20 or newer; OpenCode SQLite ingestion is pure Python.

## Configuration

Variables are read from the process environment. trace2rules does not load `.env` files.

| Variable | Purpose |
|---|---|
| `TRACE2RULES_DB` | Override the default DuckDB path |
| `TRACE2RULES_CACHE_DIR` | Local parquet cache; defaults to `~/.cache/trace2rules` |
| `TRACE2RULES_WINDOWS_STORES` | Auto-detects WSL and scans `/mnt/c/Users`; set to `0` to disable or `1` to force |
| `TRACE2RULES_WORKERS` | Mining worker count; defaults to the memory budget |
| `TRACE2RULES_MEMORY_LIMIT` | DuckDB memory cap; defaults to 25% of available memory |

## Data and privacy

- Session data stays in your local DuckDB database.
- `mine` performs no LLM calls. Findings can still contain commands, paths, and repository names; review before sharing a prompt externally.
- `data/` is gitignored.

## Known limits

- Detectors are heuristics. Review every proposed rule.
- Local-session formats change frequently. `agent-trajectory` is the compatibility layer; pin its version.

## Development

```bash
uv sync
uv run pytest
uv build
```

## License

MIT. See `LICENSE`.
