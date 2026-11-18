from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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
ACTIONABLE_SECTION_RE = re.compile(
    r"(?i)(guideline|rule|command|test|lint|type|style|quality|workflow|"
    r"development|contribut|requirement|check|performance|convention)"
)
COMMAND_SECTION_RE = re.compile(
    r"(?i)(command|test|lint|type|quality|check|install|benchmark|setup)"
)
NON_ACTIONABLE_SECTION_RE = re.compile(
    r"(?i)(about|overview|architecture|key concepts|common usage|important files|"
    r"entry points|philosophy)"
)


@dataclass(slots=True)
class AgentRule:
    text: str
    category: str
    commands: tuple[str, ...]
    paths: tuple[str, ...]
    keywords: tuple[str, ...]
    source: str = "AGENTS.md"
    line: int = 0
    section: str = ""


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
    source: str = "AGENTS.md"
    line: int = 0
    section: str = ""
    compliant_sessions: int = 0
    failed_validation_sessions: int = 0
    not_observed_sessions: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "line": self.line,
            "section": self.section,
            "category": self.category,
            "exercised_sessions": self.exercised_sessions,
            "applicable_sessions": self.applicable_sessions,
            "compliant_sessions": self.compliant_sessions,
            "violation_sessions": self.violation_sessions,
            "failed_validation_sessions": self.failed_validation_sessions,
            "not_observed_sessions": self.not_observed_sessions,
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


def _make_rule(
    text: str,
    source: str,
    line: int,
    section: str,
    *,
    commands: tuple[str, ...] | None = None,
    paths: tuple[str, ...] | None = None,
) -> AgentRule:
    commands = commands or tuple(COMMAND_RE.findall(text))
    paths = paths or tuple(dict.fromkeys(PATH_RE.findall(text)))
    keywords = tuple(
        token
        for token in dict.fromkeys(WORD_RE.findall(text))
        if token.lower() not in {"that", "this", "with", "from", "when", "then", "should", "never"}
    )
    return AgentRule(
        text,
        _category(text, paths),
        commands,
        paths,
        keywords,
        source,
        line,
        section,
    )


def parse_agents(path: str | Path) -> list[AgentRule]:
    file_path = Path(path)
    source = file_path.name or "AGENTS.md"
    return parse_agents_text(file_path.read_text(encoding="utf-8"), source)


def parse_agents_text(text: str, source: str = "AGENTS.md") -> list[AgentRule]:
    rules: list[AgentRule] = []
    seen: set[tuple[str, str]] = set()
    section = ""
    in_code = False
    code_lang = ""
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code:
                in_code = False
                code_lang = ""
            else:
                in_code = True
                code_lang = stripped[3:].strip().lower()
            continue
        if in_code:
            if (
                code_lang in {"bash", "sh", "shell", "console", "zsh"}
                and section
                and ACTIONABLE_SECTION_RE.search(section)
                and COMMAND_SECTION_RE.search(section)
                and stripped
                and not stripped.startswith("#")
            ):
                rule = _make_rule(f"Run `{stripped}`", source, line_no, section, commands=(stripped,))
                key = (rule.source, rule.text)
                if key not in seen:
                    seen.add(key)
                    rules.append(rule)
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
            continue
        if not stripped:
            continue
        if section and (
            NON_ACTIONABLE_SECTION_RE.search(section)
            or not ACTIONABLE_SECTION_RE.search(section)
        ):
            continue
        if stripped[0] in "-*":
            text = stripped[1:].strip()
            if text.startswith(("*", "**")):
                continue
        else:
            numbered = re.match(r"^\d+[.)]\s+(.*)", stripped)
            if not numbered:
                continue
            text = numbered.group(1).strip()
        if not text or len(text) < 12:
            continue
        rule = _make_rule(text, source, line_no, section)
        key = (
            (rule.source, "cmd:" + _normalize_command(rule.commands[0]))
            if rule.category == "test_command" and rule.commands
            else (rule.source, rule.text)
        )
        if key in seen:
            continue
        seen.add(key)
        rules.append(rule)
    return rules


def _normalize_command(command: str) -> str:
    command = command.strip()
    command = re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", command)
    command = re.sub(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+",
        "",
        command,
    )
    command = re.sub(r"^(?:timeout\s+\S+\s+)", "", command)
    return command.strip().lower()


def _path_matches(path: str, target: str) -> bool:
    path = path.strip().rstrip("/")
    target = target.strip().rstrip("/")
    if not path or not target:
        return False
    if "<" in path:
        prefix = path.split("<", 1)[0].rstrip("/")
        return target.startswith(prefix + "/") or target == prefix
    return target == path or target.startswith(path + "/") or path in target


def _needles(rule: AgentRule) -> tuple[str, ...]:
    values = [_normalize_command(command) for command in rule.commands]
    values.extend(rule.paths)
    for token in rule.keywords:
        if "/" in token or "." in token or token.isupper():
            values.append(token)
    return tuple(dict.fromkeys(value.lower() for value in values if len(value) >= 3))


def _matches(rule: AgentRule, raw_target: str, target: str, object_id: str) -> bool:
    if rule.commands:
        normalized = _normalize_command(raw_target)
        for command in rule.commands:
            needle = _normalize_command(command)
            if needle and (needle in normalized or needle in (target or "").lower()):
                return True
    if rule.paths:
        return any(_path_matches(path, target or "") for path in rule.paths)
    haystack = f"{raw_target}\n{target}\n{object_id}".lower()
    return any(needle in haystack for needle in _needles(rule))


