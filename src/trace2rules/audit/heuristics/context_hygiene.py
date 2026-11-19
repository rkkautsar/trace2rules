from __future__ import annotations

import re
from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    edit_events: list[tuple[str, str]],
    matched: bool,
    *,
    matches: Callable[[Any, str, str, str], bool],
    needles: Callable[[Any], tuple[str, ...]],
    scratch_re: re.Pattern[str],
) -> AuditOutcome | None:
    if not edit_events and not matched:
        return None
    dirty = any(
        action == "edit"
        and (
            matches(rule, raw, target, object_id)
            or (not needles(rule) and scratch_re.search(object_id or target))
        )
        and not target.startswith(("/tmp/", "tmp/"))
        for _sid, _step, action, raw, target, object_id, _outcome in events
    )
    return AuditOutcome(
        applicable=1,
        not_observed=0 if matched else 1,
        violations=1 if dirty else 0,
    )
