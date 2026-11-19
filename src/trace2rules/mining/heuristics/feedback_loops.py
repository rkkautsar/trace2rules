from __future__ import annotations

import duckdb

from ..models import Finding


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


def correction_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH corrections AS (
            SELECT session_id, repo, step_idx, text
            FROM feedback_events
            WHERE step_idx > 0
              AND regexp_matches(
                  text,
                  '(?i)\b(no|not|stop|don''t|do not|instead|wrong|actually|still|but)\b'
              )
        ),
        repeats AS (
            SELECT DISTINCT c.session_id, c.repo, c.step_idx, c.text
            FROM corrections c
            JOIN tool_events before
              ON before.session_id = c.session_id
             AND before.step_idx < c.step_idx
             AND before.step_idx >= c.step_idx - 5
            JOIN tool_events after
              ON after.session_id = c.session_id
             AND after.step_idx > c.step_idx
             AND after.step_idx <= c.step_idx + 5
             AND after.action_type = before.action_type
             AND (
                 (after.target <> '' AND after.target = before.target)
                 OR (after.raw_target <> '' AND after.raw_target = before.raw_target)
             )
        )
        SELECT repo, count(DISTINCT session_id) AS sessions,
               count(*) AS corrections,
               arg_max(text, step_idx) AS example
        FROM repeats
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="correction_loop",
            repo=repo,
            title="same action repeated after user correction",
            sessions=sessions,
            tokens=0,
            detail=f"{corrections} corrections followed by a repeated action",
            cause=(example or "")[:240],
            side="agent",
            recommendation=(
                "After explicit user correction, change the target or approach. "
                "Restate the correction and verify the next action against it."
            ),
        )
        for repo, sessions, corrections, example in con.execute(query).fetchall()
    ]
