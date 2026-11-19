from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .common import AuditOutcome


def evaluate(
    rule: Any,
    events: list[tuple],
    root: Path | None,
    read_hygiene: Callable[[list[tuple], Path | None, Any], tuple[list[str], bool]],
) -> AuditOutcome | None:
    reads, whole = read_hygiene(events, root, rule)
    if not reads:
        return None
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=0 if whole else 1,
        violations=1 if whole else 0,
        examples=reads[:2],
    )
