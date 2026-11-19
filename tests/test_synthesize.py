from __future__ import annotations

from trace2rules.db import connect
from trace2rules.mining import Finding
from trace2rules.ingest import write_sessions
from trace2rules.models import Session
from trace2rules.synthesis.prompt import _client
from trace2rules.synthesis.selection import _sequence_tokens, select_findings
from trace2rules.synthesis.service import synthesize


def test_custom_llm_key_uses_explicit_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("TRACE2RULES_LLM_API_KEY", "test-key")
    monkeypatch.setenv("TRACE2RULES_BASE_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("TRACE2RULES_MODEL", "test-model")
    client, model = _client()
    assert client is not None
    assert model == "test-model"


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


def test_repo_without_findings_is_skipped(tmp_path, monkeypatch) -> None:
    db = tmp_path / "empty.duckdb"
    write_sessions(db, [Session("s1", "acme/empty", source="test")])
    con = connect(db)
    called = False

    def fake_llm(findings, repo, labels):
        nonlocal called
        called = True
        raise AssertionError("LLM should not run without findings")

    monkeypatch.setattr("trace2rules.synthesis.service._llm_synthesis", fake_llm)
    results = synthesize(con)
    con.close()
    assert results == []
    assert called is False


def test_rule_with_unknown_source_is_dropped() -> None:
    from trace2rules.mining import Finding
    from trace2rules.synthesis.models import Rule, Synthesis
    from trace2rules.synthesis.offline import _validate_sources

    finding = Finding(
        kind="recovery",
        repo="acme/repo",
        title="a -> b",
        sessions=3,
        tokens=10,
        window=({"session_ref": "session.jsonl", "step": 4},),
    )
    synthesis = Synthesis(
        repo="acme/repo",
        rules=[
            Rule(
                category="convention",
                rule_markdown="Do a.",
                evidence_summary="3 sessions",
                estimated_tokens_saved_per_session=1,
                sources=["invented.jsonl (step 4)"],
            ),
            Rule(
                category="convention",
                rule_markdown="Do b.",
                evidence_summary="3 sessions",
                estimated_tokens_saved_per_session=1,
                sources=["session.jsonl (step 4)"],
            ),
        ],
    )
    validated = _validate_sources(synthesis, [finding])
    assert [rule.rule_markdown for rule in validated.rules] == ["Do b."]


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
    prompt = _prompt([finding], "acme/repo", [])
    assert "ADD A TEST-ONE TARGET" not in prompt
    assert '"confidence": "medium"' in prompt


def test_rule_provenance_is_attached_from_evidence() -> None:
    from trace2rules.synthesis.models import Rule, Synthesis
    from trace2rules.synthesis.offline import _attach_rule_provenance

    finding = Finding(
        kind="recovery",
        repo="acme/repo",
        title="a -> b",
        sessions=12,
        tokens=100,
        paired_instances=3,
        window=({"session_ref": "session.jsonl", "step": 4},),
    )
    synthesis = Synthesis(
        repo="acme/repo",
        rules=[
            Rule(
                category="convention",
                rule_markdown="Do a.",
                evidence_summary="12 sessions",
                estimated_tokens_saved_per_session=1,
                sources=["session.jsonl (step 4)"],
            )
        ],
    )
    result = _attach_rule_provenance(synthesis, [finding])
    assert result.rules[0].support_sessions == 12
    assert result.rules[0].support_instances == 3
    assert result.rules[0].confidence == "high"
    assert result.rules[0].finding_kinds == ["recovery"]


def test_llm_rules_require_sources() -> None:
    from trace2rules.synthesis.models import Rule, Synthesis
    from trace2rules.synthesis.offline import _validate_sources

    synthesis = Synthesis(
        repo="acme/repo",
        rules=[
            Rule(
                category="convention",
                rule_markdown="Do a.",
                evidence_summary="evidence",
                estimated_tokens_saved_per_session=1,
            )
        ],
    )
    assert _validate_sources(synthesis, [], require_sources=True).rules == []
    assert len(_validate_sources(synthesis, []).rules) == 1
