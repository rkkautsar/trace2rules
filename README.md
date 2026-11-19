# trace2rules

Mine and audit coding-agent sessions into evidence-backed `AGENTS.md` rules.

`trace2rules` is a local-first CLI. It ingests agent trajectories, mines recurring friction, audits existing `AGENTS.md`/`CLAUDE.md` rules against real sessions, and identifies whether each rule is observed, compliant, violated, unobserved, or already enforced by CI/pre-commit. New rule proposals are reconciled against existing rules instead of blindly duplicated.

Status: beta. The output is a proposal, not an automatically applied `AGENTS.md`.

## Project layout

- `mining/heuristics/`: one focused detector per file, such as `repeated_reads.py`.
- `detectors_core.py`: shared `Finding` model, session summary, and evidence-window helpers.
- `detectors.py`: stable facade for detector imports and the aggregate `all_findings`.
- `session_signals.py`: compatibility facade for the session-signal heuristics.
- `agents.py`: auditing, rule parsing, enforcement, and audit payloads.
- `synthesize.py`: optional built-in synthesis; the main workflow remains `run`/`report`.

New heuristics should live in `mining/heuristics/<name>.py` and be exported from `mining/heuristics/__init__.py`.

## Quick start: audit existing rules

Run from the target checkout. `AGENTS.md` and `CLAUDE.md` are auto-detected.

```text
Install trace2rules if missing (`uv tool install trace2rules`). Audit this repo's AGENTS.md and CLAUDE.md against my local sessions with `trace2rules audit`. Report unobserved, violated, and unenforced rules, and propose existing CI/pre-commit checks without duplicating configured enforcement.
```

Manual:

```bash
trace2rules audit --prompt > audit.txt
```

## Quick start: your own sessions

Ask your coding agent to mine, report, and synthesize:

```text
Install trace2rules if missing (`uv tool install trace2rules`). Run `trace2rules run --out-dir reports/` for my local agent sessions. Read the report files and synthesize repo-specific AGENTS.md rules yourself. Keep only rules supported by the evidence.
```

Manual:

```bash
uv tool install trace2rules
trace2rules run --out-dir reports/
```

To include an existing `AGENTS.md` audit in the prompt:

```bash
trace2rules run --out-dir reports/ --agents AGENTS.md
```

Then give `reports/*.md` to your agent to synthesize the rules. `run` ingests local sessions automatically when the database is empty.

## Example finding

On SWC (Rust), auditing the repository’s real `AGENTS.md` against 188 local sessions found 66 rules, 9 measured, 1 partial, and 56 unmeasured:

| Rule | Coverage | Opportunities | Followed | Violations | Undecided | Confidence | Enforcement | Required |
|---|---|---:|---:|---:|---:|---|---|---|
| Read source before editing | measured | 185 | 121 | 64 | 0 | high | unknown | unknown |
| Verify assumptions by reading source | measured | 185 | 121 | 64 | 0 | high | unknown | unknown |
| `git commit` only after `git add` | measured | 14 | 8 | 6 | 0 | low | missing | no |
| Submodule update before tests | measured | 32 | 0 | 32 | 0 | low | missing | no |
| `cargo test -p <crate>` | measured | 171 | 5 | 158 | 8 | high | missing | no |
| `cargo fmt --all` | measured | 185 | 0 | 185 | 0 | high | configured | unknown |
| `cargo clippy ...` | measured | 185 | 0 | 185 | 0 | high | configured | unknown |
| `--no-verify` guard | partial | 14 | 13 | 1 | 0 | low | missing | no |

Test evidence is also reported per rule, so `cargo test -p <crate>` is not just “5 followed”:

```text
no_test:           147
before_final_edit:  11
alternate_form:      8
exact_form:          5
```

`cargo fmt` and `cargo clippy` are CI-configured but were never run locally. `configured` does not mean required: the branch rulesets do not define required status checks, so those two rules remain `required: unknown`.

The same sessions produce suggested rules:

**`swc-project/swc`**

- Build and validate the touched crate offline with the pinned toolchain, e.g. `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p swc_ecma_codegen --offline`; bare `cargo` attempts to download the nightly channel and fails in this sandbox.
- In large sources such as crates/swc_ecma_codegen/src/lib.rs and crates/swc_ecma_minifier/src/compress/optimize/{mod,sequences}.rs, locate symbols with `grep -n` and read `sed -n` line ranges instead of whole-file reads or repeated full re-reads.

### Local examples

**`rkkautsar/trace2rules`**

- For a large or repeatedly read file, use `rg -n` to locate the symbol and read the enclosing line range; a single whole-file read is fine for small files.
  - 4 sessions, medium confidence, `selector_miss`.
- When a selector reports 0 selected/deselected, inspect the runner's collection output and use the printed node ID; selectors match test names, not arbitrary file text.
  - 4 sessions, medium confidence, `selector_miss`.
- Edit the file named by the traceback or failing assertion; do not patch a different layer or the reproducer.
  - 3 sessions, medium confidence, `site_edit_mismatch`.

