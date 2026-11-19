from __future__ import annotations

import re

import duckdb

from .detectors_core import (
    Finding,
    _BOUNDED_RE,
    _DOC_TARGET_RE,
    _context_advice,
    _is_orientation,
    _representative_window,
    _repo_window,
    session_summary,
)
from .session_signals import (
    command_errors,
    edit_churn,
    feedback_loops,
    permission_denials,
    repeated_reads,
)

_TEST_OR_SCRATCH_RE = re.compile(
    r"(?i)(^|/)(tests?|specs?|__tests__)(/|$)"
    r"|(^|/)(test_|conftest\.|.*_test\.|.*\.spec\.)"
    r"|(^|/)(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)"
    r"|/(site-packages|dist-packages)/"
)


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
                    AND e.action_type = 'edit' AND e.target = a.object_id
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


def stuck_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, target, count(*) AS edits, count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens, any_value(error_family) AS family
        FROM tool_events
        WHERE action_type = 'edit' AND target <> '' AND target NOT LIKE '<%'
        GROUP BY 1, 2
        HAVING count(*) >= 3 AND count(DISTINCT session_id) >= 2
        ORDER BY edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="stuck_loop",
            repo=repo,
            title=object_id,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits across {sessions} sessions; recurring family {family or 'unknown'}",
            side="agent",
            recommendation="After two edits leave the same failure family unchanged, stop editing and read the traceback site or failing test before another patch.",
            window=_repo_window(con, repo, "action_type = 'edit' AND object_id = ?", (object_id,)),
        )
        for repo, object_id, edits, sessions, tokens, family in con.execute(query).fetchall()
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


def premature_completion(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH last_edit AS (
            SELECT session_id, max(step_idx) AS step
            FROM tool_events WHERE action_type = 'edit' GROUP BY 1
        ),
        session_tokens AS (
            SELECT session_id, sum(approx_out_tokens) AS tokens
            FROM tool_events GROUP BY 1
        )
        SELECT s.repo, count(*) AS sessions,
               coalesce(sum(t.tokens), 0) AS tokens
        FROM sessions s
        JOIN last_edit l USING (session_id)
        LEFT JOIN session_tokens t USING (session_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM tool_events e
            WHERE e.session_id = s.session_id
              AND e.step_idx > l.step
              AND e.action_type = 'exec'
              AND e.outcome = 'ok'
        )
        GROUP BY 1 ORDER BY sessions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="premature_completion",
            repo=repo,
            title="session ends after an edit without validation",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{sessions} sessions; no successful exec after last edit; ~{int(tokens or 0):,} tokens",
            side="agent",
            recommendation="After the final edit, run the narrowest relevant test or lint command and confirm it passes before finishing.",
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, tokens in con.execute(query).fetchall()
    ]


def low_validation(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH first_edit AS (
            SELECT session_id, min(step_idx) AS step
            FROM tool_events WHERE action_type = 'edit' GROUP BY 1
        ),
        session_tokens AS (
            SELECT session_id, sum(approx_out_tokens) AS tokens
            FROM tool_events GROUP BY 1
        )
        SELECT s.repo, count(*) AS sessions,
               coalesce(sum(t.tokens), 0) AS tokens
        FROM sessions s
        JOIN first_edit f USING (session_id)
        LEFT JOIN session_tokens t USING (session_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM tool_events e
            WHERE e.session_id = s.session_id
              AND e.step_idx > f.step
              AND e.action_type = 'exec'
              AND regexp_matches(e.raw_target, '(?i)(pytest|unittest|test|lint|typecheck|mypy|ruff|tox|vitest|jest|cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test)')
        )
        GROUP BY 1 ORDER BY sessions DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="low_validation",
            repo=repo,
            title="edits without a validation command",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{sessions} sessions; no test/lint exec after first edit; ~{int(tokens or 0):,} tokens",
            side="agent",
            recommendation="After changing code, run the narrowest test/lint command that exercises the change.",
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, tokens in con.execute(query).fetchall()
    ]


