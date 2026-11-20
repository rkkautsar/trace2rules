from __future__ import annotations

from dataclasses import dataclass

import duckdb

SOURCE_FILTER = """
    AND target NOT IN ('', '.')
    AND target NOT LIKE '<%'
    AND target NOT LIKE '% %'
    AND target NOT LIKE '%>%'
    AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')
"""


@dataclass(frozen=True)
class MissingPrecondition:
    repo: str
    sessions: int
    events: int
    tokens: int
    example_session: str
    example_step: int
    example_target: str = ""


@dataclass(frozen=True)
class PreconditionConfig:
    source_mode: str  # all | first | last
    before: str = ""
    after: str = ""
    min_sessions: int = 0
    token_mode: str = "source"  # source | session


def missing_precondition_findings(
    con: duckdb.DuckDBPyConnection,
    config: PreconditionConfig,
    *,
    source_filter: str = SOURCE_FILTER,
) -> list[MissingPrecondition]:
    ranked = ""
    selected = "source"
    if config.source_mode in {"first", "last"}:
        order = "asc" if config.source_mode == "first" else "desc"
        ranked = (
            ", row_number() OVER (PARTITION BY session_id "
            f"ORDER BY step_idx {order}) AS rn"
        )
        selected = "source WHERE rn = 1"
    columns = {
        row[1] for row in con.execute("PRAGMA table_info('tool_events')").fetchall()
    }
    target_expr = "target" if "target" in columns else "'' AS target"
    token_expr = (
        "sum(st.tokens)"
        if config.token_mode == "session"
        else "sum(s.approx_out_tokens)"
    )
    join_tokens = (
        "LEFT JOIN session_tokens st USING (session_id)"
        if config.token_mode == "session"
        else ""
    )
    session_tokens = (
        """
        session_tokens AS (
            SELECT session_id, sum(approx_out_tokens) AS tokens
            FROM tool_events GROUP BY 1
        ),
        """
        if config.token_mode == "session"
        else ""
    )
    rows = con.execute(
        f"""
        WITH {session_tokens}
        source AS (
            SELECT repo, session_id, step_idx, {target_expr}, approx_out_tokens {ranked}
            FROM tool_events
            WHERE action_type = 'edit'
              {source_filter}
        ),
        selected AS (SELECT * FROM {selected})
        SELECT s.repo, count(DISTINCT s.session_id) AS sessions,
               count(*) AS events,
               coalesce({token_expr}, 0) AS tokens,
               arg_min(s.session_id, s.step_idx) AS example_session,
               arg_min(s.step_idx, s.step_idx) AS example_step,
               arg_max(s.target, s.approx_out_tokens) AS example_target
        FROM selected s
        {join_tokens}
        WHERE TRUE
          {config.before}
          {config.after}
        GROUP BY s.repo
        HAVING count(DISTINCT s.session_id) >= ?
        ORDER BY sessions DESC
        """,
        [int(config.min_sessions)],
    ).fetchall()
    return [
        MissingPrecondition(
            repo=repo,
            sessions=int(sessions),
            events=int(events),
            tokens=int(tokens or 0),
            example_session=example_session,
            example_step=int(example_step or 0),
            example_target=example_target or "",
        )
        for repo, sessions, events, tokens, example_session, example_step, example_target in rows
    ]
