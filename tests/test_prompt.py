from __future__ import annotations

from trace2rules.mining import Finding
from trace2rules.synthesis.selection import _sequence_tokens, select_findings


def test_nested_exact_sequences_keep_the_longer_workflow() -> None:
    short = Finding(
        kind="exact",
        repo="acme/repo",
        title="",
        sessions=4,
        tokens=100,
        cost=400,
        paired_instances=2,
        delta_rework=1.0,
        sequence="s1:exec:bash:a:err | s2:edit:edit:b:ok | s3:exec:bash:c:ok",
    )
    long = Finding(
        kind="exact",
        repo="acme/repo",
        title="",
        sessions=3,
        tokens=100,
        cost=300,
        paired_instances=2,
        delta_rework=1.0,
        sequence="s1:exec:bash:a:err | s2:edit:edit:b:ok | s3:exec:bash:c:ok | s4:exec:bash:d:ok",
    )
    selected = select_findings(
        [short, long],
        {"recovery": [], "hotspots": [], "context": [], "clusters": []},
        "acme/repo",
    )
    assert len(selected) == 1
    assert selected[0].sequence == long.sequence


def test_generic_pair_never_beats_its_superset() -> None:
    pair = Finding(
        kind="exact",
        repo="acme/repo",
        title="",
        sessions=10,
        tokens=100,
        cost=1000,
        paired_instances=2,
        delta_rework=1.0,
        sequence="s1:read:read:src/a.py:ok | s2:edit:edit:src/a.py:ok",
    )
    triple = Finding(
        kind="exact",
        repo="acme/repo",
        title="",
        sessions=4,
        tokens=100,
        cost=400,
        paired_instances=2,
        delta_rework=1.0,
        sequence="s1:read:read:src/a.py:ok | s2:edit:edit:src/a.py:ok | s3:exec:pytest:pytest:err",
    )
    selected = select_findings(
        [pair, triple],
        {"recovery": [], "hotspots": [], "context": [], "clusters": []},
        "acme/repo",
    )
    assert len(selected) == 1
    assert selected[0].sequence == triple.sequence


def test_pipe_text_is_not_a_step_boundary() -> None:
    finding = Finding(
        kind="exact",
        repo="acme/repo",
        title="",
        sessions=3,
        tokens=100,
        sequence="s1:exec:bash:npm test 2>&1 | tail -5:err | s2:edit:edit:src/a.py:ok",
    )
    assert len(_sequence_tokens(finding)) == 2


def test_prompt_omits_detector_recommendations() -> None:
    from trace2rules.mining import Finding
    from trace2rules.synthesis.prompt import _prompt

    finding = Finding(
        kind="context",
        repo="acme/repo",
        title="make unit",
        sessions=10,
        tokens=100,
        side="repo",
        recommendation="ADD A TEST-ONE TARGET",
        detail="exec via execute_bash",
    )
    prompt = _prompt([finding], "acme/repo")
    assert "ADD A TEST-ONE TARGET" not in prompt
    assert "### 1. context: make unit" in prompt
    assert "confidence medium" in prompt
    assert '"confidence":' not in prompt
    assert "Return copyable Markdown rules first" in prompt
    assert "## Skill candidates" in prompt
    assert "not AGENTS.md content" in prompt
    assert "untrusted data" in prompt
    assert "Do not encode memories" in prompt
    assert "prevents the failure before it happens" in prompt
    assert "Do not restate the obvious recovery step" in prompt


def test_pattern_window_includes_successful_recovery() -> None:
    from trace2rules.mining import Finding
    from trace2rules.synthesis.prompt import _prompt

    window = tuple(
        {
            "step": step,
            "action": "exec",
            "verb": "pytest",
            "outcome": "err" if step < 5 else "ok",
            "session_ref": "session-1",
        }
        for step in range(1, 6)
    )
    prompt = _prompt(
        [
            Finding(
                kind="attribute_pattern",
                repo="acme/repo",
                title="exec:pytest:err -> edit:test:ok -> exec:pytest:ok",
                sessions=3,
                tokens=100,
                window=window,
            )
        ],
        "acme/repo",
    )
    assert "step 5 · exec pytest — ok" in prompt


