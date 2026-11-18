from pathlib import Path

from trace2rules.agents import audit_agents, parse_agents, parse_agents_text
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


def test_workaround_detector_is_not_cancelled(tmp_path: Path) -> None:
    from trace2rules.detectors import workaround_scripts

    db = tmp_path / "workaround.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s",
                "acme/repo",
                calls=[
                    ToolCall(
                        "Edit",
                        "reproduce_issue.py",
                        action_type="edit",
                        target="reproduce_issue.py",
                        object_id="reproduce_issue.py",
                    ),
                ],
            )
        ],
    )
    con = connect(db)
    try:
        findings = workaround_scripts(con)
    finally:
        con.close()
    assert findings
    assert findings[0].sessions == 1


def test_audit_since_filters_sessions(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `pytest tests/test_api.py` after editing.\n")
    db = tmp_path / "since.duckdb"
    sessions = [
        Session(
            "old",
            "acme/repo",
            started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")],
        ),
        Session(
            "new",
            "acme/repo",
            started_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")],
        ),
    ]
    write_sessions(db, sessions)
    con = connect(db)
    try:
        audits = audit_agents(
            con,
            "acme/repo",
            parse_agents(path),
            since=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    finally:
        con.close()
    assert audits[0].applicable_sessions == 1


def test_resolve_agents_detects_claude_md(tmp_path: Path, monkeypatch) -> None:
    from trace2rules.agents import resolve_agents

    (tmp_path / "CLAUDE.md").write_text("- Run `pytest tests/test_api.py` after editing.\n")
    monkeypatch.chdir(tmp_path)
    assert [path.name for path in resolve_agents()] == ["CLAUDE.md"]


def test_parse_agents_collects_shell_commands(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "## Testing\n"
        "```bash\n"
        "make check\n"
        "```\n"
        "## Architecture\n"
        "```bash\n"
        "cat README.md\n"
        "```\n"
    )
    rules = parse_agents(path)
    assert [rule.commands for rule in rules] == [("make check",)]


def test_discover_enforcement_finds_make_and_ci(tmp_path: Path) -> None:
    from trace2rules.agents import discover_enforcement

    (tmp_path / "Makefile").write_text("check:\n\tpytest\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("run: make check\n")
    rule = parse_agents_text("- Run `make check` after editing.")[0]
    status, source = discover_enforcement(tmp_path, rule)
    assert status == "configured"
    assert "ci.yml" in source


def test_discover_enforcement_marks_make_target_available(tmp_path: Path) -> None:
    from trace2rules.agents import discover_enforcement

    (tmp_path / "Makefile").write_text("check:\n\tpytest\n")
    rule = parse_agents_text("- Run `make check` after editing.")[0]
    status, source = discover_enforcement(tmp_path, rule)
    assert status == "available"
    assert "Makefile" in source


def test_parse_agents_accepts_document_title_sections() -> None:
    rules = parse_agents_text("# AGENTS.md\n- Run `pytest` before committing.\n")
    assert [rule.text for rule in rules] == ["Run `pytest` before committing."]


def test_context_hygiene_without_commands_detects_scratch(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Do not commit scratch files.\n")
    db = tmp_path / "hygiene.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s",
                "acme/repo",
                calls=[
                    ToolCall(
                        "Edit",
                        "reproduce_issue.py",
                        action_type="edit",
                        target="reproduce_issue.py",
                        object_id="reproduce_issue.py",
                    )
                ],
            )
        ],
    )
    con = connect(db)
    try:
        audits = audit_agents(con, "acme/repo", parse_agents(path))
    finally:
        con.close()
    assert audits[0].violation_sessions == 1


def test_audit_not_observed_is_session_scoped(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `pytest` after editing.\n")
    db = tmp_path / "counts.duckdb"
    sessions = []
    for sid, validate in (("edit-only", False), ("edit-test", True), ("test-only", True)):
        calls = [
            ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")
        ]
        if validate:
            calls.append(
                ToolCall(
                    "Bash",
                    "pytest",
                    action_type="exec",
                    target="pytest",
                    object_id="pytest",
                    is_error=False,
                )
            )
        if sid == "test-only":
            calls = calls[1:]
        sessions.append(Session(sid, "acme/repo", calls=calls))
    write_sessions(db, sessions)
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path))[0]
    finally:
        con.close()
    assert audit.applicable_sessions == 2
    assert audit.exercised_sessions == 1
    assert audit.compliant_sessions == 1
    assert audit.violation_sessions == 1
    assert audit.not_observed_sessions == 1