**`rkkautsar/louibot`**

- Validate source changes from the repo root with `npm test && npm run build`, the pair used in the recorded validation runs. Do not spend turns on one-off `npx tsc --noEmit ... | head` variants that add no coverage beyond the build.
- `src/agent.ts` is the ~2.7k-line core holding model prompts, controller validation, and thread compaction, while `src/benchmark.ts` runs benchmarks and `benchmarks/README.md` documents them. Locate a symbol with `grep -n` and read a narrow `sed -n` range instead of re-reading the whole file.
- Extract benchmark state by filtering `.event` with `jq` or Python from `.louibot/benchmarks/runs/<run>/louibot-state-*/<n>.jsonl`, and read the run's `report.json` for pass/fail status. Never `grep` or dump whole state JSONL files or `.louibot/benchmarks/runs` trees into context.
- Run scripts that import the vendored Mini-SWE-Agent environment under `.louibot/benchmarks/*/miniswe-agent-env` with `python3.10`. The default `python3` is 3.15 and fails with `ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`.

**`rkkautsar/personal-website`**

- After editing `scripts/generate-blog.mjs` or `scripts/og-image.mjs`, regenerate with `node scripts/generate-blog.mjs` and confirm the affected `dist/blog/<slug>/index.html` contains the expected `<meta property="og:image">`/`og:image:width` tags before reporting done.
- Read `scripts/generate-blog.mjs` and `scripts/og-image.mjs` with targeted line ranges (`sed -n '1,40p'`) around the function under change instead of dumping the whole file in one call.

Rules are capped at 5 rules, 160 lines, and 12,000 characters per repo.

## Local session details

`--source` is repeatable and defaults to your home directory. It scans `~/.claude/projects`, `~/.codex/sessions`, `~/.pi/agent/sessions`, `~/.omp/agent/sessions`, and `~/.local/share/opencode`. The default cap is 200 sessions per source and selects the most recently modified files; use `--limit 0` for all.

For local sessions, the repo label comes from the git remote (`owner/name`) when available, falling back to the working-directory basename. `report --prompt` emits one prompt per detected repo.

To write one prompt per detected repo instead of one combined stream:

```bash
trace2rules report --prompt --out-dir reports/
```

Agent synthesis from the report is the default workflow. `trace2rules synthesize` is the optional built-in LLM fallback.

`run` and `mine` auto-ingest local sessions when the database is empty. `ingest` writes to `$TMPDIR/trace2rules/trace2rules.duckdb` by default. Set `TRACE2RULES_DB` to use a persistent path.

## Audit existing rules

`audit` parses `AGENTS.md` and `CLAUDE.md`, checks each rule against real sessions, and finds existing enforcement:

```bash
trace2rules audit
trace2rules audit --commit HEAD --json
trace2rules audit --prompt
```

Real sqlglot example:

```text
Make sure all tests added to tests/dialects/*.py actually run...
observed: 723  applicable: 1000  compliant: 705
violations: 295  not observed: 277
scope: sqlglot/dialects/snowflake.py
```

```text
Run `make check`
status: configured
source: .github/workflows/package-test.yml
```

`available` means a Makefile/package target exists but is not wired into CI. `missing` means no mechanism was found. `--prompt` emits a self-contained review prompt for a fresh-context agent.

`trace2rules synthesize --agents AGENTS.md` reconciles proposed rules against the audit: duplicates are dropped, configured enforcement is reused, and soft rules are upgraded with concrete enforcement. When run from the target checkout, `--agents` is optional.

Use `trace2rules synthesize --json` for machine-readable proposals. Each rule carries `support_sessions`, `support_instances`, `confidence`, `finding_kinds`, `evidence_quotes`, and source references; the deterministic path also labels non-window evidence as `finding:<kind>:<title>`.

## Synthesis

Synthesis is LLM-first. `trace2rules synthesize` sends redacted gated findings for each repo to an OpenAI-compatible chat model and asks for structured rules. Common API keys, bearer tokens, URL credentials, query tokens, password assignments, and private keys are redacted before external calls; redaction is best-effort, not a guarantee.

Provider selection:

1. `TRACE2RULES_LLM_API_KEY` — API key for a custom OpenAI-compatible endpoint.
2. `GEMINI_API_KEY` or `GOOGLE_API_KEY` — Gemini OpenAI-compatible endpoint.
3. `OPENAI_API_KEY` — OpenAI.

If no key is configured, or the call fails, synthesis falls back to deterministic rules and continues. `--offline` forces the deterministic path without a network call.

`TRACE2RULES_BASE_URL` makes any OpenAI-compatible endpoint usable, including a LiteLLM proxy.

## Supported inputs

| Format | What it reads | Notes |
|---|---|---|
| `swe-rebench` | `nebius/SWE-rebench-openhands-trajectories` parquet | Opt-in; cached locally from Hugging Face |
| `agent-trajectory` | Local Claude Code, Codex, Pi, OMP, OpenCode SQLite/JSON | Default; Node.js 20+ required for non-OpenCode sources |

