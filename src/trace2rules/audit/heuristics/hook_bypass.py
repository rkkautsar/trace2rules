from __future__ import annotations

import re
from typing import Any

from .common import AuditOutcome


def evaluate(rule: Any, events: list[tuple]) -> AuditOutcome | None:
    commits = [
        raw
        for _sid, _step, action, raw, _target, _object, _outcome in events
        if action == "exec" and re.search(r"\bgit\s+commit\b", raw)
    ]
    if not commits:
        return None
    bypassed = any("--no-verify" in raw for raw in commits)
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=0 if bypassed else 1,
        violations=1 if bypassed else 0,
        examples=commits[:2],
    )
