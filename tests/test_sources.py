from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import duckdb
import pytest

from trace2rules.mining import Finding
from trace2rules.ingest import write_sessions
from trace2rules.models import Session, ToolCall
from trace2rules.ingest.sources.agent_trajectory import (
    TranscriptMeta,
    _assign_unique_session_ids,
    _detect_source,
    _normalize_batch,
    _records_to_session,
    _relevant_line,
    _resolve_instance,
    _resolve_repos,
    _scan_transcript,
    extract_transcript_meta,
    ingest_agent_sessions,
)
from trace2rules.ingest.sources.common import classify_tool_call
from trace2rules.ingest.sources.opencode import ingest_opencode
from trace2rules.ingest.sources.swe_chat import ingest_swe_chat


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


def test_detect_codex_sessions() -> None:
    assert _detect_source(Path("/home/user/.codex/sessions")) == "codex"


def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def test_omp_compaction_segments_calls() -> None:
    text = _jsonl(
        [
            {"type": "session", "id": "omp-1", "parentSession": "parent-1"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "c1"}],
                },
            },
            {"type": "compaction"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "c2"}],
                },
            },
        ]
    )
    meta = extract_transcript_meta(Path("omp.jsonl"), "omp", text)
    assert meta.session_id == "omp-1"
    assert meta.parent_session_id == "parent-1"
    assert meta.segments == 2
    assert meta.segment_of_call == {"c1": 0, "c2": 1}


def test_codex_compaction_segments_calls() -> None:
    text = _jsonl(
        [
            {"type": "session_meta", "payload": {"id": "cx-1", "parent_thread_id": "p"}},
            {
                "type": "response_item",
                "payload": {"type": "function_call", "call_id": "f1"},
            },
            {"type": "response_item", "payload": {"type": "compaction"}},
            {
                "type": "response_item",
                "payload": {"type": "function_call", "call_id": "f2"},
            },
        ]
    )
    meta = extract_transcript_meta(Path("codex.jsonl"), "codex", text)
    assert meta.session_id == "cx-1"
    assert meta.parent_session_id == "p"
    assert meta.segments == 2
    assert meta.segment_of_call == {"f1": 0, "f2": 1}


def test_claude_compaction_segments_calls() -> None:
    text = _jsonl(
        [
            {"type": "user", "sessionId": "cc-1", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "t1"}]},
            },
            {"type": "summary", "summary": "compact"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "t2"}]},
            },
        ]
    )
    meta = extract_transcript_meta(Path("claude.jsonl"), "claude-code", text)
    assert meta.session_id == "cc-1"
    assert meta.segments == 2
    assert meta.segment_of_call == {"t1": 0, "t2": 1}


def test_pi_compaction_segments_calls() -> None:
    text = _jsonl(
        [
            {"type": "session", "id": "pi-1"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "p1"}],
                },
            },
            {"type": "compaction"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "p2"}],
                },
            },
        ]
    )
    meta = extract_transcript_meta(Path("pi.jsonl"), "pi", text)
    assert meta.session_id == "pi-1"
    assert meta.segments == 2
    assert meta.segment_of_call == {"p1": 0, "p2": 1}


def test_continuation_identity_preserves_titles() -> None:
    metas = {
        "root": TranscriptMeta(session_id="root", title="Task"),
        "same": TranscriptMeta(
            session_id="same",
            parent_session_id="root",
            title="Task",
            is_continuation=True,
        ),
        "changed": TranscriptMeta(
            session_id="changed",
            parent_session_id="root",
            title="Different",
            is_continuation=True,
        ),
        "subagent": TranscriptMeta(
            session_id="subagent",
            parent_session_id="root",
            title="Subtask",
        ),
    }
    assert _resolve_instance("same", metas) == "root"
    assert _resolve_instance("changed", metas) == "changed"
    assert _resolve_instance("subagent", metas) == "root"


def test_opencode_v2_ingests_tools_and_compaction(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE session_v2 (id TEXT, parent_id TEXT, directory TEXT, title TEXT)"
    )
    con.execute(
        "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, data TEXT)"
    )
    con.execute(
        "INSERT INTO session_v2 VALUES ('s1', NULL, '/repo', 'task')"
    )
    con.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "s1",
            "assistant",
            0,
            json.dumps(
                {
                    "content": [
                        {
                            "type": "tool",
                            "name": "bash",
                            "state": {
                                "status": "completed",
                                "input": {"command": "git status"},
                                "output": "ok",
                            },
                        }
                    ]
                }
            ),
        ),
    )
    con.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
        ("m2", "s1", "assistant", 1, json.dumps({"content": [{"type": "compaction"}]})),
    )
    con.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
        (
            "m3",
            "s1",
            "assistant",
            2,
            json.dumps(
                {
                    "content": [
                        {
                            "type": "tool",
                            "name": "read",
                            "state": {
                                "status": "error",
                                "input": {"filePath": "src/a.py"},
                                "error": "missing",
                            },
                        }
                    ]
                }
            ),
        ),
    )
    con.commit()
    con.close()
    out = tmp_path / "trace.duckdb"
    stats = ingest_opencode(db, out, split_compactions=False)
    assert stats[0].sessions == 1
    assert stats[0].events == 2
    con = duckdb.connect(str(out), read_only=True)
    try:
        rows = con.execute(
            "SELECT target, segment_idx FROM tool_events ORDER BY step_idx"
        ).fetchall()
        assert rows == [("git status", 0), ("src/a.py", 1)]
    finally:
        con.close()