def test_enforcement_ignores_backticked_paths(tmp_path: Path) -> None:
    from trace2rules.agents import discover_enforcement

    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "run: coverage report --include=src/api.py\n"
    )
    rule = parse_agents_text("- Keep `src/api.py` free of CLI imports.")[0]
    status, _source = discover_enforcement(tmp_path, rule)
    assert status != "configured"


def test_parse_agents_dedupes_bullet_and_shell_block() -> None:
    rules = parse_agents_text(
        "## Testing\n- Run `make check`\n\n## Testing\n```bash\nmake check\n```\n"
    )
    assert [rule.text for rule in rules] == ["Run `make check`"]


def test_parse_agents_joins_continued_shell_lines() -> None:
    rules = parse_agents_text(
        "## Testing\n```bash\npytest \\\n  tests/test_api.py -q\n```\n"
    )
    assert rules[0].commands == ("pytest tests/test_api.py -q",)


def test_audit_infers_test_scope_from_test_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "api.py").write_text("")
    (root / "tests" / "test_api.py").write_text("")
    path = root / "AGENTS.md"
    path.write_text("- Run `pytest tests/test_api.py` after editing.\n")
    db = tmp_path / "scope.duckdb"
    write_sessions(
        db,
        [
            Session(
                "api",
                "acme/repo",
                calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")],
            ),
            Session(
                "other",
                "acme/repo",
                calls=[ToolCall("Edit", "src/other.py", action_type="edit", target="src/other.py")],
            ),
        ],
    )
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path), root=root)[0]
    finally:
        con.close()
    assert audit.scope == "src/api.py"
    assert audit.applicable_sessions == 1


def test_infer_scope_handles_nested_test_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "backend" / "src").mkdir(parents=True)
    (root / "backend" / "tests").mkdir()
    (root / "backend" / "src" / "api.py").write_text("")
    (root / "backend" / "tests" / "test_api.py").write_text("")
    path = root / "AGENTS.md"
    path.write_text("- Run `pytest backend/tests/test_api.py` after editing.\n")
    rule = parse_agents(path)[0]
    from trace2rules.agents import _infer_scope

    assert _infer_scope(rule, root) == ("backend/src/api.py",)


def test_audit_exposes_opportunities_and_rate(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `pytest tests/test_api.py` after editing.\n")
    db = tmp_path / "rate.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s",
                "acme/repo",
                calls=[
                    ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py"),
                    ToolCall(
                        "Bash",
                        "pytest tests/test_api.py",
                        action_type="exec",
                        target="pytest tests/test_api.py",
                        is_error=False,
                    ),
                ],
            )
        ],
    )
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path))[0]
    finally:
        con.close()
    assert audit.detector == "test-after-change"
    assert audit.measurable is True
    assert audit.opportunities == 1
    assert audit.followed == 1
    assert audit.rate is None


def test_audit_marks_unobservable_rules(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "- Never commit secrets.\n"
        "- Use comments for complex logic only.\n"
        "- Run `pytest tests/test_api.py` after editing.\n"
    )
    db = tmp_path / "coverage.duckdb"
    write_sessions(
        db,
        [Session("s", "acme/repo", calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")])],
    )
    con = connect(db)
    try:
        audits = audit_agents(con, "acme/repo", parse_agents(path))
    finally:
        con.close()
    by_text = {audit.text: audit for audit in audits}
    assert by_text["Never commit secrets."].coverage == "dark"
    assert by_text["Use comments for complex logic only."].coverage == "unmeasured"
    assert by_text["Run `pytest tests/test_api.py` after editing."].coverage == "measured"


def test_audit_opportunity_partition_and_path_commands(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `pytest` after editing `src/api.py`.\n")
    db = tmp_path / "partition.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s",
                "acme/repo",
                calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")],
            )
        ],
    )
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path))[0]
    finally:
        con.close()
    assert audit.exercised_sessions == 0
    assert audit.opportunities == audit.followed + audit.violation_sessions + audit.undecided
    assert audit.violation_sessions == 1


