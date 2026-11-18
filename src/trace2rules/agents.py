from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

VALIDATION_RE = re.compile(
    r"(?i)\b(test|tests|lint|typecheck|validate|validation|pytest|cargo test|go test|"
    r"npm test|vitest|jest|make (?:unit|test)|check)\b"
)
SCRATCH_RE = re.compile(
    r"(?i)\b(?:reproduce|repro|scratch|debug|verify|validation_test|comprehensive|final)[\w.-]*\b"
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
    applicable_sessions: int
    violation_sessions: int
    enforceable: bool
    enforcement: str
    suggestion: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "category": self.category,
            "exercised_sessions": self.exercised_sessions,
            "applicable_sessions": self.applicable_sessions,
            "violation_sessions": self.violation_sessions,
            "enforceable": self.enforceable,
            "enforcement": self.enforcement,
            "suggestion": self.suggestion,
        }


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _category(text: str, paths: tuple[str, ...]) -> str:
    lower = text.lower()
    if SCRATCH_RE.search(lower) and any(
        token in lower for token in ("do not", "don't", "never", "keep", "outside")
    ):
        return "context_hygiene"
    if VALIDATION_RE.search(lower) and re.search(
        r"(?i)\b(run|execute|invoke|use|validate|check|test)\b", lower
    ) and not lower.startswith(("do not add test", "don't add test", "never add test")):
        return "test_command"
    if paths or any(
        token in lower
        for token in ("directory", "package", "module", "src/", "crate", "dialect", "parser")
    ):
        return "architecture_map"
    return "convention"


def parse_agents(path: str | Path) -> list[AgentRule]:
    return parse_agents_text(Path(path).read_text(encoding="utf-8"))


def parse_agents_text(text: str) -> list[AgentRule]:
    lines = text.splitlines()
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
        rules.append(AgentRule(text, _category(text, paths), commands, paths, keywords))
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
    con: duckdb.DuckDBPyConnection,
    repo: str,
    rules: list[AgentRule],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[RuleAudit]:
    filters = ["e.repo = ?"]
    params: list[Any] = [repo]
    since = _utc_naive(since)
    until = _utc_naive(until)
    if since is not None:
        filters.append("s.started_at >= ?")
        params.append(since)
    if until is not None:
        filters.append("s.started_at < ?")
        params.append(until)
    rows = con.execute(
        f"""
        SELECT e.session_id, e.step_idx, e.action_type, e.raw_target,
               e.target, e.object_id, e.outcome
        FROM tool_events e JOIN sessions s USING (session_id)
        WHERE {' AND '.join(filters)}
        """,
        params,
    ).fetchall()
    sessions: dict[str, list[tuple]] = {}
    for row in rows:
        sessions.setdefault(row[0], []).append(row)

    audits: list[RuleAudit] = []
    for rule in rules:
        exercised = 0
        applicable = 0
        violations = 0
        for events in sessions.values():
            if any(
                _matches(rule, raw, target, object_id)
                for _sid, _step, _action, raw, target, object_id, _outcome in events
            ):
                exercised += 1
            edit_steps = [
                step for _sid, step, action, _raw, _target, _object, _outcome in events
                if action == "edit"
            ]
            if rule.category == "test_command":
                applicable += 1 if edit_steps else 0
            else:
                applicable += 1
            if not edit_steps:
                continue
            last_edit = max(edit_steps)
            if rule.category == "test_command":
                followed = any(
                    action == "exec"
                    and outcome == "ok"
                    and step > last_edit
                    and (
                        _matches(rule, raw, target, object_id)
                        or VALIDATION_RE.search(raw)
                    )
                    for _sid, step, action, raw, target, object_id, outcome in events
                )
                if not followed:
                    violations += 1
            elif rule.category == "context_hygiene":
                if any(
                    action == "edit"
                    and (
                        _matches(rule, _raw, target, object_id)
                        or not _needles(rule) and SCRATCH_RE.search(object_id or target)
                    )
                    and not target.startswith(("/tmp/", "tmp/"))
                    for _sid, _step, action, _raw, target, object_id, _outcome in events
                ):
                    violations += 1
        if not _needles(rule):
            suggestion = "Rule has no command, path, or concrete pattern; add one."
        elif violations:
            suggestion = (
                f"Rule was violated in {violations} of {applicable} applicable sessions; "
                "move it into CI/pre-commit."
            )
        elif exercised == 0:
            suggestion = "No matching tool calls; the rule may be stale or phrased differently."
        else:
            suggestion = "Rule was exercised without detected violations; keep as guidance."
        enforceable = rule.category in {"test_command", "architecture_map", "context_hygiene"}
        audits.append(
            RuleAudit(
                rule.text,
                rule.category,
                exercised,
                applicable,
                violations,
                enforceable,
                _enforcement(rule),
                suggestion,
            )
        )
    return audits


