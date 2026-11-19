from __future__ import annotations

import duckdb

from ...detectors_core import Finding, _representative_window


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
