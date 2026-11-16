from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest

from trace2rules.detectors import Finding
from trace2rules.ingest import write_sessions
from trace2rules.models import Session, ToolCall
from trace2rules.sources.agent_trajectory import (
    _detect_source,
    _records_to_session,
    ingest_agent_sessions,
)
from trace2rules.sources.common import classify_tool_call
from trace2rules.sources.swe_chat import ingest_swe_chat
from trace2rules.synthesize import _offline_synthesis


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


def test_same_stem_sessions_get_unique_ids(tmp_path: Path) -> None:
    records = [{"role": "meta", "cwd": "/home/user/project"}]
    first = _records_to_session(
        tmp_path / "project-a" / "Task.jsonl", "claude-code", records, None
    )
    second = _records_to_session(
        tmp_path / "project-b" / "Task.jsonl", "claude-code", records, None
    )
    assert first.session_id != second.session_id

    db = tmp_path / "trace.duckdb"
    write_sessions(db, [first, second])
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM sessions").fetchone() == (2,)
    finally:
        con.close()


def test_opencode_json_export_is_detected(tmp_path: Path) -> None:
    export = tmp_path / "session.json"
    export.write_text("{}")
    assert _detect_source(export) == "opencode"

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "one.json").write_text("{}")
    assert _detect_source(export_dir) == "opencode"


def test_unknown_tools_are_other_actions() -> None:
    action, target, verb, object_id = classify_tool_call(
        "mcp__Claude_Browser__navigate", {}, '{"url": "https://example.com"}'
    )
    assert action == "other"
    assert target
    assert verb == "mcp__claude_browser__navigate"
    assert object_id


def test_offline_synthesis_skips_command_hotspots() -> None:
    synthesis = _offline_synthesis(
        [
            Finding(
                kind="hotspot",
                repo="acme/repo",
                title="git diff",
                sessions=5,
                tokens=100,
            ),
            Finding(
                kind="hotspot",
                repo="acme/repo",
                title="src/app.py",
                sessions=5,
                tokens=100,
            ),
        ],
        "acme/repo",
    )
    assert len(synthesis.rules) == 1
    assert "git diff" not in synthesis.rules[0].rule_markdown
    assert "src/app.py" in synthesis.rules[0].rule_markdown


def test_opencode_export_ingests(tmp_path: Path) -> None:
    pytest.importorskip("trajectory")
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for agent-trajectory")
    export = tmp_path / "session.json"
    export.write_text(
        json.dumps(
            {
                "info": {
                    "id": "ses_test",
                    "directory": "/repo",
                    "time": {"created": 1_700_000_000_000},
                },
                "messages": [
                    {
                        "info": {
                            "id": "msg_1",
                            "role": "user",
                            "sessionID": "ses_test",
                            "time": {"created": 1_700_000_000_001},
                        },
                        "parts": [
                            {
                                "type": "text",
                                "text": "check the repo",
                                "id": "part_1",
                                "sessionID": "ses_test",
                                "messageID": "msg_1",
                            }
                        ],
                    },
                    {
                        "info": {
                            "id": "msg_2",
                            "role": "assistant",
                            "sessionID": "ses_test",
                            "time": {"created": 1_700_000_000_002},
                        },
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "bash",
                                "callID": "call_1",
                                "state": {
                                    "status": "completed",
                                    "input": {"command": "pwd"},
                                    "output": "/repo",
                                },
                                "id": "part_2",
                                "sessionID": "ses_test",
                                "messageID": "msg_2",
                            }
                        ],
                    },
                ],
            }
        )
    )
    db = tmp_path / "trace.duckdb"
    stats = ingest_agent_sessions(export, db, limit=1)
    assert stats[0].sessions == 1
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT agent, source FROM sessions").fetchone() == (
            "opencode",
            "agent-trajectory",
        )
        assert con.execute(
            "SELECT action_type, target FROM tool_events"
        ).fetchone() == ("exec", "pwd")
    finally:
        con.close()
