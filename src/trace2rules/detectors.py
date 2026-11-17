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


_TEST_TARGET_RE = re.compile(
    r"(?i)(^|\s)(pytest|py\.test|tox|nox|python3?\s+-m\s+(?:pytest|unittest)|make\s+(?:test|unit|check)|"
    r"npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test)(\s|$)"
)
_INSTALL_TARGET_RE = re.compile(
    r"(?i)(^|\s)(pip|pip3|uv|poetry|npm|yarn|pnpm|cargo|go)\s+(?:install|sync|add|get)"
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
        if "npm" in target or "yarn" in target or "pnpm" in target:
            return "agent", "Run the runner's single-test selector instead of the full test script."
        if "mvn" in target or "gradle" in target:
            return "agent", "Run a single test class/method instead of the full build test task."
        return (
            "agent",
            "Run a targeted test (`pytest path::Test::test -q --tb=short`) instead of the full suite or `-v`.",
        )
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
        return "agent", "Use `rg -n` or a line range instead of reading the whole file."
    return "agent", "Bound the command output with a quiet flag, `tail`, or a narrower selector."


def _representative_window(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    failure_step: int,
    recovery_step: int | None = None,
) -> tuple[dict, ...]:
    if not session_id:
        return ()
    end = max(failure_step, recovery_step or failure_step + 2)
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
               CASE WHEN e.outcome = 'err' THEN e.error_message ELSE '' END
        FROM tool_events e
        JOIN sessions s USING (session_id)
        WHERE e.session_id = ? AND e.step_idx BETWEEN ? AND ? AND e.segment_idx = ?
        ORDER BY step_idx
        LIMIT 10
        """,
        [session_id, max(0, failure_step - 3), end, segment_idx],
    ).fetchall()
    return tuple(
        {
            "step": int(step),
            "session_id": session_id,
            "session_ref": session_ref,
            "action": action,
            "verb": verb,
            "object": object_id,
            "outcome": outcome,
            "exit_code": exit_code,
            "tokens": int(tokens or 0),
            "recovered": recovery_step is not None and int(step) == int(recovery_step),
            "excerpt": (excerpt or "")[:300],
            "error_message": (error_message or "")[:600],
        }
        for step, action, verb, object_id, outcome, exit_code, tokens, excerpt, _segment, session_ref, error_message in rows
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
            recommendation="When `-k` reports 0 selected/deselected, run `pytest --collect-only -q <path>` and use the printed node ID; `-k` matches test node names, not files.",
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
        )
        for repo, sessions, edits, runs, tokens, failures in con.execute(query).fetchall()
    ]


def stuck_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, object_id, count(*) AS edits, count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens, any_value(error_family) AS family
        FROM tool_events
        WHERE action_type = 'edit' AND object_id <> ''
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
        )
        for repo, occurrences, sessions, exemplar in con.execute(query).fetchall()
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
    return findings
