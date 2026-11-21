from __future__ import annotations

import re
from typing import Any, Callable

from .common import AuditOutcome
from ..parsing import COMMAND_RE, NEGATIVE_RULE_RE


def _forbidden(rule: Any) -> tuple[str, ...]:
    clauses = re.split(r"[.;]\s+", rule.text)
    selected: list[str] = []
    for clause in clauses:
        if NEGATIVE_RULE_RE.search(clause):
            selected.extend(COMMAND_RE.findall(clause))
    return tuple(dict.fromkeys(selected or rule.commands))


def evaluate(
    rule: Any,
    events: list[tuple],
    normalize: Callable[[str], str],
    argv_match: Callable[[str, str], bool],
) -> AuditOutcome | None:
    forbidden = _forbidden(rule)
    if not forbidden:
        return None
    hits = [
        raw
        for _sid, _step, action, raw, _target, _object, _outcome in events
        if action == "exec"
        and any(argv_match(normalize(command), normalize(raw)) for command in forbidden)
    ]
    if not hits:
        return None
    return AuditOutcome(applicable=1, observed=1, compliant=0, violations=1, examples=hits[:2])
