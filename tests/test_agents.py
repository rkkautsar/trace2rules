from pathlib import Path

from trace2rules.agents import audit_agents, parse_agents
from trace2rules.db import connect
from trace2rules.ingest import write_sessions
from trace2rules.models import Session, ToolCall


def test_parse_agents_extracts_commands_and_categories(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "# Rules\n"
        "- Run `python -m pytest tests/test_api.py` after editing.\n"
        "- Keep `src/api.py` free of CLI imports.\n"
        "- Never add `reproduce_issue.py` at the root.\n"
    )
    rules = parse_agents(path)
    assert [rule.category for rule in rules] == [
        "test_command",
        "architecture_map",
        "context_hygiene",
    ]
    assert rules[0].commands == ("python -m pytest tests/test_api.py",)


def test_audit_counts_exercised_and_violated_sessions(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `python -m pytest tests/test_api.py` after editing.\n")
    db = tmp_path / "audit.duckdb"
    write_sessions(
        db,
        [
            Session(
                "good",
                "acme/repo",
                calls=[
                    ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py", object_id="src/api.py"),
                    ToolCall(
                        "Bash",
                        "python -m pytest tests/test_api.py",
                        action_type="exec",
                        target="python -m pytest tests/test_api.py",
                        object_id="pytest",
                        is_error=False,
                    ),
                ],
            ),
            Session(
                "bad",
                "acme/repo",
                calls=[
                    ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py", object_id="src/api.py"),
                ],
            ),
        ],
    )
    con = connect(db)
    try:
        audits = audit_agents(con, "acme/repo", parse_agents(path))
    finally:
        con.close()
    assert len(audits) == 1
    assert audits[0].exercised_sessions == 1
    assert audits[0].applicable_sessions == 2
    assert audits[0].violation_sessions == 1
