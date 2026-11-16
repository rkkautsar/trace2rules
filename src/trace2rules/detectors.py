from __future__ import annotations

from dataclasses import dataclass, field

import duckdb


@dataclass(frozen=True)
class Finding:
    kind: str
    repo: str
    title: str
    sessions: int
    tokens: int
    detail: str = ""
    sequence: str = ""
    variants: tuple[str, ...] = field(default_factory=tuple)
    lift: float = 0.0
    rework: int = 0
    setup_tax: int = 0
    cost: int = 0
    paired_instances: int = 0
    delta_rework: float = 0.0
    delta_setup: float = 0.0

    @property
    def evidence(self) -> str:
        return f"{self.detail}; ~{self.tokens:,} tokens"


def session_summary(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT s.repo,
               count(*) AS sessions,
               sum(CASE WHEN s.resolved THEN 1 ELSE 0 END) AS resolved,
               count(*) FILTER (WHERE s.source NOT IN ('swe-chat', 'agent-trajectory')) AS known_resolution,
               (SELECT count(*) FROM tool_events e WHERE e.repo = s.repo) AS events,
               (SELECT sum(approx_out_tokens) FROM tool_events e WHERE e.repo = s.repo) AS tokens
        FROM sessions s
        GROUP BY s.repo
        ORDER BY sessions DESC
        """
    ).fetchall()
    return [
        {
            "repo": r[0],
            "sessions": r[1],
            "resolved": r[2],
            "known_resolution": r[3],
            "events": r[4] or 0,
            "tokens": r[5] or 0,
        }
        for r in rows
    ]


def hotspots(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH per_session AS (
            SELECT repo, target, session_id,
                   sum(approx_out_tokens) FILTER (WHERE action_type IN ('read', 'search')) AS read_tokens,
                   bool_or(action_type = 'edit') AS was_edited
            FROM tool_events
            WHERE target NOT IN ('', '.')
            GROUP BY repo, target, session_id
        ),
        agg AS (
            SELECT repo, target,
                   count(*) AS sessions,
                   sum(CASE WHEN NOT was_edited THEN coalesce(read_tokens, 0) ELSE 0 END) AS wasted_tokens
            FROM per_session
            WHERE read_tokens IS NOT NULL
            GROUP BY repo, target
        )
        SELECT a.repo, a.target, a.sessions, s.n AS repo_sessions, a.wasted_tokens
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
            detail=f"read-only in {sessions}/{repo_sessions} sessions ({sessions / repo_sessions:.0%})",
        )
        for repo, target, sessions, repo_sessions, wasted in con.execute(query).fetchall()
    ]


def recovery_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT a.repo, a.target AS failed, b.target AS recovery,
               count(DISTINCT a.session_id) AS sessions,
               count(*) AS occurrences,
               sum(a.approx_out_tokens) AS wasted_tokens
        FROM tool_events a
        JOIN tool_events b
          ON a.session_id = b.session_id
         AND b.step_idx > a.step_idx
         AND b.step_idx - a.step_idx <= 6
         AND a.target <> b.target
        WHERE a.is_error AND NOT b.is_error
          AND a.action_type = 'exec' AND b.action_type = 'exec'
          AND a.target <> '' AND b.target <> ''
        GROUP BY 1, 2, 3
        HAVING count(DISTINCT a.session_id) >= 2
        ORDER BY sessions DESC, wasted_tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="recovery",
            repo=repo,
            title=f"{failed}  ->  {recovery}",
            sessions=sessions,
            tokens=int(wasted or 0),
            detail=f"{occurrences} recurrences across {sessions} sessions",
        )
        for repo, failed, recovery, sessions, occurrences, wasted in con.execute(query).fetchall()
    ]


def context_blasts(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, target, tool_name, action_type,
               count(DISTINCT session_id) AS sessions,
               count(*) AS calls,
               avg(approx_out_tokens) AS avg_tokens,
               sum(approx_out_tokens) AS total_tokens
        FROM tool_events
        WHERE action_type IN ('exec', 'read')
          AND target <> ''
        GROUP BY 1, 2, 3, 4
        HAVING avg(approx_out_tokens) >= 1500 AND count(DISTINCT session_id) >= 2
        ORDER BY total_tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="context",
            repo=repo,
            title=target,
            sessions=sessions,
            tokens=int(total or 0),
            detail=f"{action_type} via {tool_name}: avg {avg:.0f} tokens over {calls} calls",
        )
        for repo, target, tool_name, action_type, sessions, calls, avg, total in con.execute(query).fetchall()
    ]


def all_findings(con: duckdb.DuckDBPyConnection, limit: int = 25) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(hotspots(con, limit=limit))
    findings.extend(recovery_loops(con, limit=limit))
    findings.extend(context_blasts(con, limit=limit))
    return findings
