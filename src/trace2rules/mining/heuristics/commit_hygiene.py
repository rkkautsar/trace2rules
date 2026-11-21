from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding


def commit_hygiene(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH per_session AS (
            SELECT e.repo, e.session_id,
                   count(*) FILTER (WHERE e.action_type = 'edit') AS edits,
                   count(*) FILTER (
                       WHERE e.action_type = 'exec'
                         AND e.outcome = 'ok'
                         AND regexp_matches(
                             coalesce(nullif(e.raw_target, ''), e.target),
                             '(?i)(^|[;&|]\s*)git\s+commit\b'
                         )
                   ) AS commits,
                   max(e.step_idx) FILTER (WHERE e.action_type = 'edit') AS last_edit,
                   max(e.step_idx) FILTER (
                       WHERE e.action_type = 'exec'
                         AND e.outcome = 'ok'
                         AND regexp_matches(
                             coalesce(nullif(e.raw_target, ''), e.target),
                             '(?i)(^|[;&|]\s*)git\s+commit\b'
                         )
                   ) AS last_commit,
                   sum(e.approx_out_tokens) AS tokens
            FROM tool_events e
            WHERE e.action_type IN ('edit', 'exec')
            GROUP BY 1, 2
        ), flagged AS (
            SELECT *, CASE
                WHEN edits >= 8 AND commits = 0 THEN 'no commit after edits'
                WHEN edits >= 12 AND commits = 1 THEN 'one large commit'
                WHEN last_commit IS NOT NULL AND last_edit > last_commit AND edits >= 4 THEN 'edits after final commit'
            END AS reason
            FROM per_session
        )
        SELECT repo, reason, count(*) AS sessions, sum(edits) AS edits,
               sum(tokens) AS tokens,
               arg_min(session_id, last_edit) AS example_session,
               arg_min(last_edit, last_edit) AS example_step
        FROM flagged
        WHERE reason IS NOT NULL
        GROUP BY 1, 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="commit_hygiene",
            repo=repo,
            title=reason,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits across {sessions} sessions with {reason}",
            side="agent",
            recommendation=(
                "Commit in small, reviewable batches and commit after the final edit; "
                "do not leave a large edit streak uncommitted."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, reason, sessions, edits, tokens, example_session, example_step in con.execute(query).fetchall()
    ]
