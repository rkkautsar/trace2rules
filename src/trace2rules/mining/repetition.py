from __future__ import annotations

from dataclasses import dataclass

import duckdb

COMMON_TARGET_FILTER = """
    AND target NOT IN ('', '.')
    AND target NOT LIKE '<%'
    AND target NOT LIKE '% %'
    AND target NOT LIKE '%>%'
    AND target NOT LIKE 'skill:%'
    AND target NOT LIKE 'xd:%'
"""


@dataclass(frozen=True)
class Repetition:
    repo: str
    target: str
    sessions: int
    actions: int
    tokens: int
    example_session: str
    family: str = ""


def repetition_findings(
    con: duckdb.DuckDBPyConnection,
    action_type: str,
    *,
    per_session_min: int | None,
    total_min: int,
    sessions_min: int,
    include_family: bool = False,
) -> list[Repetition]:
    """Find the same action on the same target repeated across sessions.

    ``per_session_min`` filters sessions with a concentrated burst; ``total_min``
    filters the aggregate. The caller maps the result to a Finding kind.
    """
    per_session_having = (
        f"HAVING count(*) >= {int(per_session_min)}"
        if per_session_min is not None
        else ""
    )
    columns = {
        row[1] for row in con.execute("PRAGMA table_info('tool_events')").fetchall()
    }
    family = (
        "any_value(error_family) AS family"
        if include_family and "error_family" in columns
        else "'' AS family"
    )
    rows = con.execute(
        f"""
        WITH per_session AS (
            SELECT repo, target, session_id, count(*) AS actions,
                   sum(approx_out_tokens) AS tokens,
                   {family}
            FROM tool_events
            WHERE action_type = ?
              {COMMON_TARGET_FILTER}
            GROUP BY 1, 2, 3
            {per_session_having}
        )
        SELECT repo, target, count(*) AS sessions, sum(actions) AS actions,
               sum(tokens) AS tokens,
               arg_max(session_id, actions) AS example_session,
               any_value(family) AS family
        FROM per_session
        GROUP BY 1, 2
        HAVING sum(actions) >= ? AND count(*) >= ?
        ORDER BY actions DESC, sessions DESC
        """,
        [action_type, int(total_min), int(sessions_min)],
    ).fetchall()
    return [
        Repetition(
            repo=repo,
            target=target,
            sessions=int(sessions),
            actions=int(actions),
            tokens=int(tokens or 0),
            example_session=example_session,
            family=family or "",
        )
        for repo, target, sessions, actions, tokens, example_session, family in rows
    ]
