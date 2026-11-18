from __future__ import annotations

import re
from dataclasses import dataclass, field

import duckdb

_DOC_TARGET_RE = re.compile(
    r"(?i)(^|/)(readme|docs?|changelog|contributing|license)([.]|/|$)"
)


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
    intent: str = ""
    object_type: str = ""
    intent_confidence: float = 0.0
    object_confidence: float = 0.0
    event_keys: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    cause: str = ""
    failure_family: str = ""
    cause_support_sessions: int = 0
    side: str = ""
    recommendation: str = ""
    window: tuple[dict, ...] = field(default_factory=tuple)
    edit_sessions: int = 0

    @property
    def evidence(self) -> str:
        return f"{self.detail}; ~{self.tokens:,} tokens"


def session_summary(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT s.repo,
               count(*) AS sessions,
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
            "events": r[2] or 0,
            "tokens": r[3] or 0,
        }
        for r in rows
    ]


_GENERIC_SCRIPT_RE = re.compile(
    r"(?i)^(?:reproduce|repro|scratch|debug|verify|verification|comprehensive|final|"
    r"test_fix|test_repro|validation_test)"
)


_TEST_TARGET_RE = re.compile(
    r"(?i)(^|\s)(pytest|py\.test|tox|nox|python3?\s+-m\s+(?:pytest|unittest)|make\s+(?:test|unit|check)|"
    r"npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|"
    r"dotnet\s+test|rspec|vitest|jest|phpunit|mix\s+test|swift\s+test|bundle\s+exec\s+rspec)(\s|$)"
)
_INSTALL_TARGET_RE = re.compile(
    r"(?i)(^|\s)(pip|pip3|uv|poetry|npm|yarn|pnpm|cargo|go|bundle|composer|dotnet|mix)\s+"
    r"(?:install|sync|add|get|ci|restore|deps\.get)"
)
_HELP_TARGET_RE = re.compile(r"(?:^|\s)(?:--help|-h)(?:\s|$)")
_BOUNDED_RE = re.compile(r"(?:\|\s*(?:head|tail)\b|\s(?:-q|--quiet|-qq|--tb=short)\b)")


def _is_orientation(target: str) -> bool:
    return bool(_DOC_TARGET_RE.search(target))


def _context_advice(target: str, action: str) -> tuple[str, str]:
    if _BOUNDED_RE.search(target):
        return "", ""
    if action == "search":
        return (
            "agent",
            f"Prefer `rg -n` plus a file/type restriction instead of broad `{target[:80]}` searches.",
        )
    if _TEST_TARGET_RE.search(target):
        if target.lstrip().startswith("make "):
            return (
                "repo",
                "Add a `test-one` target that forwards the test path; the current target runs the whole suite.",
            )
        if "unittest" in target:
            return (
                "agent",
                "Run a targeted unittest case (`python -m unittest module.Class.test`) instead of full discovery or `-v`.",
            )
        if "cargo" in target:
            return "agent", "Run a targeted `cargo test <name>` instead of the full test suite."
        if "go test" in target:
            return "agent", "Run `go test ./path -run TestName` instead of the full package suite."
        if "npm" in target or "yarn" in target or "pnpm" in target or "vitest" in target or "jest" in target:
            return "agent", "Run the runner's single-test selector instead of the full test script."
        if "mvn" in target or "gradle" in target or "dotnet" in target:
            return "agent", "Run a single test class/method instead of the full build test task."
        if "phpunit" in target:
            return "agent", "Run `phpunit --filter <test>` instead of the full PHP test suite."
        if "rspec" in target:
            return "agent", "Run `rspec <spec_file>:<line>` instead of the full RSpec suite."
        if "mix test" in target:
            return "agent", "Run `mix test <file>:<line>` instead of the full test suite."
        if "swift test" in target:
            return "agent", "Run `swift test --filter <test>` instead of the full suite."
        return "agent", "Use the runner's single-test selector instead of a full-suite run."
    if _INSTALL_TARGET_RE.search(target):
        return (
            "repo",
            "Document or prebuild the environment once; repeated editable-install output is setup tax.",
        )
    if _HELP_TARGET_RE.search(target):
        return "agent", "Pipe `--help` through `head -60` or inspect the subcommand docs."
    if action == "read" and " " not in target and "." not in target.rsplit("/", 1)[-1]:
        return "agent", "List the directory one level deep (`ls`) instead of dumping a recursive tree."
    if action == "read":
        return (
            "agent",
            "For a large or repeatedly read file, use `rg -n` to locate the symbol and read the enclosing line range; a single whole-file read is fine for small files.",
        )
    return "agent", "Bound the command output with a quiet flag, `tail`, or a narrower selector."


