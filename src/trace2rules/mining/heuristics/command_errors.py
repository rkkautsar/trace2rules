from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding

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
