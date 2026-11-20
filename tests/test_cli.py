from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from trace2rules.cli import app
from trace2rules.mining.sequences import friction_windows
from trace2rules.db import connect
from trace2rules.ingest import IngestStats, write_sessions
from trace2rules.models import Session, ToolCall
from trace2rules.patterns import mine_sequences

runner = CliRunner()


def test_help_loads() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output


def test_unknown_ingest_format_is_a_usage_error() -> None:
    result = runner.invoke(app, ["ingest", "--format", "nope"])
    assert result.exit_code == 2
    assert "Unknown format" in result.output


def test_patterns_on_fresh_db_is_friendly(tmp_path: Path) -> None:
    db = tmp_path / "fresh.duckdb"
    con = connect(db)
    con.close()
    result = runner.invoke(app, ["patterns", "--db", str(db)])
    assert result.exit_code == 1
    assert "No sessions" in result.output


def test_patterns_use_normalized_action_tokens(tmp_path: Path) -> None:
    db = tmp_path / "patterns.duckdb"
    sessions = []
    for index in range(3):
        sessions.append(
            Session(
                f"s{index}",
                "acme/repo",
                source="test",
                calls=[
                    ToolCall(
                        "Bash",
                        "make test",
                        observation="error",
                        action_type="exec",
                        target="make test",
                        verb="make",
                        object_id="make test",
                        is_error=True,
                    ),
                    ToolCall(
                        "Read",
                        "src/a.py",
                        observation="contents",
                        action_type="read",
                        target="src/a.py",
                        verb="read",
                        object_id="src/a.py",
                    ),
                    ToolCall(
                        "Bash",
                        "make test",
                        observation="error",
                        action_type="exec",
                        target="make test",
                        verb="make",
                        object_id="make test",
                        is_error=True,
                    ),
                ],
            )
        )
    write_sessions(db, sessions)
    con = connect(db)
    try:
        patterns = mine_sequences(con, top=5)
    finally:
        con.close()
    assert patterns
    assert any(
        "exec:make:err" in pattern.sequence and "read:source:ok" in pattern.sequence
        for pattern in patterns
    )


def test_mine_accepts_bracket_targets(tmp_path: Path) -> None:
    db = tmp_path / "brackets.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="test",
                calls=[
                    ToolCall(
                        "Read",
                        "[/red]",
                        observation="x",
                        action_type="read",
                        target="[/red]",
                        verb="read",
                        object_id="[/red]",
                    )
                ],
            )
        ],
    )
    result = runner.invoke(app, ["mine", "--db", str(db)])
    assert result.exit_code == 0


def test_mine_does_not_render_resolution(tmp_path: Path) -> None:
    db = tmp_path / "unknown.duckdb"
    write_sessions(db, [Session("s1", "acme/repo", source="swe-chat")])
    result = runner.invoke(app, ["mine", "--db", str(db)])
    assert result.exit_code == 0
    assert "Resolved" not in result.output
    assert "n/a" not in result.output


