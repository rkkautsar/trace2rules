# trace2rules

Mine and audit coding-agent sessions into evidence-backed `AGENTS.md` rules.

`trace2rules` is a local-first CLI. It ingests agent trajectories, mines recurring friction, audits existing `AGENTS.md`/`CLAUDE.md` rules against real sessions, and identifies whether each rule is observed, compliant, violated, unobserved, or already enforced by CI/pre-commit. New rule proposals are reconciled against existing rules instead of blindly duplicated.

Status: beta. The output is a proposal, not an automatically applied `AGENTS.md`.

## Example finding

On SWC (Rust), auditing the repository’s real `AGENTS.md` against 188 local sessions showed the gap between written rules and actual behavior:

| Rule | Observed | Applicable | Compliant | Violations | Enforcement |
|---|---:|---:|---:|---:|---|
| `Prefer fixture tests over inline (#[test]) tests` | 5 | 188 | 1 | 187 | missing |
| `Use gh CLI for github.com data` | 87 | 188 | 0 | 0 | configured in `.github/workflows/CI.yml` |

The first rule is a soft instruction: nearly every session was applicable, but only five were observed exercising it. The second is already backed by CI. That distinction is what `audit` exposes.

The same sessions produce suggested rules:

```markdown
## test_command
- **Rule:** Run `RUSTUP_TOOLCHAIN=1.84.1 cargo test -p <crate> --offline` after editing a crate.
  **Evidence:** 21 sessions spent the bootstrap retry; 74 of 188 sessions ended without validation.

## context_hygiene
- **Rule:** Locate a symbol with `rg -n` and read the enclosing line range instead of paging through large files such as `sequences.rs`.
  **Evidence:** 33 reads of `sequences.rs` across 7 sessions averaged 1,802 tokens.
```

### Local examples

- **`rkkautsar/trace2rules`**: use `uv run pytest -q`; map changes through `sources/` → `ingest.py` → `detectors.py` → `cluster.py` → `synthesize.py` → `cli.py`.
- **`rkkautsar/louibot`**: verify with `npx tsc --noEmit && npm test && npm run build`; use `python3.10` for vendored Mini-SWE-Agent code.
- **`rkkautsar/personal-website`**: after blog/OG edits run `node scripts/generate-blog.mjs` and verify the generated `og:image` tags.

Rules are capped at 5 rules, 60 lines, and 2,500 characters per repo.

## Quick start: audit existing rules

Run from the target checkout. `AGENTS.md` and `CLAUDE.md` are auto-detected.

```text
Install trace2rules if missing (`uv tool install trace2rules`). Audit this repo's AGENTS.md and CLAUDE.md against my local sessions with `trace2rules audit`. Report unobserved, violated, and unenforced rules, and propose existing CI/pre-commit checks without duplicating configured enforcement.
```

Manual:

```bash
trace2rules audit --prompt > audit.txt
```

Then give `audit.txt` to your agent.

## Quick start: your own sessions

Ask your coding agent to mine, report, and synthesize:

```text
Install trace2rules if missing (`uv tool install trace2rules`). Run `trace2rules ingest` for my local agent sessions, then `trace2rules mine`, then `trace2rules report --prompt`. Read that report and synthesize repo-specific AGENTS.md rules yourself. Keep only rules supported by the evidence.
```

Or run the evidence step manually:

```bash
uv tool install trace2rules
trace2rules ingest
trace2rules mine
trace2rules report --prompt > evidence.txt
```

Then give `evidence.txt` to your agent to synthesize the rules. `trace2rules synthesize` is the optional built-in LLM fallback.

To write one prompt per detected repo instead of one combined stream:

```bash
trace2rules report --prompt --out-dir reports/
```

`--source` is repeatable and defaults to your home directory. It scans all standard local stores: `~/.claude/projects`, `~/.codex/sessions`, `~/.pi/agent/sessions`, `~/.omp/agent/sessions`, and `~/.local/share/opencode`. The default cap is 200 sessions per source and selects the most recently modified files; use `--limit 0` for all.

For local sessions, the repo label comes from the git remote (`owner/name`) when available, falling back to the working-directory basename. `report --prompt` emits one prompt per detected repo. Run `trace2rules mine` to see labels.

Agent synthesis from the report is the default workflow. `trace2rules synthesize` is optional; add `--offline` to force its deterministic rules.

`ingest` writes to `$TMPDIR/trace2rules/trace2rules.duckdb` by default. Set `TRACE2RULES_DB` to use a persistent path.

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
trace2rules synthesize
trace2rules audit --agents AGENTS.md
trace2rules audit --agents AGENTS.md --commit HEAD --json
trace2rules audit --agents AGENTS.md --prompt
trace2rules report --prompt --agents AGENTS.md
```

## Agentic workflow

The built-in synthesizer is optional. An agent can consume the evidence directly:

```bash
trace2rules ingest
trace2rules report > /tmp/trace2rules-report.json
trace2rules report --prompt > /tmp/trace2rules-prompt.txt
```

`report` emits JSON with repo/session/instance counts, findings, exact sequences, and patterns. For the main agent workflow, `report --prompt` emits the evidence prompt directly; feed that prompt to a fresh agent and let it write the rules. The prompt includes findings, support, windows, and source references, and excludes orientation/hotspot noise. Each finding carries `sessions`, `tokens`, `evidence`, and detector-specific fields such as `lift`, `rework`, `setup_tax`, `cost`, and `paired_instances`.

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
