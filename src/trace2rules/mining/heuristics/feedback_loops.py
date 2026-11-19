from __future__ import annotations

import duckdb

from ...detectors_core import Finding


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
