# trace2rules — architecture

## Goal

Turn coding-agent sessions into a small set of repository-specific `AGENTS.md` proposals backed by measured evidence.

## Pipeline

```text
source adapter
  -> canonical Session / ToolCall
  -> DuckDB sessions + tool_events
  -> detectors (hotspots, recovery loops, context blasts)
  -> exact sequence mining + lift / paired rework
  -> gated findings
  -> LLM synthesis (deterministic fallback)
  -> AGENTS.<owner>__<repo>.proposed.md
```

## Sources

| Format | Adapter | Notes |
|---|---|---|
| `swe-rebench` | `ingest.py` | OpenHands-style parquet, streamed from Hugging Face |
| `swe-chat` | `sources/swe_chat.py` | Normalized `sessions.parquet` + `conversations.parquet` |
| `agent-trajectory` | `sources/agent_trajectory.py` | Claude Code, Codex, Pi, OMP, and OpenCode JSON exports |

`agent-trajectory` is an optional extra because it requires Node.js 20+.

## DuckDB

`sessions` stores one row per session:

```text
session_id, repo, instance_id, resolved, total_turns,
source, agent, success_score
```

`tool_events` stores normalized tool calls:

```text
event_id, session_id, repo, step_idx,
tool_name, action_type, verb, object_id, target, raw_target,
outcome, exit_code, error_signature, is_error,
approx_out_tokens, obs_excerpt
```

`target_labels` is optional and populated by `trace2rules label`:

```text
tool_name, action_type, target,
intent, intent_confidence,
object_type, object_confidence,
model, labeled_at
```

`write_sessions()` replaces rows by `session_id`, so re-importing a session is idempotent.

## Evidence

- Setup failures: repeated missing-import / editable-install patterns.
- Same-error loops: repeated exception signatures across edits.
- Hotspots: read-only files or paths touched across many sessions.
- Context blasts: commands or reads averaging large output.
- Exact sequences: 3-step windows with support, lift, and paired-rework evidence.
- Intent labels: optional clef classification of `(intent, object_type)` used by `patterns` to mine semantic friction sequences.

Generated verifier scripts and low-support task-specific sequences are filtered before synthesis.

## Output constraints

Per repository:

- at most 5 rules;
- at most 60 lines;
- at most 2,500 characters;
- no file is written when no rule survives.

`--offline` uses deterministic rules. The LLM path receives gated findings, not the raw database.

## Non-goals

- Automatically editing a real `AGENTS.md`.
- Reconstructing tool execution that the agent never records.
- Real-time LiteLLM proxy capture.
- Committing datasets or generated proposal files.

## Data and privacy

Session data stays in `data/trace2rules.duckdb`. Downloaded datasets, generated proposals, and secrets are gitignored. External LLM and labeling calls are opt-in or explicit commands.
