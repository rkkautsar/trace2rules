from __future__ import annotations

import re

import duckdb

from ..core import (
    _BOUNDED_RE,
    _DOC_TARGET_RE,
    _TEST_OR_SCRATCH_RE,
    _context_advice,
    _is_orientation,
    _representative_window,
    _repo_window,
)
from ..models import Finding

def environment_thrash(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH installs AS (
            SELECT session_id, repo, step_idx, approx_out_tokens, is_error,
                   coalesce(nullif(raw_target, ''), target) AS command
            FROM tool_events
            WHERE action_type = 'exec'
              AND regexp_matches(
                  coalesce(raw_target, target),
                  '(?i)\b(pip3?|uv|poetry|conda|npm|yarn|pnpm|cargo|go|bundle|composer|dotnet|mix)\s+(install|add|sync|update|remove|uninstall|upgrade|lock|get|ci)\b'
              )
        )
        SELECT repo,
               count(*) AS installs,
               count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens,
               count(*) FILTER (WHERE is_error) AS failures,
               arg_min(command, step_idx) AS example,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM installs
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2 AND count(*) >= 5
        ORDER BY installs DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="environment_thrash",
            repo=repo,
            title="repeated dependency/environment setup",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{installs} package-manager calls across {sessions} sessions; {failures} failed; example {example[:120]}",
            side="repo",
            recommendation="Prebuild or document the environment. Do not make every session rediscover and reinstall the same tooling.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, installs, sessions, tokens, failures, example, example_session, example_step in con.execute(query).fetchall()
    ]
