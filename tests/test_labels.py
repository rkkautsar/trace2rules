from __future__ import annotations

from trace2rules.db import connect
from trace2rules.mining import Finding
from trace2rules.ingest import write_sessions
from trace2rules.labels import (
    _openai_answers,
    _openai_questions,
    _resolve_model,
    detect_model,
    label_targets,
)
from trace2rules.models import Session, ToolCall
from trace2rules.synthesize import (
    _offline_synthesis,
    _prompt,
    annotate_findings,
    label_summary,
)


def test_decision_model_aliases() -> None:
    assert _resolve_model("clef-flash")["name"] == "clef-flash"
    assert _resolve_model("jev")["provider"] == "cloudflare"
    assert _resolve_model("luna")["name"] == "gpt-6-luna"
    assert _resolve_model("gemini")["name"] == "gemini-decision-1"


def test_openai_decision_payload_and_answers() -> None:
    questions = _openai_questions()
    intent = next(question for question in questions if question["name"] == "intent")
    assert intent["type"] == "choice"
    assert {
        "value": "run_tests",
        "description": "Run pytest or a specific test file/selector",
    } in intent["choices"]

    answers = _openai_answers(
        {
            "answers": [
                {"name": "intent", "choice": "read", "confidence": 0.9},
                {"name": "object_type", "choice": "file", "confidence": 0.8},
            ]
        }
    )
    assert answers["intent"] == {"choice": "read", "confidence": 0.9}
    assert answers["object_type"] == {"choice": "file", "confidence": 0.8}


def test_labels_annotate_findings_and_prompt(tmp_path) -> None:
    db = tmp_path / "labels.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                calls=[
                    ToolCall(
                        "Read",
                        "src/a.py",
                        action_type="read",
                        target="src/a.py",
                        verb="read",
                        object_id="src/a.py",
                    )
                ],
            )
        ],
    )
    con = connect(db)
    try:
        con.execute(
            "INSERT INTO target_labels VALUES "
            "('Read', 'read', 'src/a.py', 'read', 0.9, 'file', 0.8, 'clef-flash', current_timestamp)"
        )
        findings = [
            Finding(
                kind="hotspot",
                repo="acme/repo",
                title="src/a.py",
                sessions=3,
                tokens=100,
            )
        ]
        annotated = annotate_findings(con, findings)
        assert annotated[0].intent == "read"
        assert annotated[0].object_type == "file"
        summary = label_summary(con, "acme/repo")
        assert summary[0]["intent"] == "read"
        prompt = _prompt(annotated, "acme/repo", summary)
        assert "Semantic target labels" in prompt
        assert '"intent": "read"' in prompt
    finally:
        con.close()


def test_offline_synthesis_drops_other_intent() -> None:
    synthesis = _offline_synthesis(
        [
            Finding(
                kind="context",
                repo="acme/repo",
                title="pytest -v",
                sessions=3,
                tokens=100,
                intent="other",
                intent_confidence=0.9,
            )
        ],
        "acme/repo",
    )
    assert synthesis.rules == []


def test_label_targets_uses_selected_backend(tmp_path, monkeypatch) -> None:
    db = tmp_path / "labels.duckdb"
    write_sessions(
        db,
        [
            Session(
                "s1",
                "acme/repo",
                calls=[
                    ToolCall(
                        "Read",
                        "src/a.py",
                        action_type="read",
                        target="src/a.py",
                        verb="read",
                        object_id="src/a.py",
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr("trace2rules.labels._auth_for", lambda spec: "test-key")
    monkeypatch.setattr(
        "trace2rules.labels._post",
        lambda auth, state, spec: {
            "intent": {"choice": "read", "confidence": 0.9},
            "object_type": {"choice": "file", "confidence": 0.8},
        },
    )
    done, failed = label_targets(db, model="gpt-6-luna", rpm=1000, workers=1)
    assert (done, failed) == (1, 0)
    con = connect(db)
    try:
        assert con.execute(
            "SELECT intent, object_type, model FROM target_labels"
        ).fetchone() == ("read", "file", "gpt-6-luna")
    finally:
        con.close()


def test_detect_model_prefers_explicit_setting(monkeypatch) -> None:
    monkeypatch.setenv("TRACE2RULES_DECISION_MODEL", "gpt-6-luna")
    assert detect_model() == "gpt-6-luna"


def test_detect_model_prefers_gemini_key(monkeypatch) -> None:
    monkeypatch.delenv("TRACE2RULES_DECISION_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("trace2rules.labels._wrangler_ready", lambda: False)
    assert detect_model() == "gemini-decision-1"


def test_detect_model_without_backend(monkeypatch) -> None:
    for key in (
        "TRACE2RULES_DECISION_MODEL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("trace2rules.labels._wrangler_ready", lambda: False)
    assert detect_model() is None
