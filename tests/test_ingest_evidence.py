from __future__ import annotations

from trace2rules.ingest.normalize import (
    _error_detail,
    _error_site,
    _event_outcome,
    _is_control_target,
    _observation_cause,
)


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
