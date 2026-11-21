from __future__ import annotations

import re
from typing import Any

from .common import AuditOutcome

_FORBIDDEN = (
    (r"\bgit\s+push\b.*(?:--force|--force-with-lease)|\bforce[- ]push\b", "force push"),
    (r"\bgit\s+reset\b.*--hard", "git reset --hard"),
    (r"\bgit\s+clean\b", "git clean"),
    (r"\b(?:npm|cargo|pdm|poetry)\s+publish\b|\btwine\s+upload\b|\bpublish\s+to\b", "package publish"),
    (r"\bgh\s+release\b", "release command"),
)


def evaluate(rule: Any, events: list[tuple]) -> AuditOutcome | None:
    text = rule.text
    lower = text.lower()
    patterns = [
        pattern
        for pattern, label in _FORBIDDEN
        if label in lower
        or re.search(pattern, text, re.I)
        or (label == "package publish" and "publish" in lower)
        or (label == "release command" and "release" in lower)
    ]
    if not patterns:
        return None
    hits = [
        raw
        for _sid, _step, action, raw, _target, _object, _outcome in events
        if action == "exec" and any(re.search(pattern, raw, re.I) for pattern in patterns)
    ]
    if not hits:
        return None
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=0,
        violations=1,
        examples=hits[:2],
    )
