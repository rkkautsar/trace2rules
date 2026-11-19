from __future__ import annotations

import duckdb

from trace2rules.detectors import (
    _context_advice,
    failure_loops,
    hotspots,
    low_validation,
    orientation,
    premature_completion,
    site_edit_mismatch,
)


def test_hotspots_ignore_readme_and_docs() -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE sessions (repo VARCHAR, session_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE tool_events (repo VARCHAR, target VARCHAR, session_id VARCHAR, action_type VARCHAR, approx_out_tokens INTEGER)"
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [("acme/repo", f"s{i}") for i in range(4)],
    )
    con.executemany(
        "INSERT INTO tool_events VALUES (?, ?, ?, 'read', 100)",
        [
            ("acme/repo", "README.md", f"s{i}") for i in range(4)
        ]
        + [
            ("acme/repo", "src/app.py", f"s{i}") for i in range(4)
        ],
    )
    findings = hotspots(con)
    assert [finding.title for finding in findings] == ["src/app.py"]


def test_orientation_is_repo_side_not_agent_friction() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE tool_events (repo VARCHAR, target VARCHAR, session_id VARCHAR, action_type VARCHAR, approx_out_tokens INTEGER)")
    con.executemany(
        "INSERT INTO tool_events VALUES (?, ?, ?, 'read', 100)",
        [("acme/repo", "README.md", f"s{i}") for i in range(4)],
    )
    findings = orientation(con)
    assert len(findings) == 1
    assert findings[0].side == "repo"
    assert findings[0].recommendation == ""


def test_validation_and_site_metrics_use_sessions_and_tokens(monkeypatch) -> None:
    import trace2rules.detectors as detectors_module

    monkeypatch.setattr(detectors_module, "_repo_window", lambda *args, **kwargs: ())
    con = duckdb.connect()
    con.execute("CREATE TABLE sessions (repo VARCHAR, session_id VARCHAR)")
    con.execute(
        "CREATE TABLE tool_events (repo VARCHAR, session_id VARCHAR, step_idx INTEGER, "
        "action_type VARCHAR, raw_target VARCHAR, object_id VARCHAR, outcome VARCHAR, "
        "approx_out_tokens INTEGER, error_site VARCHAR, segment_idx INTEGER)"
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [("acme/repo", f"s{i}") for i in range(3)],
    )
    con.executemany(
        "INSERT INTO tool_events VALUES ('acme/repo', ?, ?, 'edit', ?, ?, 'ok', 10, '', 0)",
        [(f"s{i}", 1, "src/a.py", "src/a.py") for i in range(3)],
    )
    for fn in (premature_completion, low_validation):
        finding = fn(con)[0]
        assert finding.sessions == 3
        assert finding.tokens == 30

    con.execute(
        "INSERT INTO tool_events VALUES "
        "('acme/repo', 's0', 10, 'exec', '', '', 'err', 7, 'src/a.py:1', 0),"
        "('acme/repo', 's0', 11, 'edit', 'src/b.py', 'src/b.py', 'ok', 11, '', 0),"
        "('acme/repo', 's0', 12, 'exec', '', '', 'err', 5, 'src/a.py:2', 0),"
        "('acme/repo', 's0', 13, 'edit', 'src/b.py', 'src/b.py', 'ok', 13, '', 0)"
    )
    finding = site_edit_mismatch(con)[0]
    assert finding.sessions == 1
    assert finding.tokens == 12
    assert "2 traceback sites" in finding.detail


def test_failure_loops_require_repeated_failures_and_intervening_edits(monkeypatch) -> None:
    import trace2rules.detectors as detectors_module

    monkeypatch.setattr(detectors_module, "_representative_window", lambda *args, **kwargs: ())
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE tool_events (session_id VARCHAR, repo VARCHAR, step_idx INTEGER, "
        "action_type VARCHAR, object_id VARCHAR, is_error BOOLEAN, error_signature VARCHAR, "
        "error_site VARCHAR, error_detail VARCHAR, obs_cause VARCHAR, approx_out_tokens INTEGER)"
    )
    rows = []
    for session in ("s1", "s2"):
        for step, action, is_error in ((1, "exec", True), (2, "edit", False), (3, "exec", True), (4, "edit", False), (5, "exec", True)):
            rows.append(
                (
                    session,
                    "acme/repo",
                    step,
                    action,
                    "src/a.py" if action == "edit" else "",
                    is_error,
                    "exception:AssertionError" if is_error else "",
                    "src/a.py:1" if is_error else "",
                    "assert 1 == 2" if is_error else "",
                    "",
                    10 if is_error else 1,
                )
            )
    con.executemany("INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    finding = failure_loops(con)[0]
    assert finding.sessions == 2
    assert finding.tokens == 60
    assert "6 repeats" in finding.detail
    assert "4 intervening edits" in finding.detail


def test_context_advice_distinguishes_test_output_from_repo_entrypoint() -> None:
    assert "single-test selector" in _context_advice("pytest tests/test_parser.py -v", "exec")[1]
    assert _context_advice("make unit", "exec")[0] == "repo"
    assert _context_advice("pytest -q tests/test_parser.py", "exec") == ("", "")


def test_runner_advice_is_language_neutral() -> None:
    for target in (
        "cargo test",
        "go test ./...",
        "npm test",
        "dotnet test",
        "rspec spec/foo_spec.rb",
        "phpunit",
        "mix test",
        "swift test",
    ):
        _, advice = _context_advice(target, "exec")
        assert "pytest" not in advice.lower()


def test_path_and_runner_heuristics_cover_multiple_languages() -> None:
    from trace2rules.ingest import _error_site, _is_path_token

    for path in ("src/main.rs", "cmd/app/main.go", "src/app.ts", "src/App.java", "src/main.c", "lib/x.rb"):
        assert _is_path_token(path), path
    assert _error_site('File "src/main.go", line 12').endswith("src/main.go:12")
    for target in ("cargo test", "go test ./...", "npm test"):
        advice = _context_advice(target, "exec")[1]
        assert "pytest" not in advice.lower()
