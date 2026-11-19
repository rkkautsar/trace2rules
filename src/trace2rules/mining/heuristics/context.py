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
from ...audit.enforcement import enforcement_inventory


def prompt_context(
    con: duckdb.DuckDBPyConnection, repo: str, root=None
) -> dict:
    """Evidence blocks that are too specific to be generic findings."""
    pairs = con.execute(
        """
        SELECT a.session_id, a.step_idx, b.step_idx, a.raw_target, b.raw_target,
               a.error_message, s.source_ref
        FROM tool_events a
        JOIN sessions s ON s.session_id = a.session_id
        JOIN tool_events b
          ON b.session_id = a.session_id
         AND b.step_idx > a.step_idx AND b.step_idx - a.step_idx <= 10
         AND b.action_type = 'exec' AND NOT b.is_error
        WHERE a.repo = ? AND a.action_type = 'exec' AND a.is_error
          AND a.raw_target <> b.raw_target
          AND regexp_matches(
              b.raw_target,
              '(?i)(cargo|go (test|build)|npm|pnpm|yarn|pytest|unittest|mvn|gradle|dotnet|make|tsc|eslint|ruff)'
          )
          AND regexp_matches(
              coalesce(a.error_message, '') || ' ' || coalesce(a.obs_cause, ''),
              '(?i)(failed to download|syncing channel|could not compile|command not found|no such file|not found)'
          )
        """,
        [repo],
    ).fetchall()
    env_re = r'''(?:^|&&|;|\|\|)\s*([A-Z][A-Z0-9_]*=(?:"[^"]*"|'[^']*'|\S+))'''
    bootstrap_map: dict[tuple[str, ...], dict] = {}
    nearest: dict[tuple[str, str], tuple[int, int, str, str]] = {}
    for session_id, failed_step, succeeded_step, failed, succeeded, failed_error, source_ref in pairs:
        key = (session_id, succeeded)
        previous = nearest.get(key)
        action_match = re.search(
            r"(?i)\b(cargo|go|npm|pnpm|yarn|pytest|unittest|mvn|gradle|dotnet|make|tsc|eslint|ruff)\s+([a-z-]+)",
            succeeded,
        )
        action = action_match.group(0).lower() if action_match else ""
        priority = 0
        if action and action in failed.lower():
            priority = 2
        elif re.search(r"(?i)(test|build|check|run|compile)", failed):
            priority = 1
        previous_priority = 0
        if previous:
            if previous[2] and action and action in previous[2].lower():
                previous_priority = 2
            elif previous[2] and re.search(r"(?i)(test|build|check|run|compile)", previous[2]):
                previous_priority = 1
        if (
            previous is None
            or (priority, int(failed_step)) > (previous_priority, previous[0])
        ):
            nearest[key] = (
                int(failed_step),
                int(succeeded_step),
                failed,
                (failed_error or "")[:300],
                source_ref,
            )
    for (session_id, succeeded), (
        failed_step,
        succeeded_step,
        failed,
        failed_error,
        source_ref,
    ) in nearest.items():
        failed_env = set(re.findall(env_re, failed))
        succeeded_env = set(re.findall(env_re, succeeded))
        delta = tuple(sorted(succeeded_env - failed_env))
        if not delta:
            continue
        item = bootstrap_map.setdefault(
            delta,
            {
                "failed": failed,
                "failed_error": failed_error,
                "succeeded": succeeded,
                "sessions": set(),
                "source": source_ref,
                "steps": [],
            },
        )
        item["sessions"].add(session_id)
        item["steps"].append(max(1, succeeded_step - failed_step))
    bootstrap = [
        (
            list(delta),
            item["failed"],
            item["failed_error"],
            item["succeeded"],
            len(item["sessions"]),
            item["source"],
            sum(item["steps"]) / len(item["steps"]),
        )
        for delta, item in sorted(
            bootstrap_map.items(), key=lambda pair: len(pair[1]["sessions"]), reverse=True
        )[:5]
    ]
    validation = con.execute(
        """
        WITH first_edit AS (
            SELECT session_id, min(step_idx) AS step
            FROM tool_events WHERE repo = ? AND action_type = 'edit' GROUP BY 1
        )
        SELECT e.raw_target, count(*) AS calls, count(DISTINCT e.session_id) AS sessions,
               sum(e.approx_out_tokens) AS tokens,
               arg_min(s.source_ref, e.step_idx) AS source
        FROM tool_events e JOIN first_edit f USING (session_id)
        JOIN sessions s ON s.session_id = e.session_id
        WHERE e.repo = ? AND e.action_type = 'exec' AND e.outcome = 'ok'
          AND e.step_idx > f.step
          AND regexp_matches(e.raw_target, '(?i)(cargo test|go test|pytest|unittest|npm test|yarn test|pnpm test|mvn test|gradle test|dotnet test|rspec|phpunit|mix test|swift test)')
        GROUP BY 1 ORDER BY sessions DESC, tokens DESC LIMIT 5
        """,
        [repo, repo],
    ).fetchall()
    reads = con.execute(
        """
        SELECT target, count(DISTINCT session_id) AS sessions, count(*) AS calls,
               sum(approx_out_tokens) AS tokens
        FROM tool_events
        WHERE repo = ? AND action_type = 'read' AND target NOT LIKE '<%'
        GROUP BY 1 HAVING count(*) >= 3
        ORDER BY tokens DESC LIMIT 5
        """,
        [repo],
    ).fetchall()
    outcomes = con.execute(
        """
        SELECT count(DISTINCT s.session_id),
               count(DISTINCT e.session_id) FILTER (WHERE e.action_type = 'edit')
        FROM sessions s LEFT JOIN tool_events e USING (session_id)
        WHERE s.repo = ?
        """,
        [repo],
    ).fetchone()
    validation_summary = con.execute(
        """
        WITH last_edit AS (
            SELECT session_id, max(step_idx) AS step
            FROM tool_events WHERE repo = ? AND action_type = 'edit' GROUP BY 1
        )
        SELECT count(*),
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM tool_events e
                   WHERE e.session_id = l.session_id
                     AND e.step_idx > l.step
                     AND e.action_type = 'exec'
                     AND e.outcome = 'ok'
                     AND regexp_matches(
                         e.raw_target,
                         '(?i)(pytest|unittest|test|lint|typecheck|mypy|ruff|tox|vitest|jest|cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test)'
                     )
               ))
        FROM last_edit l
        """,
        [repo],
    ).fetchone()
    return {
        "bootstrap": [
            {
                "delta": delta,
                "failed": failed,
                "failed_error": failed_error,
                "succeeded": succeeded,
                "sessions": sessions,
                "source": source_ref,
                "mean_steps": round(float(steps or 0), 1),
            }
            for delta, failed, failed_error, succeeded, sessions, source_ref, steps in bootstrap
        ],
        "validation": [
            {
                "command": command.splitlines()[0][:300],
                "calls": calls,
                "sessions": sessions,
                "tokens": tokens,
                "source": source,
            }
            for command, calls, sessions, tokens, source in validation
        ],
        "reads": [
            {"target": target, "sessions": sessions, "calls": calls, "tokens": tokens}
            for target, sessions, calls, tokens in reads
        ],
        "enforcement": enforcement_inventory(root),
        "outcomes": {
            "sessions": outcomes[0],
            "with_edit": outcomes[1],
            "validated_sessions": validation_summary[1] if validation_summary else 0,
            "unvalidated_sessions": (validation_summary[0] - validation_summary[1]) if validation_summary else 0,
            "validation_coverage": validation_summary[0] if validation_summary else 0,
        },
    }
