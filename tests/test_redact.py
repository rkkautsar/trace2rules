from __future__ import annotations

from trace2rules.redact import redact_text, redact_value


def test_redacts_env_style_secrets() -> None:
    text = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    assert "sk-proj" not in redact_text(text)
    assert "<redacted>" in redact_text(text)


def test_redacts_bearer_and_url_credentials() -> None:
    assert "abc123" not in redact_text("Authorization: Bearer abc123")
    assert "secret" not in redact_text("git clone https://user:secret@example.com/repo")
    assert "token=secret" not in redact_text("https://example.com/?token=secret&x=1")


def test_redacts_private_keys_and_nested_values() -> None:
    key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\nabc123\n"
        + "-----END "
        + "PRIVATE KEY-----"
    )
    assert redact_text(key) == "<redacted-private-key>"
    value = redact_value({"command": "PASSWORD=hunter2", "items": ["sk-abcdefghijklmnop"]})
    assert "hunter2" not in str(value)
    assert "sk-abcdefghijklmnop" not in str(value)


def test_redacts_quoted_values_and_dsn_passwords() -> None:
    text = 'password: "hunter2" client_secret=shh postgres://user:s3cr3t@db/internal'
    redacted = redact_text(text)
    assert "hunter2" not in redacted
    assert "shh" not in redacted
    assert "s3cr3t" not in redacted


def test_audit_prompt_redacts_evidence() -> None:
    from trace2rules.audit.engine import audit_prompt

    prompt = audit_prompt({"repo": "acme/repo", "examples": ["PASSWORD=hunter2"]})
    assert "hunter2" not in prompt
