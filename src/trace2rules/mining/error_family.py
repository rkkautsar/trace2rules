from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class ErrorFamilyConfig:
    scope: str  # repo_target | repo | cross_repo
    regex: str = ""
    min_sessions: int = 0
    min_repos: int = 0
    min_instances: int = 0
    action_filter: str = ""


@dataclass(frozen=True)
class ErrorFamilyHit:
    repo: str
    sessions: int
    events: int = 0
    tokens: int = 0
    target: str = ""
    error: str = ""
    family: str = ""
    repos: int = 0
    instances: int = 0
    repo_names: str = ""
    example_session: str = ""
    example_step: int = 0


def error_family_findings(
    con: duckdb.DuckDBPyConnection,
    config: ErrorFamilyConfig,
) -> list[ErrorFamilyHit]:
    if config.scope == "repo_target":
        rows = con.execute(
            f"""
            SELECT repo, target, count(DISTINCT session_id) AS sessions,
                   count(*) AS events, sum(approx_out_tokens) AS tokens,
                   arg_min(session_id, step_idx) AS example_session,
                   arg_min(step_idx, step_idx) AS example_step,
                   arg_min(error_message, step_idx) AS error
            FROM tool_events
            WHERE outcome = 'err'
              {config.action_filter}
              AND regexp_matches(coalesce(error_message, ''), ?)
            GROUP BY 1, 2
            HAVING count(DISTINCT session_id) >= ?
            ORDER BY sessions DESC, events DESC
            """,
            [config.regex, config.min_sessions],
        ).fetchall()
        return [
            ErrorFamilyHit(
                repo=repo,
                target=target or "",
                sessions=int(sessions),
                events=int(events),
                tokens=int(tokens or 0),
                error=error or "",
                example_session=example_session or "",
                example_step=int(example_step or 0),
            )
            for repo, target, sessions, events, tokens, example_session, example_step, error in rows
        ]

    if config.scope == "repo":
        rows = con.execute(
            """
            SELECT repo, count(*) AS events, count(DISTINCT session_id) AS sessions,
                   sum(approx_out_tokens) AS tokens, any_value(raw_target) AS exemplar
            FROM tool_events
            WHERE is_error AND action_type = 'exec'
              AND regexp_matches(
                  coalesce(error_message, '') || ' ' || coalesce(obs_cause, ''),
                  ?
              )
            GROUP BY 1
            ORDER BY sessions DESC, tokens DESC
            """,
            [config.regex],
        ).fetchall()
        return [
            ErrorFamilyHit(
                repo=repo,
                sessions=int(sessions),
                events=int(events),
                tokens=int(tokens or 0),
                target=exemplar or "",
            )
            for repo, events, sessions, tokens, exemplar in rows
        ]

    rows = con.execute(
        """
        SELECT e.verb || ':' || e.error_signature || ':' ||
               coalesce(nullif(e.error_detail, ''), nullif(e.obs_cause, ''), '<unknown>') AS family,
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
        HAVING count(DISTINCT s.repo) >= ?
           AND count(DISTINCT s.instance_id) >= ?
           AND count(DISTINCT e.session_id) >= ?
        ORDER BY repos DESC, instances DESC, tokens DESC
        """,
        [config.min_repos, config.min_instances, config.min_sessions],
    ).fetchall()
    return [
        ErrorFamilyHit(
            repo="*",
            family=family,
            repos=int(repos),
            instances=int(instances),
            sessions=int(sessions),
            tokens=int(tokens or 0),
            repo_names=repo_names or "",
            error=example or "",
        )
        for family, repos, instances, sessions, tokens, repo_names, example in rows
    ]
