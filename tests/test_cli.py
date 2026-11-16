from __future__ import annotations

from typer.testing import CliRunner

from trace2rules.cli import app

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
