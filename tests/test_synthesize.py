from __future__ import annotations

from trace2rules.mining import Finding
from trace2rules.synthesis.prompt import _client
from trace2rules.synthesis.selection import _sequence_tokens, select_findings


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
    assert "Return only copyable Markdown rules" in prompt
    assert "Do not encode memories" in prompt




def test_synthesize_wrapper_caps_rules(monkeypatch) -> None:
    from trace2rules.synthesis import service
    from trace2rules.synthesis.models import Rule, Synthesis

    rules = [
        Rule(
            category="convention",
            rule_markdown=f"rule {index}",
            evidence_summary="evidence",
            estimated_tokens_saved_per_session=1,
        )
        for index in range(12)
    ]
    monkeypatch.setattr(
        service,
        "_llm_prompt",
        lambda prompt: Synthesis(repo="acme/repo", rules=rules),
    )
    assert len(service.synthesize_report("# Repository: acme/repo").rules) == 10
    assert len(service.synthesize_report("# Repository: acme/repo", max_rules=3).rules) == 3
    monkeypatch.setenv("TRACE2RULES_MAX_RULES", "4")
    assert len(service.synthesize_report("# Repository: acme/repo").rules) == 4


def test_synthesize_adds_json_contract(monkeypatch) -> None:
    from trace2rules.synthesis import service

    captured = {}

    def fake(prompt):
        captured["prompt"] = prompt
        return None

    monkeypatch.setattr(service, "_llm_prompt", fake)
    try:
        service.synthesize_report("# Repository: acme/repo")
    except RuntimeError:
        pass
    assert "Machine-readable output" in captured["prompt"]
    assert "Return JSON only" in captured["prompt"]
