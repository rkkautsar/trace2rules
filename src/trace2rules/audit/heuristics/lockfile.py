from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import AuditOutcome


def _matches(target: str, path: str) -> bool:
    target = target.strip().rstrip("/")
    path = path.strip().rstrip("/")
    return target == path or target.endswith("/" + path) or Path(target).name == Path(path).name


def evaluate(rule: Any, edit_events: list[tuple[str, str]]) -> AuditOutcome | None:
    paths = tuple(path for path in rule.paths if Path(path).name.endswith((".lock", "-lock.json", "lock.yaml")) or Path(path).name in {"yarn.lock"})
    if not paths:
        return None
    hits = [target for _step, target in edit_events if any(_matches(target, path) for path in paths)]
    if not hits:
        return None
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=0,
        violations=1,
        examples=hits[:2],
    )