def test_parse_agents_reads_validation_code_block(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("## Validation\n```bash\nuv run pytest -q\n```\n")
    rules = parse_agents(path)
    assert [rule.commands for rule in rules] == [("uv run pytest -q",)]


def test_enforcement_requires_command_boundary(tmp_path: Path) -> None:
    from trace2rules.agents import discover_enforcement

    (tmp_path / "package.json").write_text('{"scripts":{"test":"npm run build"}}')
    rule = parse_agents_text("- Run `pytest tests/test_api.py` after editing.")[0]
    status, source = discover_enforcement(tmp_path, rule)
    assert status != "configured"


def test_infer_scope_indexes_typescript(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "api.ts").write_text("")
    (root / "tests" / "test_api.ts").write_text("")
    path = root / "AGENTS.md"
    path.write_text("- Run `npm test -- tests/test_api.ts` after editing.\n")
    from trace2rules.agents import _infer_scope

    assert _infer_scope(parse_agents(path)[0], root) == ("src/api.ts",)


def test_matches_command_placeholders() -> None:
    from trace2rules.agents import _matches

    rule = parse_agents_text("- Run `cargo test -p <crate> --offline` after editing.")[0]
    assert _matches(rule, "cargo test -p swc_ecma_parser --offline", "", "")


def test_repo_matches_cwd_suffix(monkeypatch, tmp_path: Path) -> None:
    from trace2rules.cli import _repo_matches_cwd

    monkeypatch.chdir(tmp_path)
    assert _repo_matches_cwd(f"owner/{tmp_path.name}")
    assert _repo_matches_cwd(tmp_path.name)
    assert not _repo_matches_cwd("owner/other")


def test_enforcement_requires_workflow_run_block(tmp_path: Path) -> None:
    from trace2rules.agents import discover_enforcement

    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("# pytest\nname: ci\n")
    rule = parse_agents_text("- Run `pytest tests/test_api.py` after editing.")[0]
    status, _source = discover_enforcement(tmp_path, rule)
    assert status != "configured"
    (workflow / "ci.yml").write_text("jobs:\n  test:\n    steps:\n      - run: pytest tests/test_api.py\n")
    status, source = discover_enforcement(tmp_path, rule)
    assert status == "configured"
    assert "ci.yml" in source


def test_command_matching_is_argv_aware() -> None:
    from trace2rules.agents import _matches

    rule = parse_agents_text("- Run `gh pr view` when checking PRs.")[0]
    assert _matches(rule, "gh pr view 42", "", "")
    assert not _matches(rule, "github-tool pr view 42", "", "")


def test_audit_exposes_confidence(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("- Run `pytest tests/test_api.py` after editing.\n")
    db = tmp_path / "confidence.duckdb"
    write_sessions(
        db,
        [Session("s", "acme/repo", calls=[ToolCall("Edit", "src/api.py", action_type="edit", target="src/api.py")])],
    )
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path))[0]
    finally:
        con.close()
    assert audit.confidence == "low"



def test_audit_reports_stale_paths_and_conditional_wording(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("## Testing\n\n- Run `pytest tests/test_missing.py` when tests change.\n")
    db = tmp_path / "stale.duckdb"
    write_sessions(db, [Session("s", "acme/repo", calls=[ToolCall("Read", "src/api.py")])])
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path), root=tmp_path)[0]
    finally:
        con.close()
    assert audit.wording == "conditional"
    assert audit.stale_paths == ("tests/test_missing.py",)


def test_stale_paths_ignore_markdown_links(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("## Links\n\n- Use [owner/repo](https://github.com/owner/repo) for issues.\n")
    db = tmp_path / "links.duckdb"
    write_sessions(db, [Session("s", "acme/repo")])
    con = connect(db)
    try:
        audit = audit_agents(con, "acme/repo", parse_agents(path), root=tmp_path)[0]
    finally:
        con.close()
    assert audit.stale_paths == ()
