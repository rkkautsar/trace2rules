from __future__ import annotations

from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    edit_events: list[tuple[str, str]],
    is_source_read: Callable[[str, str, str, str], bool],
) -> AuditOutcome | None:
    if not edit_events:
        return None
    first_edit, first_target = min(edit_events, key=lambda item: item[0])
    reads = [
        raw
        for _sid, step, action, raw, target, _object, _outcome in events
        if step < first_edit and is_source_read(action, raw, target, first_target)
    ]
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=1 if reads else 0,
        violations=0 if reads else 1,
        examples=reads[:2],
    )
