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

def destructive_commands(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        SELECT repo, coalesce(nullif(raw_target, ''), target) AS command,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM tool_events
        WHERE action_type = 'exec'
          AND regexp_matches(
              coalesce(raw_target, target),
              '(?i)(git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f|git\s+checkout\s+(--\s+)?\.|git\s+stash\s+drop|git\s+branch\s+-D|git\s+push\s+.*--force|--no-verify|HUSKY=0|core\.hooksPath|--admin)'
          )
        GROUP BY 1, 2
        ORDER BY sessions DESC, events DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="destructive_command",
            repo=repo,
            title=command,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{events} destructive or gate-bypassing invocations",
            side="agent",
            recommendation="Do not run destructive git or bypass verification hooks without an explicit user request; inspect the working tree first.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, command, sessions, events, tokens, example_session, example_step in con.execute(query).fetchall()
    ]
