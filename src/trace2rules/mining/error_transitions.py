from __future__ import annotations

import re
from collections import defaultdict

import duckdb

from .core import _representative_window
from .heuristics.command_errors import COMMAND_ERROR_RE
from .heuristics.permission_denials import PERMISSION_RE
from .models import Finding

SELECTOR_RE = re.compile(r"(?i)deselected|no tests ran|collected 0 items")


def _classes(action: str, signature: str, message: str, cause: str) -> list[str]:
    text = f"{message or ''} {cause or ''}"
    classes: list[str] = []
    if action in {"exec", "read"} and re.search(COMMAND_ERROR_RE, text):
        classes.append("command")
    if re.search(PERMISSION_RE, text):
        classes.append("permission")
    if action == "exec" and SELECTOR_RE.search(text):
        classes.append("selector")
    if "AssertionError" in (signature or ""):
        classes.append("assertion")
    if "ModuleNotFoundError" in (signature or "") or "ImportError" in (signature or ""):
        classes.append("import")
    if "timeout" in (signature or "").lower():
        classes.append("timeout")
    return classes


def error_transitions(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    """Find ordered transitions between distinct error classes in one session."""
    rows = con.execute(
        """
        SELECT e.session_id, s.repo, s.instance_id, e.step_idx, e.action_type,
               e.error_signature, e.error_message, e.obs_cause,
               e.approx_out_tokens
        FROM tool_events e JOIN sessions s USING (session_id)
        WHERE e.is_error
        ORDER BY e.session_id, e.step_idx
        """
    ).fetchall()

    sequences: dict[tuple[str, str], list[str]] = defaultdict(list)
    class_steps: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    session_cost: dict[tuple[str, str], int] = defaultdict(int)
    session_instance: dict[tuple[str, str], str] = {}
    for session_id, repo, instance_id, step, action, signature, message, cause, cost in rows:
        session_key = (session_id, repo)
        session_cost[session_key] += int(cost or 0)
        session_instance.setdefault(session_key, instance_id or session_id)
        for cls in _classes(action, signature, message, cause):
            sequence = sequences[session_key]
            if not sequence or sequence[-1] != cls:
                sequence.append(cls)
            class_steps[session_key].setdefault(cls, int(step))

    sessions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    instances: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    occurrences: dict[tuple[str, str, str], int] = defaultdict(int)
    costs: dict[tuple[str, str, str], int] = defaultdict(int)
    examples: dict[tuple[str, str, str], tuple[str, int]] = {}
    for session_key, sequence in sequences.items():
        session_id, repo = session_key
        for first, second in zip(sequence, sequence[1:]):
            key = (repo, first, second)
            sessions[key].add(session_id)
            instances[key].add(session_instance[session_key])
            occurrences[key] += 1
            costs[key] += session_cost[session_key]
            examples.setdefault(key, (session_id, class_steps[session_key][first]))

    findings = [
        Finding(
            kind="error_transition",
            repo=repo,
            title=f"{first} -> {second}",
            sessions=len(sessions[key]),
            tokens=int(costs[key]),
            detail=(
                f"{occurrences[key]} ordered transitions across "
                f"{len(instances[key])} instances"
            ),
            failure_family=f"{first}->{second}",
            side="agent",
            recommendation=(
                "Treat these as one failure chain: resolve the earlier error "
                "class before changing the next command or test selector."
            ),
            window=_representative_window(con, *examples[key]),
        )
        for key in sessions
        for repo, first, second in [key]
        if len(sessions[key]) >= 5 and len(instances[key]) >= 2
    ]
    findings.sort(key=lambda finding: (finding.sessions, finding.tokens), reverse=True)
    return findings[:limit] if limit is not None else findings
