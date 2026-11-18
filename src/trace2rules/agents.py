from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

VALIDATION_RE = re.compile(
    r"(?i)\b(test|tests|lint|typecheck|validate|validation|pytest|cargo test|go test|"
    r"npm test|vitest|jest|make (?:unit|test)|check)\b"
)
SCRATCH_RE = re.compile(
    r"(?i)\b(reproduce|repro|scratch|debug|verify|validation_test|comprehensive|final)\b"
)
PATH_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+/){1,}[\w.-]+")
COMMAND_RE = re.compile(r"`([^`]+)`")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")


@dataclass(slots=True)
class AgentRule:
    text: str
    category: str
    commands: tuple[str, ...]
    paths: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(slots=True)
class RuleAudit:
    text: str
    category: str
    exercised_sessions: int
    edit_sessions: int
    violation_sessions: int
    enforcement: str
    suggestion: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "category": self.category,
            "exercised_sessions": self.exercised_sessions,
            "edit_sessions": self.edit_sessions,
            "violation_sessions": self.violation_sessions,
            "enforcement": self.enforcement,
            "suggestion": self.suggestion,
        }


def _category(text: str) -> str:
    lower = text.lower()
    if VALIDATION_RE.search(lower):
        return "test_command"
    if SCRATCH_RE.search(lower):
        return "context_hygiene"
    if any(token in lower for token in ("directory", "package", "module", "src/", "crate")):
        return "architecture_map"
    return "convention"


def parse_agents(path: str | Path) -> list[AgentRule]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    rules: list[AgentRule] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```")):
            continue
        if stripped[0] in "-*":
            text = stripped[1:].strip()
        else:
            numbered = re.match(r"^\d+[.)]\s+(.*)", stripped)
            if not numbered:
                continue
            text = numbered.group(1).strip()
        if not text or len(text) < 12:
            continue
        commands = tuple(COMMAND_RE.findall(text))
        paths = tuple(dict.fromkeys(PATH_RE.findall(text)))
        keywords = tuple(
            token
            for token in dict.fromkeys(WORD_RE.findall(text))
            if token.lower() not in {"that", "this", "with", "from", "when", "then", "should", "never"}
        )
        rules.append(AgentRule(text, _category(text), commands, paths, keywords))
    return rules


def _needles(rule: AgentRule) -> tuple[str, ...]:
    values = list(rule.commands) + list(rule.paths)
    for token in rule.keywords:
        if "/" in token or "." in token or token.isupper():
            values.append(token)
    return tuple(dict.fromkeys(value.lower() for value in values if len(value) >= 3))


def _matches(rule: AgentRule, raw_target: str, target: str, object_id: str) -> bool:
    haystack = f"{raw_target}\n{target}\n{object_id}".lower()
    return any(needle in haystack for needle in _needles(rule))


def _enforcement(rule: AgentRule) -> str:
    if rule.category == "test_command":
        return "CI required check / pre-commit hook running the documented command."
    if rule.category == "architecture_map":
        return "Import-boundary lint, CODEOWNERS, or a CI forbidden-path check."
    if rule.category == "context_hygiene":
        return "Pre-commit or CI check rejecting scratch/reproducer files outside /tmp."
    return "No mechanical enforcement; keep as guidance unless a concrete check is added."


def audit_agents(
    con: duckdb.DuckDBPyConnection, repo: str, rules: list[AgentRule]
) -> list[RuleAudit]:
    rows = con.execute(
        """
        SELECT session_id, step_idx, action_type, raw_target, target, object_id, outcome
        FROM tool_events
        WHERE repo = ?
        """,
        [repo],
    ).fetchall()
    sessions: dict[str, list[tuple]] = {}
    for row in rows:
        sessions.setdefault(row[0], []).append(row)

    audits: list[RuleAudit] = []
    for rule in rules:
        exercised = 0
        edited = 0
        violations = 0
        for events in sessions.values():
            if any(
                action == "edit" or _matches(rule, raw, target, object_id)
                for _sid, _step, action, raw, target, object_id, _outcome in events
            ):
                exercised += 1
            edit_steps = [
                step for _sid, step, action, _raw, _target, _object, _outcome in events
                if action == "edit"
            ]
            if not edit_steps:
                continue
            edited += 1
            last_edit = max(edit_steps)
            if rule.category == "test_command":
                followed = any(
                    action == "exec"
                    and outcome == "ok"
                    and step > last_edit
                    and _matches(rule, raw, target, object_id)
                    for _sid, step, action, raw, target, object_id, outcome in events
                )
                if not followed:
                    violations += 1
            elif rule.category == "context_hygiene":
                if any(
                    SCRATCH_RE.search(object_id)
                    and not raw.startswith(("cd /tmp", "cd /tmp/"))
                    for _sid, _step, _action, raw, _target, object_id, _outcome in events
                ):
                    violations += 1
        if not _needles(rule):
            suggestion = "Rule has no command, path, or concrete pattern; add one."
        elif exercised == 0:
            suggestion = "No matching tool calls; the rule may be stale or phrased differently."
        elif violations:
            suggestion = f"Rule was violated in {violations} edited sessions; move it into CI/pre-commit."
        else:
            suggestion = "Rule was exercised without detected violations; keep as guidance."
        audits.append(
            RuleAudit(
                rule.text,
                rule.category,
                exercised,
                edited,
                violations,
                _enforcement(rule),
                suggestion,
            )
        )
    return audits


def audit_payload(con: duckdb.DuckDBPyConnection, repo: str, path: str | Path) -> dict[str, Any]:
    rules = parse_agents(path)
    return {
        "repo": repo,
        "agents_md": str(path),
        "rules": [audit.as_dict() for audit in audit_agents(con, repo, rules)],
    }