def site_edit_mismatch(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    rows = con.execute(
        """
        SELECT repo, session_id, step_idx, error_site, approx_out_tokens,
               next_action, next_object
        FROM (
            SELECT e.repo, e.session_id, e.step_idx, e.error_site, e.approx_out_tokens,
                   lead(e.action_type) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_action,
                   lead(e.object_id) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_object
            FROM tool_events e
        ) e
        WHERE error_site <> ''
        """
    ).fetchall()
    counts: dict[str, int] = {}
    sessions_by_repo: dict[str, set[str]] = {}
    tokens_by_repo: dict[str, int] = {}
    examples: dict[str, str] = {}
    for repo, session, step, site, tokens, next_action, next_object in rows:
        if next_action != "edit" or not next_object:
            continue
        site_file = site.rsplit(":", 1)[0]
        if _TEST_OR_SCRATCH_RE.search(site_file):
            continue
        if site_file.rsplit("/", 1)[-1] == next_object.rsplit("/", 1)[-1]:
            continue
        counts[repo] = counts.get(repo, 0) + 1
        sessions_by_repo.setdefault(repo, set()).add(session)
        tokens_by_repo[repo] = tokens_by_repo.get(repo, 0) + int(tokens or 0)
        examples.setdefault(repo, f"{site} -> {next_object}")
    findings = [
        Finding(
            kind="site_edit_mismatch",
            repo=repo,
            title=examples.get(repo, "traceback site differs from edited file"),
            sessions=len(sessions_by_repo.get(repo, set())),
            tokens=tokens_by_repo.get(repo, 0),
            detail=(
                f"{counts[repo]} traceback sites in "
                f"{len(sessions_by_repo.get(repo, set()))} sessions followed by an edit to a different file"
            ),
            side="agent",
            recommendation="Edit the file named by the traceback or failing assertion; do not patch a different layer or the reproducer.",
        )
        for repo in sorted(
            counts,
            key=lambda key: (len(sessions_by_repo.get(key, set())), tokens_by_repo.get(key, 0)),
            reverse=True,
        )
    ]
    return findings[:limit] if limit is not None else findings


def prompt_context(con: duckdb.DuckDBPyConnection, repo: str) -> dict:
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
        "outcomes": {
            "sessions": outcomes[0],
            "with_edit": outcomes[1],
            "validated_sessions": validation_summary[1] if validation_summary else 0,
            "unvalidated_sessions": (validation_summary[0] - validation_summary[1]) if validation_summary else 0,
            "validation_coverage": validation_summary[0] if validation_summary else 0,
        },
    }


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


def edit_rejections(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, target,
               count(DISTINCT session_id) AS sessions,
               count(*) AS rejects,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step,
               arg_min(coalesce(nullif(error_message, ''), obs_cause), step_idx) AS cause
        FROM tool_events
        WHERE action_type = 'edit' AND outcome = 'err'
          AND target NOT IN ('', '.')
          AND target NOT LIKE '<%'
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, rejects DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="edit_rejection",
            repo=repo,
            title=target,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{rejects} rejected edits across {sessions} sessions",
            cause=(cause or "")[:240],
            side="agent",
            recommendation="After an edit is rejected, re-read the target and recompute the anchor; do not retry the same stale patch.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, target, sessions, rejects, tokens, example_session, example_step, cause in con.execute(query).fetchall()
    ]


def blind_edits(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH edits AS (
            SELECT session_id, repo, step_idx, target, approx_out_tokens
            FROM tool_events
            WHERE action_type = 'edit'
              AND target NOT IN ('', '.')
              AND target NOT LIKE '<%'
              AND target NOT LIKE '% %'
              AND target NOT LIKE '%>%'
              AND raw_target NOT LIKE 'create%'
        )
        SELECT repo,
               count(*) AS edits,
               count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens,
               arg_max(target, approx_out_tokens) AS example_target,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM edits e
        WHERE NOT EXISTS (
            SELECT 1 FROM tool_events r
            WHERE r.session_id = e.session_id
              AND r.action_type = 'read'
              AND r.target = e.target
              AND r.step_idx < e.step_idx
        )
        AND NOT EXISTS (
            SELECT 1 FROM tool_events c
            WHERE c.session_id = e.session_id
              AND c.action_type = 'edit'
              AND c.raw_target LIKE 'create%'
              AND c.target = e.target
              AND c.step_idx < e.step_idx
        )
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="blind_edit",
            repo=repo,
            title="edits without a preceding read",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits with no prior read of the target; example {example_target[:120]}",
            side="agent",
            recommendation="Read the target before editing it. For a new file, state that it is new and avoid patching unseen existing content.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, edits, sessions, tokens, example_target, example_session, example_step in con.execute(query).fetchall()
    ]