def test_selection_dedupes_prevention_against_setup_heuristic() -> None:
    from trace2rules.mining import Finding
    from trace2rules.synthesis.selection import select_findings

    prevention = Finding(
        kind="prevention_delta",
        repo="acme/repo",
        title="pip install -e . before pytest",
        sessions=12,
        tokens=1000,
        paired_instances=4,
    )
    environment = Finding(
        kind="environment_thrash",
        repo="acme/repo",
        title="repeated dependency/environment setup",
        sessions=30,
        tokens=5000,
    )
    selected = select_findings(
        [],
        {"prevention_delta": [prevention], "environment_thrash": [environment]},
        "acme/repo",
    )
    assert [finding.kind for finding in selected] == ["prevention_delta"]


def test_selection_dedupes_same_prevention_action_across_entities() -> None:
    from trace2rules.mining import Finding
    from trace2rules.synthesis.selection import select_findings

    first = Finding(
        kind="prevention_delta",
        repo="acme/repo",
        title="pip install -e . before pytest",
        sessions=12,
        tokens=1000,
    )
    second = Finding(
        kind="prevention_delta",
        repo="acme/repo",
        title="pip install -e . before python",
        sessions=8,
        tokens=500,
    )
    selected = select_findings(
        [],
        {"prevention_delta": [first, second]},
        "acme/repo",
    )
    assert [finding.kind for finding in selected] == ["prevention_delta"]



def test_existing_audit_verdict_is_rendered() -> None:
    from trace2rules.synthesis.prompt import _prompt

    prompt = _prompt(
        [],
        "acme/repo",
        existing={
            "sessions": 1,
            "rules": [
                {
                    "text": "Read the target before editing.",
                    "verdict": "delete",
                    "stale_paths": ["src/old.py"],
                }
            ],
        },
    )
    assert "verdict delete" in prompt
    assert "If its verdict is `delete`, omit it." in prompt


def test_synthesis_proposal_markdown_is_parseable() -> None:
    from trace2rules.audit.parsing import parse_agents_text

    text = """## architecture_map
- **Rule:** Keep `src/cli.py` limited to wiring.
  **Evidence:** hotspot.
  **Enforcement:** guidance only.
  **Source:** `session-1` (step 1)

## context_hygiene
- **Rule:** Before editing a target file, read that file or confirm it does not exist.
  **Evidence:** blind edits.
  **Enforcement:** guidance only.
  **Source:** `session-1` (step 2)
"""
    rules = parse_agents_text(text, "proposal.md")
    assert [rule.category for rule in rules] == ["architecture_map", "read_before_edit"]


def test_numbered_rule_does_not_crash_parser() -> None:
    from trace2rules.audit.parsing import parse_agents_text

    rules = parse_agents_text("# Rules\n1. Run `pytest` now.")
    assert len(rules) == 1
    assert rules[0].category == "test_command"


def test_proposal_scope_is_repo_when_file_is_outside_checkout(tmp_path: Path) -> None:
    from trace2rules.audit.engine import audit_payload
    from trace2rules.db import connect
    from trace2rules.ingest import write_sessions
    from trace2rules.models import Session

    db = tmp_path / "scope.duckdb"
    write_sessions(db, [Session("s", "acme/repo")])
    proposal = tmp_path / "AGENTS.proposed.md"
    proposal.write_text("## Validation\n\n- Run `pytest tests/test_api.py` after editing.\n")
    con = connect(db)
    try:
        payload = audit_payload(
            con,
            "acme/repo",
            [proposal],
            root=tmp_path,
            source_scope="repo",
        )
    finally:
        con.close()
    assert [rule["source_scope"] for rule in payload["rules"]] == ["repo"]


def test_audit_prompt_omits_deterministic_suggestions() -> None:
    from trace2rules.synthesis.prompt import _prompt

    prompt = _prompt(
        [],
        "acme/repo",
        existing={
            "sessions": 1,
            "rules": [
                {
                    "text": "Run `pytest` after edits.",
                    "verdict": "enforce",
                    "suggestion": "MOVE THIS INTO CI",
                }
            ],
        },
    )
    assert "MOVE THIS INTO CI" not in prompt
