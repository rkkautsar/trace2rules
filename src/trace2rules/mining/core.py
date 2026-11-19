from __future__ import annotations

import re

import duckdb

_TEST_OR_SCRATCH_RE = re.compile(
    r"(?i)(^|/)(tests?|specs?|__tests__)(/|$)"
    r"|(^|/)(test_|conftest\.|.*_test\.|.*\.spec\.)"
    r"|(^|/)(reproduce|debug|verify|comprehensive|final|test_fix|test_repro)"
    r"|/(site-packages|dist-packages)/"
)


_DOC_TARGET_RE = re.compile(
    r"(?i)(^|/)(readme|docs?|changelog|contributing|license)([.]|/|$)"
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
