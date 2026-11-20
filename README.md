# trace2rules

Mine and audit coding-agent sessions into evidence-backed `AGENTS.md` rules.

`trace2rules` is a local-first CLI. It ingests agent trajectories, mines recurring friction, audits existing `AGENTS.md`/`CLAUDE.md` rules against real sessions, and identifies whether each rule is observed, compliant, violated, unobserved, or already enforced by CI/pre-commit. New rule proposals are reconciled against existing rules instead of blindly duplicated.

Status: beta. The output is a proposal, not an automatically applied `AGENTS.md`.

## Scope

`trace2rules` proposes **durable, normative `AGENTS.md` rules**, not memories.

It targets two destinations:

- **Enforceable rules**: a CI check, pre-commit hook, lint rule, or test can observe and enforce them.
- **Stable AGENTS rules**: durable repo or global policy that future agents should follow even though it is not mechanically checkable.

It deliberately does not mine memories, historical decisions, ownership, dates, task state, or "what happened" summaries. Those are contextual and belong in a memory system, not in `AGENTS.md`. If evidence is descriptive rather than prescriptive, the tool should omit it.

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

## Example audit

On this repository’s local sessions, auditing the current `AGENTS.md` against 12 applicable sessions found four rules:

| Rule | Coverage | Followed | Violations | Confidence | Enforcement |
|---|---|---:|---|---|---|
| Locate symbols with `rg -n`, then read the enclosing range | measured | 1 | 4/5 (80%) | low | guidance only |
| Avoid whole-file reads on large modules | measured | 1 | 4/5 (80%) | low | guidance only |
| Run `uv run pytest -q` | measured | 0 | - | none | missing |
| Run `uv run pytest -q tests/test_cli.py -k <pattern>` | measured | 0 | - | none | missing |

The unobserved test rules are still useful candidates: the audit recommends making them required CI/pre-commit checks instead of repeating them in `AGENTS.md`.

## Example patterns

After normalizing command families and path classes, error-anchored patterns look like this:

```text
exec:python:ok -> exec:pytest:err
edit:test:ok -> exec:python:err
edit:scratch:ok -> exec:python:err
edit:test:ok -> exec:python:ok -> exec:pytest:err
```

Raw variants remain available as evidence. Before normalization, these windows contained unique commands such as `pytest tests/dialects/test_snowflake.py -q`, so they did not cluster across sessions.

Useful commands:

```bash
trace2rules patterns --top 10
trace2rules exact --length 4 --per-repo 2
```

`exact` balances output per repository; `patterns` uses normalized, error-anchored attribute sequences with up to two skipped events.

## Real-world examples

These were regenerated from the current report prompts, not copied from the earlier README examples.

**`swc-project/swc`**

- [guidance] Reproduce the defect before editing crate sources: 118 sessions edited source with no preceding failing exec. Start with a targeted test such as `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p swc_ecma_transforms_compat --lib regex_dot_all --offline`.
- [guidance] Read the exact target range before editing: 72 sessions made 132 edits with no prior read of the target, e.g. inspect the `ExprExt` sites in `crates/swc_ecma_utils/src/lib.rs`.
- [enforceable] Require a test/lint command after edits: 26 sessions had no test/lint exec after the first edit and 74 of 188 sessions were unvalidated. Add a CI job running `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p <changed-crate> --offline`; the pinned toolchain was the successful delta in 21 sessions.

**`rkkautsar/trace2rules`**

- [enforceable] Make `uv run pytest -q` a required check in `.github/workflows/ci.yml`; the current audit shows the rule unobserved in applicable sessions.
- [enforceable] Keep dependencies declared in `pyproject.toml` and install with `uv sync --frozen`; `environment_thrash` found 25 package-manager calls across 3 sessions, 4 failed.
- [guidance] Read the owning package module by symbol or range, not whole-file: the synthesis and ingest modules were read-only hotspots in 22/62 and 20/62 sessions.

**`rkkautsar/louibot`**

- [enforceable] Gate changes with `npm run format:check && npm run build && npm test`, matching `.github/workflows/ci.yml`; 3 sessions used each validation command.
- [guidance] Read the target region of `src/agent.ts` and its matching test before editing: 133 edits had no prior read across 6 sessions, and `src/agent.ts` had 227 reads across 9 sessions.
- [enforceable] Block `git push --force-with-lease` on `codex/*` branches and `rm -rf .louibot/benchmarks/runs/<run>`; 3 destructive/gate-bypassing invocations occurred across 3 sessions.

**`rkkautsar/personal-website`**

- [enforceable] Run `npm run lint && npm run format:check`, plus `npm run check:worker` for `functions/**` and `npm run test:discussion` for `functions/api/discussion/[slug].ts`; 3 sessions made edits with no test/lint exec after the first edit.
- [enforceable] Reproduce API/worker failures before editing source: 3 sessions edited source with no preceding failing exec; extend `npm run test:discussion` first.
- [guidance] Read `src/components/sections/WritingSection.tsx`, `src/index.css`, and `src/blog/client.ts` before editing; 117 edits across 3 sessions had no prior read of the target.

