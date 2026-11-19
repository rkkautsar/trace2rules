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

def selector_misses(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, count(*) AS events, count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens, any_value(raw_target) AS exemplar
        FROM tool_events
        WHERE is_error AND action_type = 'exec'
          AND regexp_matches(coalesce(error_message, '') || ' ' || coalesce(obs_cause, ''),
                             '(?i)deselected|no tests ran|collected 0 items')
        GROUP BY 1
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="selector_miss",
            repo=repo,
            title=exemplar or "test selector",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{events} selector misses in {sessions} sessions",
            side="agent",
            recommendation="When a selector reports 0 selected/deselected, inspect the runner's collection output and use the printed node ID; selectors match test names, not arbitrary file text.",
            window=_repo_window(
                con,
                repo,
                "is_error AND action_type = 'exec' AND regexp_matches(coalesce(error_message, '') || ' ' || coalesce(obs_cause, ''), '(?i)deselected|no tests ran|collected 0 items')",
            ),
        )
        for repo, events, sessions, tokens, exemplar in con.execute(query).fetchall()
    ]

def workaround_scripts(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, count(*) AS sessions, sum(edits) AS edits, sum(runs) AS runs,
               sum(tokens) AS tokens, sum(failures) AS failures,
               any_value(exemplar) AS exemplar
        FROM (
            SELECT session_id, repo,
                   any_value(object_id) FILTER (WHERE action_type = 'edit' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS exemplar,
                   count(*) FILTER (WHERE action_type = 'edit' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS edits,
                   count(*) FILTER (WHERE action_type = 'exec' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS runs,
                   sum(approx_out_tokens) FILTER (WHERE action_type = 'exec' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS tokens,
                   count(*) FILTER (WHERE outcome = 'err' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS failures
            FROM tool_events
            GROUP BY 1, 2
        ) s
        WHERE edits > 0
        GROUP BY 1
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="workaround_script",
            repo=repo,
            title=exemplar or "root-level throwaway reproducer scripts",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits, {runs} runs, {failures} failures",
            side="agent",
            recommendation="Write at most one scratch reproducer, run it once, then delete it; use the repository's narrowest existing test selector.",
            window=_repo_window(
                con,
                repo,
                "action_type = 'exec' AND regexp_matches(object_id, '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)') AND object_id = ?",
                (exemplar or "",),
            ),
        )
        for repo, sessions, edits, runs, tokens, failures, exemplar in con.execute(query).fetchall()
    ]
