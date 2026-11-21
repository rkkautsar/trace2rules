from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding

_LOCKFILES = r"(?:Cargo\.lock|uv\.lock|pdm\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock)"
_MANIFESTS = r"(?:Cargo\.toml|pyproject\.toml|package\.json|pnpm-workspace\.yaml)"


def lockfile_edits(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = rf"""
        WITH per_session AS (
            SELECT e.repo, e.session_id,
                   count(*) FILTER (
                       WHERE e.action_type = 'edit'
                         AND regexp_matches(e.target, '(^|/){_LOCKFILES}$')
                   ) AS lock_edits,
                   count(*) FILTER (
                       WHERE e.action_type = 'edit'
                         AND regexp_matches(e.target, '(^|/){_MANIFESTS}$')
                   ) AS manifest_edits,
                   sum(e.approx_out_tokens) AS tokens,
                   arg_min(e.step_idx, e.step_idx) AS first_step
            FROM tool_events e
            GROUP BY 1, 2
        )
        SELECT repo, count(*) AS sessions, sum(lock_edits) AS edits,
               sum(tokens) AS tokens,
               arg_min(session_id, first_step) AS example_session,
               arg_min(first_step, first_step) AS example_step
        FROM per_session
        WHERE lock_edits > 0 AND manifest_edits = 0
        GROUP BY 1
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="lockfile_edit",
            repo=repo,
            title="lockfile edited without manifest change",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} lockfile edits across {sessions} sessions without a manifest edit",
            side="agent",
            recommendation="Regenerate lockfiles through the package manager and commit them with the matching manifest change; do not hand-edit lockfiles.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, sessions, edits, tokens, example_session, example_step in con.execute(query).fetchall()
    ]
