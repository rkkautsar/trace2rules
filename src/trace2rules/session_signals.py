from __future__ import annotations

import re

import duckdb

from .detectors_core import Finding, _representative_window

PERMISSION_RE = r"(?i)(permission denied|operation not permitted|access denied|not authorized|eacces)"
COMMAND_ERROR_RE = r"(?i)(unrecognized arguments|unrecognized command|unknown option|invalid option|command not found|usage: )"


def command_errors(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = f"""
        SELECT repo, target,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step,
               arg_min(error_message, step_idx) AS example_error
        FROM tool_events
        WHERE outcome = 'err'
          AND action_type IN ('exec', 'read')
          AND regexp_matches(coalesce(error_message, ''), '{COMMAND_ERROR_RE}')
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, events DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, sessions, events, tokens, session, step, error in con.execute(query).fetchall():
        findings.append(
            Finding(
                kind="command_error",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"{events} malformed command invocations across {sessions} sessions",
                recommendation=(
                    f"Verify the flags for `{target[:100]}` with one bounded help call; "
                    "do not retry the same malformed invocation."
                ),
                cause=(error or "")[:240],
                side="agent",
                window=_representative_window(con, session, step),
            )
        )
    return findings


def edit_churn(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH per_session AS (
            SELECT repo, target, session_id, count(*) AS edits,
                   sum(approx_out_tokens) AS tokens
            FROM tool_events
            WHERE action_type = 'edit' AND target NOT IN ('', '.')
            GROUP BY repo, target, session_id
            HAVING count(*) >= 3
        )
        SELECT repo, target,
               count(*) AS sessions,
               sum(edits) AS edits,
               sum(tokens) AS tokens,
               arg_max(session_id, edits) AS example_session
        FROM per_session
        GROUP BY repo, target
        HAVING count(*) >= 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, sessions, edits, tokens, session in con.execute(query).fetchall():
        step = con.execute(
            "SELECT min(step_idx) FROM tool_events WHERE session_id = ? AND target = ? AND action_type = 'edit'",
            [session, target],
        ).fetchone()[0]
        scratch = bool(re.search(r"(?i)(reproduce|repro|scratch|debug|verify)", target))
        recommendation = (
            f"Stop patching `{target[:100]}`; fix the product path or the test input instead."
            if scratch
            else f"Inspect `{target[:100]}` before each edit and batch related changes instead of repeated rewrites."
        )
        findings.append(
            Finding(
                kind="edit_churn",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"{edits} edits across {sessions} sessions",
                recommendation=recommendation,
                side="agent",
                window=_representative_window(con, session, int(step or 0)),
            )
        )
    return findings


def repeated_reads(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH per_session AS (
            SELECT repo, target, session_id,
                   count(*) AS reads,
                   sum(approx_out_tokens) AS tokens
            FROM tool_events
            WHERE action_type = 'read'
              AND target NOT IN ('', '.')
              AND target NOT LIKE '<%'
              AND target NOT LIKE '% %'
              AND target NOT LIKE '%>%'
              AND target NOT LIKE 'skill:%'
              AND target NOT LIKE 'xd:%'
            GROUP BY repo, target, session_id
            HAVING count(*) >= 2
        )
        SELECT repo, target,
               count(*) AS sessions,
               sum(reads - 1) AS extra_reads,
               sum(tokens) AS tokens,
               arg_max(session_id, reads) AS example_session
        FROM per_session
        GROUP BY repo, target
        HAVING count(*) >= 2
        ORDER BY extra_reads DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, sessions, extra, tokens, session in con.execute(query).fetchall():
        step = con.execute(
            "SELECT min(step_idx) FROM tool_events WHERE session_id = ? AND target = ? AND action_type = 'read'",
            [session, target],
        ).fetchone()[0]
        findings.append(
            Finding(
                kind="reread",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"{extra} extra reads across {sessions} sessions",
                recommendation=(
                    f"Read `{target[:100]}` once, then reread only the enclosing range after it changes; "
                    "avoid repeatedly loading the whole file."
                ),
                side="agent",
                window=_representative_window(con, session, int(step or 0)),
            )
        )
    return findings


def permission_denials(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = f"""
        SELECT repo, target,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step,
               arg_min(error_message, step_idx) AS example_error
        FROM tool_events
        WHERE outcome = 'err' AND regexp_matches(coalesce(error_message, ''), '{PERMISSION_RE}')
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, events DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, sessions, events, tokens, session, step, error in con.execute(query).fetchall():
        findings.append(
            Finding(
                kind="permission_denied",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"{events} denied calls across {sessions} sessions",
                recommendation=(
                    f"Do not retry `{target[:100]}` after a permission denial; "
                    "request escalation or fix the entrypoint and file permissions first."
                ),
                cause=(error or "")[:240],
                side="agent",
                window=_representative_window(con, session, step),
            )
        )
    return findings


def feedback_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, keys, count(*) AS mentions,
               count(DISTINCT session_id) AS sessions,
               arg_max(text, step_idx) AS example,
               list(DISTINCT substr(text, 1, 300)) AS examples
        FROM feedback_events
        WHERE keys <> ''
        GROUP BY 1, 2
        HAVING count(*) >= 2
        ORDER BY sessions DESC, mentions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="feedback_loop",
            repo=repo,
            title=key,
            sessions=sessions,
            tokens=mentions,
            detail=f"{mentions} mentions across {sessions} sessions",
            side="agent",
            recommendation=(
                f"The user repeated the `{key}` constraint. Treat it as blocking: "
                "restate it, inspect the current setting, and change the next action instead of retrying."
            ),
            cause=(example or "")[:240],
        )
        for repo, key, mentions, sessions, example, examples in con.execute(query).fetchall()
    ]