def audit_payload(
    con: duckdb.DuckDBPyConnection,
    repo: str,
    path: str | Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    rules = parse_agents(path)
    coverage = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE started_at IS NOT NULL) FROM sessions WHERE repo = ?",
        [repo],
    ).fetchone()
    return {
        "repo": repo,
        "agents_md": str(path),
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "sessions_with_timestamps": coverage[1],
        "sessions": coverage[0],
        "rules": [
            audit.as_dict()
            for audit in audit_agents(con, repo, rules, since=since, until=until)
        ],
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def latest_agents_commit(path: str | Path) -> tuple[str, datetime]:
    file_path = Path(path).resolve()
    root = Path(_git(file_path.parent, "rev-parse", "--show-toplevel"))
    relative = file_path.relative_to(root).as_posix()
    output = _git(root, "log", "-1", "--format=%H%x00%cI", "--", relative)
    if not output:
        raise ValueError(f"{relative} has no commit history")
    sha, stamp = output.split("\x00", 1)
    return sha, datetime.fromisoformat(stamp)


def agents_at_commit(path: str | Path, rev: str) -> tuple[list[AgentRule], datetime]:
    file_path = Path(path).resolve()
    root = Path(_git(file_path.parent, "rev-parse", "--show-toplevel"))
    relative = file_path.relative_to(root).as_posix()
    content = _git(root, "show", f"{rev}:{relative}")
    stamp = _git(root, "show", "-s", "--format=%cI", rev)
    return parse_agents_text(content), datetime.fromisoformat(stamp)


def audit_commit(
    con: duckdb.DuckDBPyConnection, repo: str, path: str | Path, rev: str
) -> dict[str, Any]:
    file_path = Path(path).resolve()
    root = Path(_git(file_path.parent, "rev-parse", "--show-toplevel"))
    relative = file_path.relative_to(root).as_posix()
    try:
        parent = _git(root, "rev-parse", f"{rev}^")
    except subprocess.CalledProcessError:
        parent = ""
    if parent:
        try:
            before_rules, _ = agents_at_commit(path, parent)
        except subprocess.CalledProcessError:
            before_rules = []
    else:
        before_rules = []
    after_rules, committed_at = agents_at_commit(path, rev)
    before = audit_agents(con, repo, before_rules, until=committed_at)
    after = audit_agents(con, repo, after_rules, since=committed_at)
    before_text = {rule.text for rule in before_rules}
    after_text = {rule.text for rule in after_rules}
    coverage = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE started_at IS NOT NULL) FROM sessions WHERE repo = ?",
        [repo],
    ).fetchone()
    return {
        "repo": repo,
        "commit": rev,
        "parent": parent,
        "committed_at": committed_at.isoformat(),
        "agents_md": str(file_path),
        "sessions_with_timestamps": coverage[1],
        "sessions": coverage[0],
        "added": sorted(after_text - before_text),
        "removed": sorted(before_text - after_text),
        "before": [audit.as_dict() for audit in before],
        "after": [audit.as_dict() for audit in after],
    }
