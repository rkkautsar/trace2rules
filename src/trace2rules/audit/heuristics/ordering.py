from __future__ import annotations

import re
from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    normalize: Callable[[str], str],
    argv_match: Callable[[str, str], bool],
    test_run_re: re.Pattern[str],
) -> AuditOutcome | None:
    if not rule.commands:
        return None
    before_tests = bool(
        re.search(r"(?i)\bbefore (?:running |running the )?tests?\b", rule.text)
    )
    if before_tests:
        tests = [
            (step, raw)
            for _sid, step, action, raw, _target, _object, _outcome in events
            if action == "exec" and test_run_re.search(raw)
        ]
        if not tests:
            return None
        first_test, first_test_raw = tests[0]
        prerequisite = rule.commands[0]
        prerequisite_events = [
            step
            for _sid, step, action, raw, _target, _object, _outcome in events
            if action == "exec"
            and (
                step < first_test
                or (step == first_test and raw.find(prerequisite) < raw.find(first_test_raw))
            )
            and argv_match(normalize(prerequisite), normalize(raw))
        ]
        return AuditOutcome(
            applicable=1,
            observed=1,
            compliant=1 if prerequisite_events else 0,
            violations=0 if prerequisite_events else 1,
            examples=[raw[:240] for _step, raw in tests[:2]],
        )
    if len(rule.commands) < 2:
        return None
    action_command, prerequisite = rule.commands[0], rule.commands[1]
    commits = [
        (step, raw)
        for _sid, step, action, raw, _target, _object, _outcome in events
        if action == "exec" and argv_match(normalize(action_command), normalize(raw))
    ]
    if not commits:
        return None
    first_commit, _first_raw = commits[0]
    prerequisite_events = [
        step
        for _sid, step, action, raw, _target, _object, _outcome in events
        if action == "exec"
        and (
            step < first_commit
            or (step == first_commit and raw.find(prerequisite) < raw.find(action_command))
        )
        and argv_match(normalize(prerequisite), normalize(raw))
    ]
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=1 if prerequisite_events else 0,
        violations=0 if prerequisite_events else 1,
        examples=[raw[:240] for _step, raw in commits[:2]],
    )
