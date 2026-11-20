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

def hotspots(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH per_session AS (
            SELECT repo, target, session_id,
                   sum(approx_out_tokens) FILTER (WHERE action_type IN ('read', 'search')) AS read_tokens,
                   bool_or(action_type = 'edit') AS was_edited
            FROM tool_events
            WHERE target NOT IN ('', '.')
              AND target NOT LIKE '<%'
              AND target NOT LIKE '% %'
              AND target NOT LIKE '%>%'
              AND target NOT LIKE '%;'
              AND target NOT LIKE 'skill:%'
              AND target NOT LIKE 'xd:%'
              AND NOT regexp_matches(
                  target,
                  '(?i)(^|/)(readme|docs?|changelog|contributing|license)([.]|/|$)'
              )
            GROUP BY repo, target, session_id
        ),
        agg AS (
            SELECT repo, target,
                   count(*) AS sessions,
                   count(*) FILTER (WHERE NOT was_edited) AS read_only_sessions,
                   sum(CASE WHEN NOT was_edited THEN coalesce(read_tokens, 0) ELSE 0 END) AS wasted_tokens
            FROM per_session
            WHERE read_tokens IS NOT NULL
            GROUP BY repo, target
        )
        SELECT a.repo, a.target, a.sessions, a.read_only_sessions,
               s.n AS repo_sessions, a.wasted_tokens
        FROM agg a
        JOIN (SELECT repo, count(*) AS n FROM sessions GROUP BY repo) s USING (repo)
        WHERE a.sessions >= 3 AND a.sessions >= 0.25 * s.n
        ORDER BY a.wasted_tokens DESC, a.sessions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="hotspot",
            repo=repo,
            title=target,
            sessions=sessions,
            tokens=int(wasted or 0),
            detail=f"read-only in {read_only}/{repo_sessions} sessions ({read_only / repo_sessions:.0%})",
            edit_sessions=sessions - read_only,
        )
        for repo, target, sessions, read_only, repo_sessions, wasted in con.execute(query).fetchall()
    ]

CONTEXT_BLAST_QUERY = """
        SELECT repo, target, tool_name, action_type,
               count(DISTINCT session_id) AS sessions,
               count(*) AS calls,
               avg(approx_out_tokens) AS avg_tokens,
               sum(approx_out_tokens) AS total_tokens,
               arg_max(session_id, approx_out_tokens) AS example_session,
               arg_max(step_idx, approx_out_tokens) AS example_step
        FROM tool_events
        WHERE action_type IN ('exec', 'read', 'search')
          AND target <> ''
          AND target NOT LIKE '<%'
          AND target NOT LIKE '%>%'
          AND target NOT LIKE '%;'
          AND target NOT LIKE 'skill:%'
          AND target NOT LIKE 'xd:%'
          AND NOT regexp_matches(
              target,
              '(?i)(^|/)(readme|docs?|changelog|contributing|license)([.]|/|$)'
          )
        GROUP BY 1, 2, 3, 4
        HAVING avg(approx_out_tokens) >= 1500 AND count(DISTINCT session_id) >= 2
           AND count(*) >= 2
        ORDER BY total_tokens DESC
    """


def context_blast_targets(con: duckdb.DuckDBPyConnection) -> set[tuple[str, str]]:
    return {
        (repo, target)
        for repo, target, _tool, _action, _sessions, _calls, _avg, _total, _session, _step in con.execute(
            CONTEXT_BLAST_QUERY
        ).fetchall()
        if not _DOC_TARGET_RE.search(target) and not _BOUNDED_RE.search(target)
    }


def context_blasts(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = CONTEXT_BLAST_QUERY
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, tool_name, action_type, sessions, calls, avg, total, example_session, example_step in con.execute(query).fetchall():
        if _DOC_TARGET_RE.search(target) or _BOUNDED_RE.search(target):
            continue
        findings.append(
            Finding(
                kind="context",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(total or 0),
                detail=f"{action_type} via {tool_name}: avg {avg:.0f} tokens over {calls} calls",
                side=_context_advice(target, action_type)[0],
                recommendation=_context_advice(target, action_type)[1],
                window=_representative_window(con, example_session, example_step),
            )
        )
    return findings

def orientation(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, target, count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens
        FROM tool_events
        WHERE action_type IN ('read', 'search')
          AND target <> '' AND target NOT LIKE '<%'
        GROUP BY repo, target
        ORDER BY tokens DESC
    """
    findings = []
    for repo, target, sessions, tokens in con.execute(query).fetchall():
        if not _is_orientation(target):
            continue
        findings.append(
            Finding(
                kind="orientation",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"orientation read in {sessions} sessions",
                side="repo",
            )
        )
    return findings[:limit] if limit is not None else findings
