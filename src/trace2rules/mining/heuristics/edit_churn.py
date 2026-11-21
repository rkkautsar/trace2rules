from __future__ import annotations

import re

import duckdb

from ..core import _representative_window
from ..models import Finding
from ..repetition import repetition_findings


def edit_churn(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for item in repetition_findings(
        con,
        "edit",
        per_session_min=3,
        total_min=3,
        sessions_min=2,
        limit=limit,
    ):
        step = con.execute(
            "SELECT min(step_idx) FROM tool_events WHERE session_id = ? AND target = ? AND action_type = 'edit'",
            [item.example_session, item.target],
        ).fetchone()[0]
        scratch = bool(
            re.search(r"(?i)(reproduce|repro|scratch|debug|verify)", item.target)
        )
        recommendation = (
            f"Stop patching `{item.target[:100]}`; fix the product path or the test input instead."
            if scratch
            else f"Inspect `{item.target[:100]}` before each edit and batch related changes instead of repeated rewrites."
        )
        findings.append(
            Finding(
                kind="edit_churn",
                repo=item.repo,
                title=item.target,
                sessions=item.sessions,
                tokens=item.tokens,
                detail=f"{item.actions} edits across {item.sessions} sessions",
                recommendation=recommendation,
                side="agent",
                window=_representative_window(
                    con, item.example_session, int(step or 0)
                ),
            )
        )
    return findings
