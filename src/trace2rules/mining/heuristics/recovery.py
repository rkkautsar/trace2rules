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

def recovery_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT a.repo, a.target AS failed, a.error_signature, b.target AS recovery,
               count(DISTINCT a.session_id) AS sessions,
               count(DISTINCT a.event_id) AS occurrences,
               sum(a.approx_out_tokens) AS wasted_tokens,
               arg_min(a.session_id, b.step_idx - a.step_idx) AS example_session,
               arg_min(a.step_idx, b.step_idx - a.step_idx) AS example_step,
               arg_min(a.error_detail, b.step_idx - a.step_idx) AS example_detail,
               arg_min(a.obs_cause, b.step_idx - a.step_idx) AS example_cause,
               arg_min(b.step_idx, b.step_idx - a.step_idx) AS example_recovery_step,
               regexp_replace(
                   coalesce(nullif(a.error_detail, ''), nullif(a.obs_cause, ''), a.error_signature),
                   '(?:/workspace|/testbed|/repo|/app)/[^ ]+', '<workspace>', 'g'
               ) AS failure_family
        FROM tool_events a
        JOIN tool_events b
          ON a.session_id = b.session_id
         AND b.step_idx > a.step_idx
         AND b.step_idx - a.step_idx <= 6
        WHERE a.is_error AND NOT b.is_error
          AND a.action_type = 'exec' AND b.action_type = 'exec'
          AND a.target <> '' AND b.target <> ''
          AND a.segment_idx = b.segment_idx
          AND coalesce(b.exit_code, 0) = 0
          AND NOT regexp_matches(
              coalesce(b.obs_cause, '') || coalesce(b.obs_excerpt, ''),
              '(?i)(Traceback|FAILED|doesn''t work)'
          )
          AND (
              (a.object_id <> '' AND a.object_id = b.object_id)
              OR (a.object_id = '' AND b.object_id = '' AND a.verb = b.verb)
              OR EXISTS (
                  SELECT 1 FROM tool_events e
                  WHERE e.session_id = a.session_id
                    AND e.step_idx BETWEEN a.step_idx AND b.step_idx
                    AND e.action_type = 'edit' AND e.object_id = a.object_id
              )
          )
        GROUP BY 1, 2, 3, 4, 13
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
            detail=f"{occurrences} recurrences across {sessions} sessions; signature {signature}",
            cause=(example_detail or example_cause or signature)[:240],
            failure_family=failure_family,
            cause_support_sessions=int(sessions),
            window=_representative_window(
                con, example_session, example_step, example_recovery_step
            ),
        )
        for repo, failed, signature, recovery, sessions, occurrences, wasted, example_session, example_step, example_detail, example_cause, example_recovery_step, failure_family in con.execute(query).fetchall()
    ]

def failure_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH failures AS (
            SELECT session_id, repo, error_signature,
                   coalesce(nullif(error_site, ''), nullif(object_id, ''), error_signature) AS site,
                   step_idx, approx_out_tokens,
                   coalesce(nullif(error_detail, ''), nullif(obs_cause, ''), error_signature) AS cause
            FROM tool_events
            WHERE is_error AND action_type = 'exec'
              AND error_signature NOT LIKE 'exit:%'
              AND error_signature NOT IN ('text_error', 'timeout', 'err:unknown')
              AND error_signature NOT LIKE 'exception:ModuleNotFoundError'
              AND error_signature NOT LIKE 'exception:ImportError'
        ),
        per_session AS (
            SELECT repo, session_id, error_signature, site,
                   count(*) AS failures,
                   sum(approx_out_tokens) AS tokens,
                   min(step_idx) AS first_step,
                   max(step_idx) AS last_step,
                   arg_max(cause, step_idx) AS cause
            FROM failures
            GROUP BY 1, 2, 3, 4
            HAVING count(*) >= 3
        ),
        with_edits AS (
            SELECT p.*,
                   (
                       SELECT count(*)
                       FROM tool_events e
                       WHERE e.session_id = p.session_id
                         AND e.action_type = 'edit'
                         AND e.step_idx BETWEEN p.first_step AND p.last_step
                   ) AS edits
            FROM per_session p
        )
        SELECT repo, error_signature, site,
               count(*) AS sessions,
               sum(failures) AS occurrences,
               sum(tokens) AS tokens,
               sum(edits) AS edits,
               arg_max(session_id, failures) AS example_session,
               arg_max(first_step, failures) AS example_step,
               arg_max(cause, failures) AS cause
        FROM with_edits
        WHERE edits >= 2
        GROUP BY 1, 2, 3
        HAVING count(*) >= 2
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="failure_loop",
            repo=repo,
            title=f"{signature} at {site}",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{occurrences} repeats with {edits} intervening edits in {sessions} sessions",
            cause=(cause or "")[:240],
            failure_family=signature,
            side="agent",
            recommendation=(
                "The same failure survived multiple edits. Stop patching; read the full traceback or failing assertion, "
                "identify the root cause, and change the test or input before the next edit."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, signature, site, sessions, occurrences, tokens, edits, example_session, example_step, cause in con.execute(query).fetchall()
    ]

def noop_retries(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH x AS (
            SELECT *,
                   lead(raw_target) OVER (PARTITION BY session_id ORDER BY step_idx) AS next_target,
                   lead(action_type) OVER (PARTITION BY session_id ORDER BY step_idx) AS next_action
            FROM tool_events
        )
        SELECT repo, count(*) AS occurrences, count(DISTINCT session_id) AS sessions,
               any_value(raw_target) AS exemplar, sum(approx_out_tokens) AS tokens
        FROM x
        WHERE is_error AND next_action = 'exec' AND next_target = raw_target
        GROUP BY 1
        ORDER BY sessions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="noop_retry",
            repo=repo,
            title=exemplar or "repeated failing command",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{occurrences} unchanged retries; ~{int(tokens or 0):,} tokens",
            side="agent",
            recommendation="A failing command repeated unchanged will fail again; change the selector/input or inspect state before rerunning.",
            window=_repo_window(con, repo, "is_error AND raw_target = ?", (exemplar or "",)),
        )
        for repo, occurrences, sessions, exemplar, tokens in con.execute(query).fetchall()
    ]