def environment_thrash(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH installs AS (
            SELECT session_id, repo, step_idx, approx_out_tokens, is_error,
                   coalesce(nullif(raw_target, ''), target) AS command
            FROM tool_events
            WHERE action_type = 'exec'
              AND regexp_matches(
                  coalesce(raw_target, target),
                  '(?i)\b(pip3?|uv|poetry|conda|npm|yarn|pnpm|cargo|go|bundle|composer|dotnet|mix)\s+(install|add|sync|update|remove|uninstall|upgrade|lock|get|ci)\b'
              )
        )
        SELECT repo,
               count(*) AS installs,
               count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens,
               count(*) FILTER (WHERE is_error) AS failures,
               arg_min(command, step_idx) AS example,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM installs
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2 AND count(*) >= 5
        ORDER BY installs DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="environment_thrash",
            repo=repo,
            title="repeated dependency/environment setup",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{installs} package-manager calls across {sessions} sessions; {failures} failed; example {example[:120]}",
            side="repo",
            recommendation="Prebuild or document the environment. Do not make every session rediscover and reinstall the same tooling.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, installs, sessions, tokens, failures, example, example_session, example_step in con.execute(query).fetchall()
    ]


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


def no_reproduction(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH first_edit AS (
            SELECT session_id, repo, min(step_idx) AS step
            FROM tool_events
            WHERE action_type = 'edit'
              AND target NOT IN ('', '.')
              AND target NOT LIKE '<%'
              AND NOT regexp_matches(
                  target,
                  '(?i)(^|/)(tests?|specs?|__tests__)(/|$)|(^|/)(test_|conftest\.|.*_test\.)|(^|/)(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)'
              )
            GROUP BY 1, 2
        ),
        session_tokens AS (
            SELECT session_id, sum(approx_out_tokens) AS tokens
            FROM tool_events GROUP BY 1
        )
        SELECT s.repo, count(*) AS sessions, coalesce(sum(t.tokens), 0) AS tokens,
               arg_min(s.session_id, f.step) AS example_session,
               arg_min(f.step, f.step) AS example_step
        FROM sessions s
        JOIN first_edit f USING (session_id)
        LEFT JOIN session_tokens t USING (session_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM tool_events e
            WHERE e.session_id = s.session_id
              AND e.step_idx < f.step
              AND e.action_type = 'exec'
              AND e.outcome = 'err'
        )
        GROUP BY 1
        HAVING count(*) >= 3
        ORDER BY sessions DESC, tokens DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="no_reproduction",
            repo=repo,
            title="source edits before any failing reproduction",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{sessions} sessions edited source without a preceding failing exec",
            side="agent",
            recommendation="For a bug fix, reproduce the failure first with the narrowest command or test; only then edit the source.",
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, tokens, example_session, example_step in con.execute(query).fetchall()
    ]


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
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, tokens, example in con.execute(query).fetchall()
    ]


def context_blasts(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
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





def all_findings(con: duckdb.DuckDBPyConnection, limit: int = 25) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(hotspots(con, limit=limit))
    findings.extend(recovery_loops(con, limit=limit))
    findings.extend(context_blasts(con, limit=limit))
    findings.extend(orientation(con, limit=limit))
    findings.extend(global_patterns(con, limit=limit))
    findings.extend(selector_misses(con, limit=limit))
    findings.extend(workaround_scripts(con, limit=limit))
    findings.extend(stuck_loops(con, limit=limit))
    findings.extend(noop_retries(con, limit=limit))
    findings.extend(premature_completion(con, limit=limit))
    findings.extend(low_validation(con, limit=limit))
    findings.extend(site_edit_mismatch(con, limit=limit))
    findings.extend(permission_denials(con, limit=limit))
    findings.extend(repeated_reads(con, limit=limit))
    findings.extend(edit_churn(con, limit=limit))
    findings.extend(command_errors(con, limit=limit))
    findings.extend(feedback_loops(con, limit=limit))
    return findings
