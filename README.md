# trace2rules

Mine and audit coding-agent sessions into evidence-backed `AGENTS.md` rules.

`trace2rules` is a local-first CLI. It ingests agent trajectories, mines recurring friction, audits existing `AGENTS.md`/`CLAUDE.md` rules against real sessions, and identifies whether each rule is observed, compliant, violated, unobserved, or already enforced by CI/pre-commit.

Status: beta. The output is a proposal, not an automatically applied `AGENTS.md`.

## Examples

### Auditing a real `AGENTS.md`

On the SWE-rebench `swc-project/swc` split (188 sessions), the current `AGENTS.md` audit finds 66 rules: 9 measured, 1 partial, and 56 unmeasured.

| Rule | Coverage | Followed | Violations | Confidence | Enforcement |
|---|---|---:|---|---|---|
| Read the codebase before editing | measured | 121 | 64/185 (35%) | high | guidance only |
| Verify assumptions against source and tests | measured | 121 | 64/185 (35%) | high | guidance only |
| `git commit` only after `git add` | measured | 8 | 6/14 (43%) | low | guidance only |
| Submodule update before tests | measured | 0 | 32/32 (100%) | medium | guidance only |
| Snapshot updates with `UPDATE=1 cargo test` | measured | 11 | 147/171 (86%) | high | missing |
| `cargo fmt --all` | measured | 0 | 185/185 (100%) | high | CI configured |
| `cargo clippy --all --all-targets -- -D warnings` | measured | 0 | 185/185 (100%) | high | CI configured |
| `cargo test -p <crate>` | measured | 5 | 158/171 (92%) | high | missing |
| `--no-verify` guard | partial | 13 | 1/14 (7%) | low | missing |

`configured` means the command appears in `.github/workflows/CI.yml`; it does not prove the check is required by branch protection. `available` means a Makefile/package target exists but is not wired into CI. `missing` means no mechanism was found.

### Rules synthesized from report prompts

**`swc-project/swc`**

- [guidance] Reproduce before editing crate sources: 118 sessions edited source with no preceding failing exec. Start with a targeted test such as `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p swc_ecma_transforms_compat --lib regex_dot_all --offline`.
- [guidance] Read the exact target range before editing: 72 sessions made 132 edits with no prior read, e.g. inspect the `ExprExt` sites in `crates/swc_ecma_utils/src/lib.rs`.
- [enforceable] Require validation after edits: 26 sessions had no test/lint exec after the first edit and 74 of 188 sessions were unvalidated. Add a CI job running `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p <changed-crate> --offline`; the pinned toolchain was the successful delta in 21 sessions.

**`rkkautsar/trace2rules`**

- [enforceable] Make `uv run pytest -q` a required check in `.github/workflows/ci.yml`.
- [enforceable] Keep dependencies declared in `pyproject.toml` and install with `uv sync --frozen`; `environment_thrash` found 25 package-manager calls across 3 sessions, 4 failed.
- [guidance] Read the owning package module by symbol or range, not whole-file: the synthesis and ingest modules were read-only hotspots in 22/62 and 20/62 sessions.

**`rkkautsar/louibot`**

- [enforceable] Gate changes with `npm run format:check && npm run build && npm test`, matching `.github/workflows/ci.yml`; 3 sessions used each validation command.
- [guidance] Read `src/agent.ts` and its matching test before editing: 133 edits had no prior read across 6 sessions, and `src/agent.ts` had 227 reads across 9 sessions.
- [enforceable] Block `git push --force-with-lease` on `codex/*` branches and `rm -rf .louibot/benchmarks/runs/<run>`; 3 destructive/gate-bypassing invocations occurred across 3 sessions.

**`rkkautsar/personal-website`**

- [enforceable] Run `npm run lint && npm run format:check`, plus `npm run check:worker` for `functions/**` and `npm run test:discussion` for `functions/api/discussion/[slug].ts`; 3 sessions made edits with no test/lint exec after the first edit.
- [enforceable] Reproduce API/worker failures before editing source: 3 sessions edited source with no preceding failing exec; extend `npm run test:discussion` first.
- [guidance] Read `src/components/sections/WritingSection.tsx`, `src/index.css`, and `src/blog/client.ts` before editing; 117 edits across 3 sessions had no prior read of the target.

## Scope

`trace2rules` proposes **durable, normative `AGENTS.md` rules**, not memories.

It targets two destinations:

- **Enforceable rules**: a CI check, pre-commit hook, lint rule, or test can observe and enforce them.
- **Stable AGENTS rules**: durable repo or global policy that future agents should follow even though it is not mechanically checkable.

It deliberately does not mine memories, historical decisions, ownership, dates, task state, or "what happened" summaries. Those are contextual and belong in a memory system, not in `AGENTS.md`. If evidence is descriptive rather than prescriptive, the tool should omit it.

## Quick start

Audit the current checkout:

```bash
trace2rules audit
trace2rules audit --prompt > audit.txt
```

Mine your local sessions and write one report prompt per detected repo:

```bash
uv tool install trace2rules
trace2rules run --out-dir reports/
```

Include an existing `AGENTS.md` audit in each prompt:

```bash
trace2rules run --out-dir reports/ --agents AGENTS.md
```

Then give `reports/*.md` to your own coding agent to synthesize the rules. `run` ingests local sessions automatically when the database is empty. `trace2rules synthesize --report reports/acme__repo.md` is an optional thin wrapper for the same prompt.

