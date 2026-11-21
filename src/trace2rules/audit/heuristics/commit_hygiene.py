from __future__ import annotations

import re
from typing import Any

from .common import AuditOutcome

_COMMIT_RE = re.compile(r"(?i)(^|[;&|]\s*)git\s+commit\b")


def evaluate(rule: Any, events: list[tuple]) -> AuditOutcome | None:
    edits = [step for _sid, step, action, _raw, _target, _object, _outcome in events if action == "edit"]
    if not edits:
        return None
    commits = [
        step
        for _sid, step, action, raw, _target, _object, outcome in events
        if action == "exec" and outcome == "ok" and _COMMIT_RE.search(raw)
    ]
    last_edit = max(edits)
    last_commit = max(commits) if commits else None
    violations = (
        (len(edits) >= 8 and not commits)
        or (len(edits) >= 12 and len(commits) == 1)
        or (last_commit is not None and last_edit > last_commit and len(edits) >= 4)
    )
    reason = (
        "no commit after edits"
        if len(edits) >= 8 and not commits
        else "one large commit"
        if len(edits) >= 12 and len(commits) == 1
        else "edits after final commit"
        if last_commit is not None and last_edit > last_commit and len(edits) >= 4
        else ""
    )
    return AuditOutcome(
        applicable=1,
        observed=1,
        compliant=0 if violations else 1,
        violations=1 if violations else 0,
        examples=[reason] if reason else [],
    )