## Local session details

`--source` is repeatable and defaults to your home directory. It scans `~/.claude/projects`, `~/.codex/sessions`, `~/.pi/agent/sessions`, `~/.omp/agent/sessions`, and `~/.local/share/opencode`. The default cap is 200 sessions per source and selects the most recently modified files; use `--limit 0` for all.

For local sessions, the repo label comes from the git remote (`owner/name`) when available, falling back to the working-directory basename. `report --prompt` emits one prompt per detected repo.

To write one prompt per detected repo instead of one combined stream:

```bash
trace2rules report --prompt --out-dir reports/
```

Agent synthesis from the report is the primary workflow. `run` writes the prompts; give them to your own agent.

`trace2rules synthesize` is only an optional thin wrapper: it sends one existing report prompt to a configured LLM and prints the parsed rules. It does not ingest, mine, or rebuild the report.

`run` and `mine` auto-ingest local sessions when the database is empty. `ingest` writes to `$TMPDIR/trace2rules/trace2rules.duckdb` by default. Set `TRACE2RULES_DB` to use a persistent path.

## Audit existing rules

`audit` parses `AGENTS.md` and `CLAUDE.md`, checks each rule against real sessions, and finds existing enforcement:

```bash
trace2rules audit
trace2rules audit --commit HEAD --json
trace2rules audit --prompt
```

`available` means a Makefile/package target exists but is not wired into CI. `missing` means no mechanism was found. `--prompt` emits a self-contained review prompt for a fresh-context agent.

`holdout` fits the same rules on an earlier train slice and a later time holdout, then reports both. This catches rules that only fit the sessions that produced them; it is not a synthesis backtest.

Use `trace2rules synthesize --report reports/acme__repo.md --json` for a machine-readable wrapper result. Each rule carries `support_sessions`, `support_instances`, `confidence`, `finding_kinds`, `evidence_quotes`, and source references when the report prompt includes them.

## Synthesis wrapper

`run`/`report --prompt` produce the evidence prompt. The normal path is to hand that prompt to your own coding agent.

`trace2rules synthesize` is a thin optional wrapper around the same prompt:

```bash
trace2rules synthesize --report reports/acme__repo.md
```

It reads the file and sends that exact prompt to an OpenAI-compatible chat model. It does not query DuckDB, re-mine sessions, or reconstruct the prompt. If no provider is configured it exits with an error.

Provider selection:

1. `TRACE2RULES_LLM_API_KEY` — API key for a custom OpenAI-compatible endpoint.
2. `GEMINI_API_KEY` or `GOOGLE_API_KEY` — Gemini OpenAI-compatible endpoint.
3. `OPENAI_API_KEY` — OpenAI.

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

## Agentic workflow

The built-in synthesizer is optional. An agent can consume the evidence directly:

```bash
trace2rules ingest
trace2rules report > /tmp/trace2rules-report.json
trace2rules report --prompt > /tmp/trace2rules-prompt.txt
```

`report` emits JSON with repo/session/instance counts, findings, exact sequences, and patterns. For the main agent workflow, `report --prompt` emits the evidence prompt directly; feed that prompt to a fresh agent and let it write the rules. The prompt includes findings, support, windows, and source references, and excludes orientation/hotspot noise. Each finding carries `sessions`, `instances`, `confidence`, `tokens`, `evidence`, and detector-specific fields such as `lift`, `rework`, `setup_tax`, `cost`, and `paired_instances`. Prefer `high`/`medium` confidence findings; treat `low` as a lead, not proof. The report prompt is bounded to the top 10 findings, 3 window events per finding, and 40 existing rules.

The calling agent can then write `AGENTS.md` rules from that evidence without configuring an LLM key.

The optional `synthesize --report` wrapper sends that same prompt to one configured model. Rules go to stdout; progress and findings go to stderr.

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

`TRACE2RULES_BASE_URL` also works with an OpenAI-compatible LiteLLM proxy.

## How it works

1. **Ingest** normalizes tool calls into DuckDB (`sessions`, `tool_events`).
2. **Detectors** find high-coverage setup failures, repeated exception signatures, read-only hotspots, and large output calls.
3. **Sequence mining** normalizes commands and paths, anchors patterns at errors, allows small gaps, and reports support/failure context. Exact sequences use lift and paired-rework evidence; windows never cross a compaction boundary.
5. **Report** selects gated findings and writes one evidence prompt per repo. The optional `synthesize --report` wrapper sends that prompt to an LLM; it does not re-mine.

Generated verifier scripts and low-support task-specific sequences are filtered before synthesis.

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
