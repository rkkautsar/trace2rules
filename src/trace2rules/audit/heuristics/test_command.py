from __future__ import annotations

import re
from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    edit_events: list[tuple[str, str]],
    scope: tuple[str, ...],
    *,
    doc_target_re: re.Pattern[str],
    scoped_test_edits: Callable[[Any, list[tuple[str, str]]], list[tuple[str, str]]],
    matches: Callable[[Any, str, str, str], bool],
    normalize: Callable[[str], str],
    argv_match: Callable[[str, str], bool],
    validation_re: re.Pattern[str],
) -> AuditOutcome | None:
    edit_events = [
        (step, target)
        for step, target in edit_events
        if scope or not doc_target_re.search(target or "")
    ]
    edit_events = scoped_test_edits(rule, edit_events)
    if not edit_events:
        return None
    last_edit = max(step for step, _target in edit_events)

    def exact(event: tuple) -> bool:
        _sid, _step, action, raw, target, object_id, _outcome = event
        return action == "exec" and (
            matches(rule, raw, target, object_id)
            if rule.commands
            else bool(validation_re.search(raw))
        )

    def family(event: tuple) -> bool:
        if event[2] != "exec":
            return False
        raw = normalize(event[3])
        if not rule.commands:
            return bool(validation_re.search(raw))
        for command in rule.commands:
            tokens = normalize(command).split()
            if not tokens:
                continue
            prefix = tokens[:2] if tokens[0] in {"cargo", "git", "uv", "npm", "pnpm"} else tokens[:1]
            if argv_match(" ".join(prefix), raw):
                return True
        return False

    post_exact = [event for event in events if event[1] > last_edit and exact(event)]
    post_any = [event for event in events if event[1] > last_edit and family(event)]
    pre_any = [
        event for event in events
        if event[1] <= last_edit and (exact(event) or family(event))
    ]
    matched = any(exact(event) for event in events)
    outcome = AuditOutcome(
        applicable=1,
        observed=1 if matched else 0,
        not_observed=0 if matched else 1,
        examples=[event[3][:240] for event in (post_exact or post_any or pre_any)[:2]],
    )
    if post_exact:
        if any(event[6] == "ok" for event in post_exact):
            outcome.compliant = 1
            outcome.test_outcomes["exact_form"] = 1
        else:
            outcome.violations = 1
            outcome.failed_validation = 1
            outcome.test_outcomes["failed_test"] = 1
    elif post_any:
        outcome.undecided = 1
        outcome.test_outcomes["alternate_form"] = 1
    elif pre_any:
        outcome.violations = 1
        outcome.test_outcomes["before_final_edit"] = 1
    else:
        outcome.violations = 1
        outcome.test_outcomes["no_test"] = 1
    return outcome