def _enforcement(rule: AgentRule) -> str:
    if rule.category == "test_command":
        command = rule.commands[0] if rule.commands else "the documented validation command"
        return f"CI required check / pre-commit hook running `{command}`."
    if rule.category == "architecture_map":
        return "Import-boundary lint, CODEOWNERS, or a CI forbidden-path check."
    if rule.category == "context_hygiene":
        return "Pre-commit or CI check rejecting scratch/reproducer files outside /tmp."
    lower = rule.text.lower()
    if re.search(r"(?i)pr title|commit message|conventional commits", lower):
        return "commitlint `commit-msg` hook plus a required PR-title workflow check."
    if re.search(r"(?i)test|coverage", lower):
        return "Diff coverage check (`diff-cover`) or required test job."
    if re.search(r"(?i)docstring|doctest|documentation", lower):
        return "Docstring/doctest lint in CI."
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
        observed = 0
        applicable = 0
        compliant = 0
        failed_validation = 0
        violations = 0
        for events in sessions.values():
            if any(
                _matches(rule, raw, target, object_id)
                for _sid, _step, _action, raw, target, object_id, _outcome in events
            ):
                observed += 1
            edit_steps = [
                step for _sid, step, action, _raw, _target, _object, _outcome in events
                if action == "edit"
            ]
            if rule.category == "test_command":
                if not edit_steps:
                    continue
                applicable += 1
                last_edit = max(edit_steps)
                matching = [
                    (step, outcome, raw, target, object_id)
                    for _sid, step, action, raw, target, object_id, outcome in events
                    if action == "exec"
                    and step > last_edit
                    and (
                        _matches(rule, raw, target, object_id)
                        if rule.commands
                        else VALIDATION_RE.search(raw)
                    )
                ]
                if any(outcome == "ok" for _step, outcome, *_rest in matching):
                    compliant += 1
                else:
                    violations += 1
                    if any(outcome != "ok" for _step, outcome, *_rest in matching):
                        failed_validation += 1
            elif rule.category == "context_hygiene":
                applicable += 1
                if any(
                    action == "edit"
                    and _matches(rule, raw, target, object_id)
                    and not target.startswith(("/tmp/", "tmp/"))
                    for _sid, _step, action, raw, target, object_id, _outcome in events
                ):
                    violations += 1
            else:
                applicable += 1

        enforcement = _enforcement(rule)
        enforceable = not enforcement.startswith("No mechanical enforcement")
        if not _needles(rule):
            suggestion = "Rule has no command, path, or concrete pattern; add one."
        elif violations:
            suggestion = (
                f"{violations} of {applicable} applicable sessions did not satisfy the rule "
                f"(observed {observed}, compliant {compliant}, failed validation {failed_validation}); "
                "move it into CI/pre-commit."
            )
        elif observed == 0 and enforceable:
            suggestion = f"Not observed in sessions; enforce mechanically with: {enforcement}."
        elif observed == 0:
            suggestion = "No matching tool calls; the rule may be stale or phrased differently."
        else:
            suggestion = "Rule was exercised without detected violations; keep as guidance."
        audits.append(
            RuleAudit(
                rule.text,
                rule.category,
                observed,
                applicable,
                violations,
                enforceable,
                enforcement,
                suggestion,
                rule.source,
                rule.line,
                rule.section,
                compliant,
                failed_validation,
                max(applicable - observed, 0),
            )
        )
    return audits


def resolve_agents(path: str | Path | None = None) -> list[Path]:
    if path is not None:
        return [Path(path)]
    candidates = [Path("AGENTS.md"), Path("CLAUDE.md")]
    return [candidate for candidate in candidates if candidate.exists()]


def audit_payload(
    con: duckdb.DuckDBPyConnection,
    repo: str,
    path: str | Path | Sequence[str | Path] | None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    paths = (
        [Path(path)]
        if isinstance(path, (str, Path))
        else [Path(item) for item in path]
        if path is not None
        else resolve_agents()
    )
    rules = [rule for file in paths for rule in parse_agents(file)]
    coverage = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE started_at IS NOT NULL) FROM sessions WHERE repo = ?",
        [repo],
    ).fetchone()
    return {
        "repo": repo,
        "agents_md": [str(file) for file in paths],
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
    return parse_agents_text(content, file_path.name), datetime.fromisoformat(stamp)


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


def audit_prompt(payload: dict[str, Any]) -> str:
    return f"""Repo: {payload.get('repo')}

Existing AGENTS.md audit (JSON):
{json.dumps(payload, indent=2)}

Review these existing rules and return a JSON object with:
- `rules`: only rules that should change, each with:
  - `rule_text`: exact existing rule text
  - `diagnosis`: `unenforced`, `stale`, `ambiguous`, `violated`, or `keep`
  - `evidence`: observed/applicable/compliant/failed/violation counts
  - `enforcement`: concrete CI, pre-commit, lint, or CODEOWNERS check
  - `replacement`: optional stricter wording, or null
- `suggested_new_rules`: rules supported by evidence but missing from AGENTS.md

Do not repeat rules that are already effective. Prefer mechanical enforcement over
soft instructions. If timestamp coverage is zero, state that before/after claims
cannot be verified. Return JSON only.
"""
