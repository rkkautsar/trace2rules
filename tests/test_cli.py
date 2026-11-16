from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trace2rules.cli import app
from trace2rules.db import connect
from trace2rules.ingest import write_sessions
from trace2rules.models import Session, ToolCall

runner = CliRunner()


def test_help_loads() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output


def test_unknown_ingest_format_is_a_usage_error() -> None:
    result = runner.invoke(app, ["ingest", "--format", "nope"])
    assert result.exit_code == 2
    assert "Unknown format" in result.output


def test_synthesize_out_requires_repo() -> None:
    result = runner.invoke(app, ["synthesize", "--out", "AGENTS.proposed.md"])
    assert result.exit_code == 2


def test_patterns_on_fresh_db_is_friendly(tmp_path: Path) -> None:
    db = tmp_path / "fresh.duckdb"
    con = connect(db)
    con.close()
    result = runner.invoke(app, ["patterns", "--db", str(db)])
    assert result.exit_code == 1
    assert "No labels" in result.output


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


def test_mine_renders_unknown_resolution_as_na(tmp_path: Path) -> None:
    db = tmp_path / "unknown.duckdb"
    write_sessions(db, [Session("s1", "acme/repo", source="swe-chat")])
    result = runner.invoke(app, ["mine", "--db", str(db)])
    assert result.exit_code == 0
    assert "n/a" in result.output


def test_bad_swebench_source_is_friendly() -> None:
    result = runner.invoke(
        app,
        ["ingest", "--source", "/tmp/no-such-trace2rules.parquet", "--limit", "1"],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