def test_opencode_v1_ingests_tools(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT)")
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (message_id TEXT, ordinal INTEGER, data TEXT)")
    con.execute("INSERT INTO session VALUES ('s1', NULL, '/repo', 'task')")
    con.execute("INSERT INTO message VALUES ('m1', 's1', 1, '{}')")
    con.execute(
        "INSERT INTO part VALUES (?, ?, ?)",
        (
            "m1",
            0,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pytest -q"},
                        "output": "ok",
                    },
                }
            ),
        ),
    )
    con.commit()
    con.close()
    out = tmp_path / "trace.duckdb"
    stats = ingest_opencode(db, out)
    assert stats[0].sessions == 1
    assert stats[0].events == 1
    con = duckdb.connect(str(out), read_only=True)
    try:
        assert con.execute("SELECT target FROM tool_events").fetchone() == ("pytest -q",)
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


def test_repo_label_uses_git_remote(tmp_path: Path) -> None:
    from trace2rules.ingest.sources.agent_trajectory import _repo_from_cwd

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:acme/repo.git"],
        check=True,
    )
    assert _repo_from_cwd(str(tmp_path)) == "acme/repo"


def test_limit_selects_most_recent_files(tmp_path: Path) -> None:
    import os
    import time
    from trace2rules.ingest.sources.agent_trajectory import _iter_files

    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    assert _iter_files(tmp_path, "omp", 1) == [new]


def test_duplicate_session_ids_are_suffixed() -> None:
    metas = [
        (Path("/sessions/a.jsonl"), TranscriptMeta(session_id="same")),
        (Path("/sessions/b.jsonl"), TranscriptMeta(session_id="same")),
    ]
    _assign_unique_session_ids(metas)
    assert metas[0][1].session_id == "same"
    assert metas[1][1].session_id.startswith("same#")
    assert metas[1][1].session_id != "same"


def test_compaction_markers_are_relevant() -> None:
    assert _relevant_line('{"tool_call": "x"}', "omp")
    assert _relevant_line('{"isCompactSummary": true}', "claude-code")


def test_normalize_batch_bisects_instead_of_retrying_each_file(tmp_path: Path) -> None:
    paths = [tmp_path / f"{index}.jsonl" for index in range(16)]
    for index, path in enumerate(paths):
        path.write_text("boom\n" if index == 15 else f"ok-{index}\n")
    calls: list[int] = []

    def fake_normalize(requests):
        calls.append(len(requests))
        if any("boom" in request["transcript"] for request in requests):
            raise RuntimeError("runtime crashed")
        return [
            {"records": [{"role": "meta", "marker": request["transcript"].strip()}]}
            for request in requests
        ]

    batch = [(path, TranscriptMeta()) for path in paths]
    results = _normalize_batch("omp", {}, batch, {}, fake_normalize)

    # Bisection isolates the bad file in O(log n) requests, not one request per file.
    assert len(calls) < len(paths)
    assert len(results) == len(batch)
    assert results[15] == {"records": [], "diagnostics": []}
    for index in range(15):
        assert results[index]["records"][0]["marker"] == f"ok-{index}"


def test_normalize_batch_reuses_scanned_text(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("hello\n")
    _path, _meta, text, error = _scan_transcript(path, "omp")
    assert error is None and text == "hello\n"
    path.unlink()
    seen: list[str] = []

    def fake_normalize(requests):
        seen.append(requests[0]["transcript"])
        return [{"records": [{"role": "meta"}]}]

    results = _normalize_batch(
        "omp", {}, [(path, TranscriptMeta())], {path: text}, fake_normalize
    )
    assert seen == ["hello\n"]
    assert results[0]["records"] == [{"role": "meta"}]


def test_resolve_repos_resolves_each_cwd_once(monkeypatch) -> None:
    from trace2rules.ingest.sources import agent_trajectory

    calls: list[str] = []

    def fake_repo(cwd):
        calls.append(cwd)
        return f"repo:{cwd}"

    monkeypatch.setattr(agent_trajectory, "_repo_from_cwd", fake_repo)
    repos = _resolve_repos(["/a", "/a", "/b", "", None])

    assert repos == {"/a": "repo:/a", "/b": "repo:/b"}
    assert sorted(calls) == ["/a", "/b"]


def test_records_to_session_uses_pre_resolved_repo(monkeypatch) -> None:
    from trace2rules.ingest.sources import agent_trajectory

    def fail(cwd):
        raise AssertionError("git lookup should not run for a pre-resolved cwd")

    monkeypatch.setattr(agent_trajectory, "_repo_from_cwd", fail)
    session = _records_to_session(
        Path("session.jsonl"),
        "omp",
        [{"role": "meta", "cwd": "/work/repo"}],
        None,
        repos={"/work/repo": "acme/repo"},
    )
    assert session.repo == "acme/repo"