def test_bad_swebench_source_is_friendly() -> None:
    result = runner.invoke(
        app,
        [
            "ingest",
            "--format",
            "swe-rebench",
            "--source",
            "/tmp/no-such-trace2rules.parquet",
            "--limit",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_ingest_defaults_to_home_agent(monkeypatch) -> None:
    import trace2rules.ingest.sources.agent_trajectory as agent_source

    calls: list[str] = []

    def fake_ingest(source, db_path, *, limit=None, repo=None, progress=None, split_compactions=True):
        calls.append(str(source))
        return [IngestStats(str(source), 0, 0, 0)]

    monkeypatch.setattr(agent_source, "ingest_agent_sessions", fake_ingest)
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0
    assert calls == [str(Path.home())]


def test_ingest_accepts_repeated_sources(monkeypatch) -> None:
    import trace2rules.ingest.sources.agent_trajectory as agent_source

    calls: list[str] = []

    def fake_ingest(source, db_path, *, limit=None, repo=None, progress=None, split_compactions=True):
        calls.append(str(source))
        return [IngestStats(str(source), 0, 0, 0)]

    monkeypatch.setattr(agent_source, "ingest_agent_sessions", fake_ingest)
    result = runner.invoke(
        app,
        [
            "ingest",
            "--format",
            "agent-trajectory",
            "--source",
            "/a",
            "--source",
            "/b",
        ],
    )
    assert result.exit_code == 0
    assert calls == ["/a", "/b"]


def test_windows_stop_at_segment_boundary(tmp_path: Path) -> None:
    db = tmp_path / "segments.duckdb"
    calls = [
        ToolCall(
            "Bash",
            "git status",
            observation="fatal",
            action_type="exec",
            target="git status",
            verb="git",
            object_id="status",
            is_error=True,
            segment_idx=0,
        ),
        ToolCall(
            "Read",
            "src/a.py",
            observation="contents",
            action_type="read",
            target="src/a.py",
            verb="read",
            object_id="src/a.py",
            segment_idx=0,
        ),
        ToolCall(
            "Read",
            "src/b.py",
            observation="contents",
            action_type="read",
            target="src/b.py",
            verb="read",
            object_id="src/b.py",
            segment_idx=1,
        ),
    ]
    write_sessions(db, [Session("s1", "acme/repo", source="test", calls=calls)])
    con = connect(db)
    try:
        assert friction_windows(con, lengths=(3,)) == []
    finally:
        con.close()


def test_ingest_reads_opencode_sqlite(tmp_path: Path) -> None:
    source = tmp_path / "opencode.db"
    con = sqlite3.connect(source)
    con.execute(
        "CREATE TABLE session_v2 (id TEXT, parent_id TEXT, directory TEXT, title TEXT)"
    )
    con.execute(
        "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, data TEXT)"
    )
    con.execute("INSERT INTO session_v2 VALUES ('s1', NULL, '/repo', 'task')")
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
    con.commit()
    con.close()
    db = tmp_path / "trace.duckdb"
    result = runner.invoke(
        app,
        [
            "ingest",
            "--format",
            "agent-trajectory",
            "--source",
            str(source),
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 0
    assert "opencode" in result.output
    con = connect(db)
    try:
        assert con.execute("SELECT target FROM tool_events").fetchone() == ("git status",)
    finally:
        con.close()


def test_report_emits_machine_readable_evidence(tmp_path: Path) -> None:
    db = tmp_path / "report.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="test",
                calls=[
                    ToolCall(
                        "Read",
                        "src/a.py",
                        observation="contents",
                        action_type="read",
                        target="src/a.py",
                        verb="read",
                        object_id="src/a.py",
                    )
                ],
            )
        ],
    )
    result = runner.invoke(app, ["report", "--db", str(db)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["sessions"] == 1
    assert payload["instances"] == 1
    assert payload["repos"] == [{"repo": "acme/repo", "sessions": 1}]
    assert isinstance(payload["findings"], list)


def test_doctor_reports_environment(tmp_path: Path, monkeypatch) -> None:
    result = runner.invoke(
        app,
        ["doctor", "--db", str(tmp_path / "pending.duckdb")],
    )
    assert result.exit_code == 0
    assert "Python" in result.output
    assert "database directory" in result.output


def test_doctor_survives_a_node_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("trace2rules.cli.shutil.which", lambda _: "/usr/local/bin/node")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["node", "--version"], 5)

    monkeypatch.setattr("trace2rules.cli.subprocess.run", timeout)
    result = runner.invoke(
        app,
        ["doctor", "--db", str(tmp_path / "pending.duckdb")],
    )
    assert result.exit_code == 0
    assert "Node.js" in result.output
    assert "unavailable" in result.output


def test_ingest_continues_after_a_bad_source(monkeypatch) -> None:
    import trace2rules.ingest.sources.agent_trajectory as agent_source

    def fake_ingest(source, db_path, *, limit=None, repo=None, progress=None, split_compactions=True):
        if str(source) == "/bad":
            raise FileNotFoundError("missing source")
        return [IngestStats(str(source), 1, 1, 1)]

    monkeypatch.setattr(agent_source, "ingest_agent_sessions", fake_ingest)
    result = runner.invoke(
        app,
        [
            "ingest",
            "--source",
            "/bad",
            "--source",
            "/good",
        ],
    )
    assert result.exit_code == 0
    assert "Failed /bad" in result.output
    assert "/good" in result.output


def test_report_does_not_audit_cwd_agents_for_other_repo(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "scope.duckdb"
    write_sessions(
        db,
        [Session("s1", "acme/other", source="test", calls=[ToolCall("Read", "src/a.py", action_type="read")])],
    )
    (tmp_path / "AGENTS.md").write_text("## Validation\n\n- CWD_RULE_SENTINEL\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["report", "--repo", "acme/other", "--prompt", "--db", str(db)],
    )
    assert result.exit_code == 0
    assert "CWD_RULE_SENTINEL" not in result.output


def test_report_uses_session_cwd_for_agents(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "AGENTS.md").write_text("## Validation\n\n- Run `CWD_RULE_SENTINEL` after editing.\n")
    db = tmp_path / "cwd.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                source="test",
                cwd=str(checkout),
                calls=[ToolCall("Read", "src/a.py", action_type="read")],
            )
        ],
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["report", "--repo", "acme/repo", "--prompt", "--db", str(db)],
    )
    assert result.exit_code == 0
    assert "CWD_RULE_SENTINEL" in result.output
