from __future__ import annotations

from trace2rules.synthesize import _client


def test_custom_llm_key_uses_explicit_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("TRACE2RULES_LLM_API_KEY", "test-key")
    monkeypatch.setenv("TRACE2RULES_BASE_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("TRACE2RULES_MODEL", "test-model")
    client, model = _client()
    assert client is not None
    assert model == "test-model"
