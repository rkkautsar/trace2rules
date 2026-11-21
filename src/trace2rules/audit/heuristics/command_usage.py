from __future__ import annotations

from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    matches: Callable[[Any, str, str, str], bool],
) -> AuditOutcome | None:
    hits = [
        event
        for event in events
        if event[2] == "exec" and matches(rule, event[3], event[4], event[5])
    ]
    if not hits:
        return None
    ok = any(event[6] == "ok" for event in hits)
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=1 if ok else 0,
        violations=0 if ok else 1,
        examples=[event[3][:240] for event in hits[:2]],
        test_outcomes={"command_observed": 1},
    )