def _representative_window(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    failure_step: int,
    recovery_step: int | None = None,
) -> tuple[dict, ...]:
    if not session_id:
        return ()
    end = max(failure_step, recovery_step or failure_step + 3)
    segment = con.execute(
        "SELECT segment_idx FROM tool_events WHERE session_id = ? AND step_idx = ?",
        [session_id, failure_step],
    ).fetchone()
    segment_idx = segment[0] if segment else 0
    rows = con.execute(
        """
        SELECT e.step_idx, e.action_type, e.verb, e.object_id, e.outcome, e.exit_code,
               e.approx_out_tokens, coalesce(nullif(e.obs_cause, ''), e.obs_excerpt),
               e.segment_idx, s.source_ref,
               CASE WHEN e.outcome = 'err' THEN e.error_message ELSE '' END,
               e.raw_target, e.target, e.assistant_context
        FROM tool_events e
        JOIN sessions s USING (session_id)
        WHERE e.session_id = ? AND e.step_idx BETWEEN ? AND ? AND e.segment_idx = ?
          AND NOT regexp_matches(
              e.object_id,
              '(?i)^(reproduce|repro|scratch|debug|verify|verification|comprehensive|final|test_fix|test_repro|validation_test)'
          )
        ORDER BY step_idx
        LIMIT 12
        """,
        [session_id, max(0, failure_step - 5), end, segment_idx],
    ).fetchall()
    return tuple(
        {
            "step": int(step),
            "session_id": session_id,
            "session_ref": session_ref,
            "action": action,
            "verb": verb,
            "object": object_id,
            "target": target,
            "command": (raw_target or "")[:300],
            "outcome": outcome,
            "exit_code": exit_code,
            "tokens": int(tokens or 0),
            "recovered": recovery_step is not None and int(step) == int(recovery_step),
            "excerpt": (excerpt or "")[:300],
            "error_message": (error_message or "")[:600],
            "assistant": (assistant_context or "")[:600],
        }
        for step, action, verb, object_id, outcome, exit_code, tokens, excerpt, _segment, session_ref, error_message, raw_target, target, assistant_context in rows
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


def _repo_window(
    con: duckdb.DuckDBPyConnection, repo: str, where: str, params: tuple = ()
) -> tuple[dict, ...]:
    row = con.execute(
        f"""SELECT session_id, step_idx FROM tool_events
            WHERE repo = ? AND {where}
            ORDER BY approx_out_tokens DESC LIMIT 1""",
        [repo, *params],
    ).fetchone()
    return _representative_window(con, row[0], row[1]) if row else ()


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
               sum(tokens) AS tokens, sum(failures) AS failures
        FROM (
            SELECT session_id, repo,
                   count(*) FILTER (WHERE action_type = 'edit' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS edits,
                   count(*) FILTER (WHERE action_type = 'exec' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS runs,
                   sum(approx_out_tokens) FILTER (WHERE action_type = 'exec' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS tokens,
                   count(*) FILTER (WHERE outcome = 'err' AND regexp_matches(object_id,
                       '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')) AS failures
            FROM tool_events
            WHERE NOT regexp_matches(
                object_id,
                '(?i)^(reproduce|repro|scratch|debug|verify|verification|comprehensive|final|test_fix|test_repro|validation_test)'
            )
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
            title="root-level throwaway reproducer scripts",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits, {runs} runs, {failures} failures",
            side="agent",
            recommendation="Write at most one scratch reproducer, run it once, then delete it; use the repository's narrowest existing test selector.",
            window=_repo_window(
                con,
                repo,
                "action_type = 'exec' AND regexp_matches(object_id, '(?i)^(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)')",
            ),
        )
        for repo, sessions, edits, runs, tokens, failures in con.execute(query).fetchall()
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
               any_value(raw_target) AS exemplar
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
            tokens=occurrences,
            detail=f"{occurrences} unchanged retries",
            side="agent",
            recommendation="A failing command repeated unchanged will fail again; change the selector/input or inspect state before rerunning.",
            window=_repo_window(con, repo, "is_error AND raw_target = ?", (exemplar or "",)),
        )
        for repo, occurrences, sessions, exemplar in con.execute(query).fetchall()
    ]


def premature_completion(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH last_edit AS (
            SELECT session_id, max(step_idx) AS step
            FROM tool_events WHERE action_type = 'edit' GROUP BY 1
        )
        SELECT s.repo, count(*) AS sessions, sum(l.step) AS edit_steps
        FROM sessions s
        JOIN last_edit l USING (session_id)
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
            tokens=int(edit_steps or 0),
            detail=f"{sessions} sessions; no successful exec after last edit",
            side="agent",
            recommendation="After the final edit, run the narrowest relevant test or lint command and confirm it passes before finishing.",
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, edit_steps in con.execute(query).fetchall()
    ]


def low_validation(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        WITH first_edit AS (
            SELECT session_id, min(step_idx) AS step
            FROM tool_events WHERE action_type = 'edit' GROUP BY 1
        )
        SELECT s.repo, count(*) AS sessions, sum(f.step) AS edit_steps
        FROM sessions s
        JOIN first_edit f USING (session_id)
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
            tokens=int(edit_steps or 0),
            detail=f"{sessions} sessions; no test/lint exec after first edit",
            side="agent",
            recommendation="After changing code, run the narrowest test/lint command that exercises the change.",
            window=_repo_window(con, repo, "action_type = 'edit'"),
        )
        for repo, sessions, edit_steps in con.execute(query).fetchall()
    ]


def site_edit_mismatch(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    rows = con.execute(
        """
        SELECT e.repo, e.session_id, e.step_idx, e.error_site,
               lead(e.action_type) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_action,
               lead(e.object_id) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_object
        FROM tool_events e
        WHERE e.error_site <> ''
        """
    ).fetchall()
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for repo, session, step, site, next_action, next_object in rows:
        if next_action != "edit" or not next_object:
            continue
        site_file = site.rsplit(":", 1)[0]
        if site_file.rsplit("/", 1)[-1] == next_object.rsplit("/", 1)[-1]:
            continue
        counts[repo] = counts.get(repo, 0) + 1
        examples.setdefault(repo, f"{site} -> {next_object}")
    findings = [
        Finding(
            kind="site_edit_mismatch",
            repo=repo,
            title=examples.get(repo, "traceback site differs from edited file"),
            sessions=count,
            tokens=count,
            detail=f"{count} traceback sites followed by an edit to a different file",
            side="agent",
            recommendation="Edit the file named by the traceback or failing assertion; do not patch a different layer or the reproducer.",
        )
        for repo, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
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
    return findings
