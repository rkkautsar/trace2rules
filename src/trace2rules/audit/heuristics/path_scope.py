from __future__ import annotations

from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    edit_events: list[tuple[str, str]],
    path_matches: Callable[[str, str], bool],
) -> AuditOutcome | None:
    paths = tuple(rule.paths)
    if not paths:
        return None
    hits = [
        (step, target)
        for step, target in edit_events
        if any(path_matches(path, target) for path in paths)
    ]
    if not hits:
        return None
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=1,
        examples=[target for _step, target in hits[:2]],
    )