Datasets are not included in this repository.

## Install

Use the published package:

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

## Local agent sessions

```bash
uv run trace2rules ingest --limit 0
```

By default, it scans the standard stores:

- `~/.claude/projects`
- `~/.codex/sessions`
- `~/.pi/agent/sessions`
- `~/.omp/agent/sessions`
- `~/.local/share/opencode`

OpenCode is read directly from its SQLite database; JSON exports are still accepted:

```bash
uv run trace2rules ingest --format agent-trajectory --source ~/.local/share/opencode
```

The repository label is inferred from the git remote or working-directory basename.

## Commands

| Command | Purpose |
|---|---|
| `ingest` | Load sessions and tool events into DuckDB |
| `mine` | Show friction clusters, hotspots, recovery loops, and context blasts |
| `report` | Emit all findings and patterns as evidence-rich JSON for an agent |
| `run` | Ingest if needed, then write one evidence prompt per detected repo |
| `doctor` | Check install, Node, credentials, source stores, and database schema |
| `patterns` | Mine structural multi-step friction |
| `exact` | Show exact recurring sequences with evidence |
| `synthesize` | Print per-repo rules to stdout |
| `audit` | Audit existing `AGENTS.md` rules against sessions and suggest enforcement |

Useful options:

```bash
trace2rules ingest                         # ~, all detected local stores
trace2rules ingest --format swe-rebench    # opt into the public dataset
trace2rules ingest --source PATH --limit 200
trace2rules synthesize
trace2rules synthesize --offline
trace2rules synthesize --max-rules 10
trace2rules synthesize --min-support 5 --json
trace2rules synthesize
trace2rules audit --agents AGENTS.md
trace2rules audit --agents AGENTS.md --commit HEAD --json
trace2rules audit --agents AGENTS.md --prompt
trace2rules report --prompt --agents AGENTS.md
trace2rules report --prompt --min-support 5
trace2rules run --out-dir reports/ --agents AGENTS.md
```

## Agentic workflow

The built-in synthesizer is optional. An agent can consume the evidence directly:

```bash
trace2rules ingest
trace2rules report > /tmp/trace2rules-report.json
trace2rules report --prompt > /tmp/trace2rules-prompt.txt
```

`report` emits JSON with repo/session/instance counts, findings, exact sequences, and patterns. For the main agent workflow, `report --prompt` emits the evidence prompt directly; feed that prompt to a fresh agent and let it write the rules. The prompt includes findings, support, windows, and source references, and excludes orientation/hotspot noise. Each finding carries `sessions`, `instances`, `confidence`, `tokens`, `evidence`, and detector-specific fields such as `lift`, `rework`, `setup_tax`, `cost`, and `paired_instances`. Prefer `high`/`medium` confidence findings; treat `low` as a lead, not proof. The report prompt is bounded to the top 10 findings, 3 window events per finding, and 40 existing rules.

The calling agent can then write `AGENTS.md` rules from that evidence without invoking `trace2rules synthesize` or configuring an LLM key.

Rules go to stdout; progress and findings go to stderr.

Synthesis is LLM-first. `--offline` skips the LLM and uses deterministic rules.

## Configuration

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini synthesis provider |
| `GOOGLE_API_KEY` | Alternate Gemini key |
| `OPENAI_API_KEY` | OpenAI synthesis provider |
| `TRACE2RULES_LLM_API_KEY` | API key for a custom OpenAI-compatible endpoint |
| `TRACE2RULES_BASE_URL` | OpenAI-compatible base URL (including LiteLLM) |
| `TRACE2RULES_MODEL` | Synthesis model, e.g. `gemini-4-hydrogen` |
| `TRACE2RULES_DECISION_MODEL` | Labeling backend: `clef-flash` (default), `clef`, `jev`, `gemini-decision-1`, `gpt-6-luna` |
| `TRACE2RULES_CACHE_DIR` | Local parquet cache; defaults to `~/.cache/trace2rules` |
| `HF_TOKEN` | Used by `hf download` for gated datasets; not read by trace2rules |

`TRACE2RULES_BASE_URL` also works with an OpenAI-compatible LiteLLM proxy.

## How it works

1. **Ingest** normalizes tool calls into DuckDB (`sessions`, `tool_events`).
2. **Detectors** find high-coverage setup failures, repeated exception signatures, read-only hotspots, and large output calls.
3. **Exact n-gram mining** groups recurring 3–5-step tool sequences with lift and paired-rework evidence. Windows never cross a compaction boundary; nested sequences are deduplicated before selection.
5. **Synthesis** is LLM-first. It selects gated findings and asks the configured model for structured rules; `--offline` forces deterministic rules.

Generated verifier scripts and low-support task-specific sequences are filtered before synthesis.

## Data and privacy

- Session data stays in your local DuckDB database.
- `--offline` performs no LLM calls.
- LLM synthesis sends mined findings, not the full raw session database. Findings can still contain commands, paths, and repository names; review before using an external provider.
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
