from __future__ import annotations

from trace2rules.db import connect
from trace2rules.detectors import Finding
from trace2rules.ingest import write_sessions
from trace2rules.models import Session
from trace2rules.synthesize import _client, _sequence_tokens, select_findings, synthesize


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

    monkeypatch.setattr("trace2rules.synthesize._llm_synthesis", fake_llm)
    results = synthesize(con, out_dir=tmp_path)
    con.close()
    assert results == []
    assert called is False
    assert not list(tmp_path.glob("AGENTS.*.proposed.md"))
