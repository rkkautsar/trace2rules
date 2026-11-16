from __future__ import annotations

import re

_PATTERNS = (
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|"
            r"password|passwd|pwd)\s*[:=]\s*)"
            r"([^\s\"'`]+)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])("
            r"sk-[A-Za-z0-9_-]{16,}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|"
            r"AKIA[0-9A-Z]{16}|"
            r"AIza[0-9A-Za-z_-]{30,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}"
            r")"
        ),
        "<redacted>",
    ),
    (
        re.compile(
            r"(?i)(https?://[^/\s:@]+):[^@\s]+@"
        ),
        r"\1:<redacted>@",
    ),
    (
        re.compile(r"(?i)([?&](?:token|key|secret|password|signature)=)[^&\s]+"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted-private-key>",
    ),
)


def redact_text(value: str) -> str:
    text = value
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
