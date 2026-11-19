from __future__ import annotations

import re

import duckdb

from ..core import _representative_window
from ..models import Finding


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
