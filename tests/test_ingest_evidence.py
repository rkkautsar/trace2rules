from __future__ import annotations

from trace2rules.ingest.test_results import parse_test_result
from trace2rules.ingest.normalize import (
    _error_detail,
    _error_site,
    _event_outcome,
    _is_control_target,
    _observation_cause,
)


def test_parse_test_result_counts_pytest_and_jest() -> None:
    assert parse_test_result(
        "uv run pytest -q", "3 failed, 10 passed, 2 skipped in 1.2s"
    ) == (15, 10, 3, 0, 2)
    assert parse_test_result(
        "npm test", "Tests: 1 failed, 2 skipped, 8 passed, 11 total"
    ) == (11, 8, 1, 0, 2)
    assert parse_test_result("ls", "3 passed") is None


def test_writer_persists_test_summary(tmp_path) -> None:
    from trace2rules.ingest import write_sessions
    from trace2rules.models import Session, ToolCall

    db = tmp_path / "tests.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                calls=[
                    ToolCall(
                        "Bash",
                        "uv run pytest -q",
                        observation="3 failed, 10 passed, 2 skipped in 1.2s",
                        action_type="exec",
                        target="uv run pytest -q",
                        verb="pytest",
                        object_id="pytest",
                        is_error=True,
                    )
                ],
            )
        ],
    )
    from trace2rules.db import connect

    con = connect(db)
    try:
        assert con.execute(
            "SELECT total, passed, failed, errors, skipped FROM test_results"
        ).fetchone() == (15, 10, 3, 0, 2)
    finally:
        con.close()


def test_search_exit_one_is_empty_not_error() -> None:
    assert _event_outcome("search", "", 1, False, False, False, "grep") == "empty"
    assert _event_outcome("search", "", 1, False, False, False, "find") == "err"
    assert _event_outcome("search", "", 2, False, False, False, "grep") == "err"


def test_control_targets_are_dropped() -> None:
    assert _is_control_target("C-c")
    assert _is_control_target("\x03")
    assert not _is_control_target("pytest -q")


def test_error_evidence_extracts_cause_and_site() -> None:
    observation = (
        "running tests\n"
        "FAILED tests/test_parser.py::test_parse - AttributeError: module 'sqlglot' has no attribute 'tokenize'\n"
        "Traceback (most recent call last):\n"
        "  File \"/workspace/tests/test_parser.py\", line 42, in test_parse\n"
    )
    cause = _observation_cause(observation)
    assert "AttributeError" in cause
    assert _error_detail(observation) == "AttributeError: <object> has no attribute 'tokenize'"
    assert _error_site(observation) == "tests/test_parser.py::test_parse"


def test_canonical_writer_persists_source_and_cause(tmp_path) -> None:
    from trace2rules.db import connect
    from trace2rules.ingest import write_sessions
    from trace2rules.models import Session, ToolCall

    db = tmp_path / "evidence.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="agent-trajectory",
                source_ref="/tmp/session.jsonl",
                calls=[
                    ToolCall(
                        "Bash",
                        "python -c 'raise ValueError(\"boom\")'",
                        observation="Traceback\nValueError: boom\n  File \"/workspace/src/a.py\", line 4",
                    )
                ],
            )
        ],
    )
    con = connect(db)
    try:
        row = con.execute(
            "SELECT source_ref, obs_cause, error_detail, error_site FROM sessions JOIN tool_events USING (session_id)"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "/tmp/session.jsonl"
    assert "ValueError" in row[1]
    assert "ValueError" in row[2]
    assert row[3].endswith("a.py:4")


def test_writer_splits_compaction_segments(tmp_path) -> None:
    from trace2rules.db import connect
    from trace2rules.ingest import write_sessions
    from trace2rules.models import Session, ToolCall

    db = tmp_path / "segments.duckdb"
    session = Session(
        "s1",
        "acme/repo",
        source="agent-trajectory",
        source_ref="/tmp/session.jsonl",
        calls=[
            ToolCall("Read", "src/a.py", action_type="read", target="src/a.py", segment_idx=0),
            ToolCall("Read", "src/b.py", action_type="read", target="src/b.py", segment_idx=0),
            ToolCall("Edit", "src/a.py", action_type="edit", target="src/a.py", segment_idx=1),
        ],
    )
    write_sessions(db, [session], split_compactions=True)
    con = connect(db)
    try:
        assert con.execute(
            "SELECT session_id, total_turns FROM sessions ORDER BY session_id"
        ).fetchall() == [("s1#seg0", 2), ("s1#seg1", 1)]
        assert con.execute(
            "SELECT DISTINCT segment_idx FROM tool_events"
        ).fetchall() == [(0,)]
        assert con.execute(
            "SELECT count(*) FROM tool_events WHERE session_id = 's1#seg1'"
        ).fetchone() == (1,)
    finally:
        con.close()

    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="agent-trajectory",
                source_ref="/tmp/session.jsonl",
                calls=[ToolCall("Read", "src/a.py", action_type="read", target="src/a.py")],
            )
        ],
        split_compactions=True,
    )
    con = connect(db)
    try:
        assert con.execute("SELECT session_id FROM sessions").fetchall() == [("s1",)]
    finally:
        con.close()
