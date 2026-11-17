from __future__ import annotations

import duckdb

from trace2rules.detectors import _context_advice, hotspots, orientation


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


def test_context_advice_distinguishes_test_output_from_repo_entrypoint() -> None:
    assert _context_advice("pytest tests/test_parser.py -v", "exec")[1].startswith("Run a targeted test")
    assert _context_advice("make unit", "exec")[0] == "repo"
    assert _context_advice("pytest -q tests/test_parser.py", "exec") == ("", "")