## How it works

1. **Ingest** normalizes tool calls into DuckDB (`sessions`, `tool_events`, `feedback_events`, `test_results`).
2. **Mining** finds hotspots, recovery loops, validation gaps, edit rejections, blind edits, environment thrash, destructive commands, generated-path edits, dependency drift, and correction loops.
3. **Sequence mining** normalizes commands and paths, anchors patterns at errors, allows small gaps, and reports support/failure context. Exact sequences use lift and paired-rework evidence; windows never cross a compaction boundary.
4. **Report** selects bounded findings and writes one evidence prompt per repo. The optional `synthesize --report` wrapper sends that prompt to an LLM; it does not re-mine.
5. **Audit** checks existing `AGENTS.md` rules against sessions. `holdout` splits sessions by time and audits the same rules on an earlier train slice and later holdout.

Generated verifier scripts and low-support task-specific sequences are filtered before synthesis.

## Commands

| Command | Purpose |
|---|---|
| `ingest` | Load sessions and tool events into DuckDB |
| `mine` | Show friction findings and failure signals |
| `report` | Emit findings and evidence as JSON or a report prompt |
| `run` | Ingest if needed, then write one evidence prompt per detected repo |
| `doctor` | Check install, Node, credentials, source stores, and database schema |
| `patterns` | Mine error-anchored, normalized attribute patterns |
| `exact` | Show recurring normalized sequences with evidence |
| `synthesize` | Send an existing report prompt to an LLM |
| `audit` | Audit existing `AGENTS.md` rules against sessions and suggest enforcement |
| `holdout` | Audit rules on an earlier train split and a later time holdout |

Useful options:

```bash
trace2rules ingest                         # ~, all detected local stores
trace2rules ingest --format swe-rebench    # opt into the public dataset
trace2rules ingest --source PATH --limit 200
trace2rules synthesize --report reports/acme__repo.md
trace2rules synthesize --report reports/acme__repo.md --json
trace2rules exact --length 4 --per-repo 2
trace2rules patterns --top 10
trace2rules audit --agents AGENTS.md
trace2rules audit --agents AGENTS.md --commit HEAD --json
trace2rules audit --agents AGENTS.md --prompt
trace2rules holdout --agents AGENTS.md --fraction 0.3
trace2rules report --prompt --agents AGENTS.md
trace2rules report --prompt --min-support 5
trace2rules run --out-dir reports/ --agents AGENTS.md
```

## Local sessions

`--source` is repeatable and defaults to your home directory. It scans:

- `~/.claude/projects`
- `~/.codex/sessions`
- `~/.pi/agent/sessions`
- `~/.omp/agent/sessions`
- `~/.local/share/opencode`

The default cap is 200 sessions per source and selects the most recently modified files; use `--limit 0` for all. OpenCode is read directly from its SQLite database; JSON exports are still accepted:

```bash
uv run trace2rules ingest --format agent-trajectory --source ~/.local/share/opencode
```

The repository label is inferred from the git remote (`owner/name`) when available, falling back to the working-directory basename. `report --prompt` emits one prompt per detected repo:

```bash
trace2rules report --prompt --out-dir reports/
```

`run` and `mine` auto-ingest local sessions when the database is empty. `ingest` writes to `$TMPDIR/trace2rules/trace2rules.duckdb` by default. Set `TRACE2RULES_DB` to use a persistent path.

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

Supported ingestion formats:

| Format | What it reads |
|---|---|
| `swe-rebench` | `nebius/SWE-rebench-openhands-trajectories` parquet, opt-in and cached locally from Hugging Face |
| `agent-trajectory` | Local Claude Code, Codex, Pi, OMP, OpenCode SQLite/JSON |

Datasets are not included in this repository.

## Synthesis wrapper

`run`/`report --prompt` produce the evidence prompt. The normal path is to hand that prompt to your own coding agent.

`trace2rules synthesize --report reports/acme__repo.md` reads the file and sends that exact prompt to an OpenAI-compatible chat model. It does not query DuckDB, re-mine sessions, or reconstruct the prompt.

Provider selection:

1. `TRACE2RULES_LLM_API_KEY` — custom OpenAI-compatible endpoint.
2. `GEMINI_API_KEY` or `GOOGLE_API_KEY` — Gemini OpenAI-compatible endpoint.
3. `OPENAI_API_KEY` — OpenAI.

`TRACE2RULES_BASE_URL` makes any OpenAI-compatible endpoint usable, including a LiteLLM proxy.

## Configuration

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini synthesis provider |
| `GOOGLE_API_KEY` | Alternate Gemini key |
| `OPENAI_API_KEY` | OpenAI synthesis provider |
| `TRACE2RULES_LLM_API_KEY` | API key for a custom OpenAI-compatible endpoint |
| `TRACE2RULES_BASE_URL` | OpenAI-compatible base URL (including LiteLLM) |
| `TRACE2RULES_MODEL` | Synthesis model, e.g. `gemini-4-hydrogen` |
| `TRACE2RULES_CACHE_DIR` | Local parquet cache; defaults to `~/.cache/trace2rules` |
| `HF_TOKEN` | Used by `hf download` for gated datasets; not read by trace2rules |

## Data and privacy

- Session data stays in your local DuckDB database.
- `synthesize --report` sends only the report prompt, not the full raw session database.
- `report`/`run` perform no LLM calls. Findings can still contain commands, paths, and repository names; review before using an external provider.
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
