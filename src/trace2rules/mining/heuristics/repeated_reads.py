from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding
from ..repetition import repetition_findings


def repeated_reads(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for item in repetition_findings(
        con,
        "read",
        per_session_min=2,
        total_min=2,
        sessions_min=2,
        limit=limit,
    ):
        step = con.execute(
            "SELECT min(step_idx) FROM tool_events WHERE session_id = ? AND target = ? AND action_type = 'read'",
            [item.example_session, item.target],
        ).fetchone()[0]
        findings.append(
            Finding(
                kind="reread",
                repo=item.repo,
                title=item.target,
                sessions=item.sessions,
                tokens=item.tokens,
                detail=(
                    f"{item.actions - item.sessions} extra reads across "
                    f"{item.sessions} sessions"
                ),
                recommendation=(
                    f"Read `{item.target[:100]}` once, then reread only the enclosing range after it changes; "
                    "avoid repeatedly loading the whole file."
                ),
                side="agent",
                window=_representative_window(
                    con, item.example_session, int(step or 0)
                ),
            )
        )
    return findings
