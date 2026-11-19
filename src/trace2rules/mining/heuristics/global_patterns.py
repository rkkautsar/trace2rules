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

def global_patterns(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    query = """
        SELECT e.verb || ':' || e.error_signature || ':' || coalesce(nullif(e.error_detail, ''), nullif(e.obs_cause, ''), '<unknown>') AS family,
               count(DISTINCT s.repo) AS repos,
               count(DISTINCT s.instance_id) AS instances,
               count(DISTINCT e.session_id) AS sessions,
               sum(e.approx_out_tokens) AS tokens,
               string_agg(DISTINCT s.repo, ', ') AS repo_names,
               arg_min(coalesce(nullif(e.error_detail, ''), nullif(e.obs_cause, ''), e.error_signature), e.step_idx) AS example
        FROM tool_events e
        JOIN sessions s USING (session_id)
        WHERE e.is_error AND e.action_type = 'exec'
          AND s.repo NOT IN ('', 'unknown')
          AND e.error_signature NOT LIKE 'exit:%'
          AND e.error_signature NOT IN ('text_error', 'timeout', 'err:unknown')
        GROUP BY 1
        HAVING count(DISTINCT s.repo) >= 2
           AND count(DISTINCT s.instance_id) >= 3
           AND count(DISTINCT e.session_id) >= 4
        ORDER BY repos DESC, instances DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="global_failure",
            repo="*",
            title=family,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{instances} instances across {repos} repos",
            cause=(example or "")[:240],
            side="agent",
            failure_family=family,
            variants=tuple((repo_names or "").split(", ")),
        )
        for family, repos, instances, sessions, tokens, repo_names, example in con.execute(query).fetchall()
    ]
