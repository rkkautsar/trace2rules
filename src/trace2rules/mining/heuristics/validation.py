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
from ..precondition import (
    PreconditionConfig,
    missing_precondition_findings,
)

def premature_completion(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    items = missing_precondition_findings(
        con,
        PreconditionConfig(
            source_mode="last",
            after="""
            AND NOT EXISTS (
                SELECT 1 FROM tool_events e
                WHERE e.session_id = s.session_id
                  AND e.step_idx > s.step_idx
                  AND e.action_type = 'exec'
                  AND e.outcome = 'ok'
            )""",
            token_mode="session",
        ),
        source_filter="",
    )
    findings = [
        Finding(
            kind="premature_completion",
            evidence_kind="failure",
            repo=item.repo,
            title="session ends after an edit without validation",
            sessions=item.sessions,
            tokens=item.tokens,
            detail=f"{item.sessions} sessions; no successful exec after last edit; ~{item.tokens:,} tokens",
            side="agent",
            recommendation="After the final edit, run the narrowest relevant test or lint command and confirm it passes before finishing.",
            window=_repo_window(
                con,
                item.repo,
                "action_type = 'edit' "
                "AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')",
            ),
        )
        for item in items
    ]
    return findings[:limit] if limit is not None else findings
def low_validation(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    items = missing_precondition_findings(
        con,
        PreconditionConfig(
            source_mode="first",
            after="""
            AND NOT EXISTS (
                SELECT 1 FROM tool_events e
                WHERE e.session_id = s.session_id
                  AND e.step_idx > s.step_idx
                  AND e.action_type = 'exec'
                  AND regexp_matches(e.raw_target, '(?i)(pytest|unittest|test|lint|typecheck|mypy|ruff|tox|vitest|jest|cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test)')
            )""",
            token_mode="session",
        ),
        source_filter="",
    )
    findings = [
        Finding(
            kind="low_validation",
            evidence_kind="failure",
            repo=item.repo,
            title="edits without a validation command",
            sessions=item.sessions,
            tokens=item.tokens,
            detail=(
                f"{item.sessions} sessions; no test/lint exec after first edit; "
                f"~{item.tokens:,} tokens"
            ),
            side="agent",
            recommendation="After changing code, run the narrowest test/lint command that exercises the change.",
            window=_repo_window(
                con,
                item.repo,
                "action_type = 'edit' "
                "AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')",
            ),
        )
        for item in items
    ]
    return findings[:limit] if limit is not None else findings
def false_completions(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH last_claim AS (
            SELECT session_id, repo, assistant_context
            FROM (
                SELECT session_id, repo, assistant_context,
                       row_number() OVER (PARTITION BY session_id ORDER BY step_idx DESC) AS rn
                FROM tool_events
                WHERE assistant_context <> ''
            ) ranked
            WHERE rn = 1
        ),
        last_edit AS (
            SELECT session_id, max(step_idx) AS step
            FROM tool_events
            WHERE action_type = 'edit'
            GROUP BY 1
        ),
        session_tokens AS (
            SELECT session_id, sum(approx_out_tokens) AS tokens
            FROM tool_events GROUP BY 1
        ),
        test_state AS (
            SELECT session_id,
                   max(step_idx) FILTER (WHERE is_test_command) AS last_step,
                   arg_max(outcome, step_idx) FILTER (WHERE is_test_command) AS last_outcome,
                   arg_max(exit_code, step_idx) FILTER (WHERE is_test_command) AS last_exit
            FROM (
                SELECT *,
                       regexp_matches(
                           raw_target,
                           '(?i)(pytest|unittest|tox|nox|npm (run )?test|yarn test|pnpm test|vitest|jest|'
                           'cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test|mvn test|gradle test)'
                       ) AS is_test_command
                FROM tool_events
                WHERE action_type = 'exec'
            ) tests
            GROUP BY 1
        )
        SELECT c.repo, count(*) AS sessions,
               coalesce(sum(t.tokens), 0) AS tokens,
               arg_max(c.assistant_context, t.tokens) AS example
        FROM last_claim c
        JOIN sessions s USING (session_id)
        JOIN last_edit e USING (session_id)
        LEFT JOIN test_state ts USING (session_id)
        LEFT JOIN session_tokens t USING (session_id)
        WHERE regexp_matches(
                  c.assistant_context,
                  '(?i)(all tests pass|tests pass|test suite passes|everything (passes|works)|'
                  'no failures|successfully (fixed|implemented|completed|verified)|'
                  'verified that|completed successfully)'
              )
          AND NOT regexp_matches(
                  c.assistant_context,
                  '(?i)(could not run|couldn''t run|unable to run|did not run|not run|'
                  'cannot run|can''t run|not verified|unverified|failing test)'
              )
          AND (
              ts.last_step IS NULL
              OR ts.last_step < e.step
              OR ts.last_outcome = 'err'
              OR coalesce(ts.last_exit, 0) <> 0
          )
        GROUP BY 1
        HAVING count(*) >= 2
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="false_completion",
            repo=repo,
            title="completion claim without green validation",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{sessions} sessions claim completion without a passing test after the last edit",
            cause=(example or "")[:240],
            side="agent",
            recommendation="Only claim completion after a passing validation command that covers the final edit; otherwise state what remains unverified.",
            window=_repo_window(
                con,
                repo,
                "action_type = 'edit' "
                "AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')",
            ),
        )
        for repo, sessions, tokens, example in con.execute(query).fetchall()
    ]

def no_reproduction(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    source_filter = """
        AND target NOT IN ('', '.')
        AND target NOT LIKE '<%'
        AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')
        AND NOT regexp_matches(
            target,
            '(?i)(^|/)(tests?|specs?|__tests__)(/|$)|(^|/)(test_|conftest\\.|.*_test\\.)|(^|/)(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)'
        )
    """
    items = missing_precondition_findings(
        con,
        PreconditionConfig(
            source_mode="first",
            before="""
            AND NOT EXISTS (
                SELECT 1 FROM tool_events e
                WHERE e.session_id = s.session_id
                  AND e.step_idx < s.step_idx
                  AND e.action_type = 'exec'
                  AND e.outcome = 'err'
            )""",
            min_sessions=3,
            token_mode="session",
        ),
        source_filter=source_filter,
    )
    findings = [
        Finding(
            kind="no_reproduction",
            evidence_kind="failure",
            repo=item.repo,
            title="source edits before any failing reproduction",
            sessions=item.sessions,
            tokens=item.tokens,
            detail=(
                f"{item.sessions} sessions edited source without a preceding "
                "failing exec"
            ),
            side="agent",
            recommendation="For a bug fix, reproduce the failure first with the narrowest command or test; only then edit the source.",
            window=_repo_window(
                con,
                item.repo,
                "action_type = 'edit' "
                "AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')",
            ),
        )
        for item in items
    ]
    return findings[:limit] if limit is not None else findings
def regressions_after_green(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT t.repo,
               count(DISTINCT t.session_id) AS sessions,
               count(*) AS regressions,
               sum(t.approx_out_tokens) AS tokens,
               arg_min(t.session_id, t.step_idx) AS example_session,
               arg_min(t.step_idx, t.step_idx) AS example_step,
               arg_max(t.command, t.step_idx) AS example_command
        FROM test_results t
        WHERE t.total > 0 AND t.failed > 0
          AND EXISTS (
              SELECT 1 FROM test_results p
              WHERE p.session_id = t.session_id
                AND p.step_idx < t.step_idx
                AND p.total > 0
                AND p.failed = 0
          )
          AND NOT EXISTS (
              SELECT 1 FROM test_results later
              WHERE later.session_id = t.session_id
                AND later.step_idx > t.step_idx
                AND later.total > 0
                AND later.failed = 0
          )
        GROUP BY 1
        HAVING count(DISTINCT t.session_id) >= 2
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="test_regression",
            repo=repo,
            title="test regression without a later green run",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{regressions} failing runs after a green baseline across {sessions} sessions; example {example_command[:100]}",
            side="agent",
            recommendation="After a green test run, rerun the affected test after every edit; do not finish on a previously green baseline.",
        )
        for repo, sessions, regressions, tokens, example_session, example_step, example_command in con.execute(query).fetchall()
    ]
