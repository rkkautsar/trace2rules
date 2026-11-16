from __future__ import annotations

from pathlib import Path

import duckdb

from trace2rules.ingest import write_sessions
from trace2rules.models import Session, ToolCall
from trace2rules.sources.agent_trajectory import _records_to_session
from trace2rules.sources.common import classify_tool_call
from trace2rules.sources.swe_chat import ingest_swe_chat


def _write_swe_chat(root: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE sessions ("
            "session_id VARCHAR, repo_id VARCHAR, agent VARCHAR, session_success VARCHAR)"
        )
        con.execute(
            "INSERT INTO sessions VALUES "
            "('s1', 'acme/repo', 'Claude Code', '82.0')"
        )
        con.execute(
            "CREATE TABLE conversations ("
            "session_id VARCHAR, turn_number BIGINT, turn_type VARCHAR, "
            "tool_name VARCHAR, tool_call_id VARCHAR, tool_input_json VARCHAR, "
            "command VARCHAR, file_path VARCHAR, content VARCHAR)"
        )
        con.execute(
            "INSERT INTO conversations VALUES "
            "('s1', 0, 'tool_use', 'Bash', 't1', '{\"command\":\"pytest -q\"}', "
            "'pytest -q', '', NULL), "
            "('s1', 1, 'tool_result', 'Bash', 't1', '', '', '', 'Exit code: 1'), "
            "('s1', 2, 'tool_use', 'Read', 't2', '{\"file_path\":\"/repo/src/a.py\"}', "
            "'', '/repo/src/a.py', NULL), "
            "('s1', 3, 'tool_result', 'Read', 't2', '', '', '', 'print(1)')"
        )
        con.execute(
            f"COPY sessions TO '{root / 'sessions.parquet'}' (FORMAT PARQUET)"
        )
        con.execute(
            f"COPY conversations TO '{root / 'conversations.parquet'}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def test_swe_chat_adapter_pairs_tools_and_results(tmp_path: Path) -> None:
    data = tmp_path / "swe-chat"
    data.mkdir()
    _write_swe_chat(data)
    db = tmp_path / "trace.duckdb"

    stats = ingest_swe_chat(data, db, limit=10)

    assert stats.sessions == 1
    assert stats.events == 2
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT source, agent, success_score FROM sessions"
        ).fetchone() == ("swe-chat", "Claude Code", 82.0)
        rows = con.execute(
            "SELECT action_type, outcome, target, obs_excerpt FROM tool_events "
            "ORDER BY step_idx"
        ).fetchall()
        assert rows[0][0] == "exec"
        assert rows[0][1] == "err"
        assert rows[0][2] == "pytest -q"
        assert rows[1][0] == "read"
        assert rows[1][1] == "ok"
        assert rows[1][2].endswith("src/a.py")
        assert rows[1][3] == "print(1)"
    finally:
        con.close()


def test_write_sessions_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "trace.duckdb"
    call = ToolCall(
        "Read",
        "/repo/src/x.py",
        observation="contents",
        action_type="read",
        target="repo/src/x.py",
        verb="read",
        object_id="repo/src/x.py",
    )
    first = Session(
        "s1", "acme/repo", source="test", agent="claude-code", calls=[call]
    )
    write_sessions(db, [first])
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="test",
                agent="claude-code",
                calls=[call, call],
            )
        ],
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT source, agent FROM sessions").fetchone() == (
            "test",
            "claude-code",
        )
        assert con.execute("SELECT count(*) FROM tool_events").fetchone() == (2,)
    finally:
        con.close()


def test_file_tool_uses_full_path_as_object_id() -> None:
    action, target, verb, object_id = classify_tool_call(
        "Read", {"file_path": "/repo/src/__init__.py"}, "/repo/src/__init__.py"
    )
    assert action == "read"
    assert target.endswith("src/__init__.py")
    assert object_id == target
    assert verb == "read"


def test_agent_records_pair_calls_and_results() -> None:
    session = _records_to_session(
        Path("session.jsonl"),
        "claude-code",
        [
            {"role": "meta", "cwd": "/home/user/project"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "Bash",
                        "args": '{"command": "pytest -q"}',
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "Exit code: 1",
                "ok": False,
            },
        ],
        None,
    )
    assert session.repo == "project"
    assert len(session.calls) == 1
    assert session.calls[0].observation == "Exit code: 1"
    assert session.calls[0].is_error is True
