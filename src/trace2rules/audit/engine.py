from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .heuristics.context_hygiene import evaluate as evaluate_context_hygiene
from .heuristics.generic import evaluate as evaluate_generic
from .heuristics.hook_bypass import evaluate as evaluate_hook_bypass
from .heuristics.ordering import evaluate as evaluate_ordering
from .heuristics.read_before_edit import evaluate as evaluate_read_before_edit
from .heuristics.read_hygiene import evaluate as evaluate_read_hygiene
from .heuristics.test_command import evaluate as evaluate_test_command

from .enforcement import (
    discover_enforcement,
    enforcement_for as _enforcement,
)
from .reads import (
    _is_source_read,
    _read_hygiene,
    _scoped_test_edits,
    _stale_paths,
)
from .scope import _infer_scope
from .parsing import (
    DOC_TARGET_RE,
    SCRATCH_RE,
    TEST_RUN_RE,
    VALIDATION_RE,
    AgentRule,
    _normalize_command,
    parse_agents,
    parse_agents_text,
)


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
    scope: str = "repository"
    existing_enforcement: str = ""
    enforcement_status: str = "unknown"
    required: bool | None = None
    required_reason: str = "not checked"
    detector: str = ""
    measurable: bool = False
    coverage: str = "unmeasured"
    coverage_reason: str = ""
    opportunities: int = 0
    followed: int = 0
    undecided: int = 0
    rate: float | None = None
    confidence: str = "none"
    wording: str = "direct"
    stale_paths: tuple[str, ...] = ()
    test_outcomes: dict[str, int] = field(default_factory=dict)
    examples: tuple[str, ...] = ()

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
            "scope": self.scope,
            "existing_enforcement": self.existing_enforcement,
            "enforcement_status": self.enforcement_status,
            "required": self.required,
            "required_reason": self.required_reason,
            "detector": self.detector,
            "measurable": self.measurable,
            "coverage": self.coverage,
            "coverage_reason": self.coverage_reason,
            "opportunities": self.opportunities,
            "followed": self.followed,
            "undecided": self.undecided,
            "rate": self.rate,
            "confidence": self.confidence,
            "wording": self.wording,
            "stale_paths": list(self.stale_paths),
            "test_outcomes": dict(self.test_outcomes),
            "examples": list(self.examples),
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


def _argv_match(required: str, actual: str) -> bool:
    required_tokens = required.split()
    actual_tokens = actual.split()
    if not required_tokens:
        return False

    def match(required_index: int, actual_index: int) -> bool:
        if required_index == len(required_tokens):
            return True
        if actual_index == len(actual_tokens):
            return False
        token = required_tokens[required_index]
        if token.startswith("<") and token.endswith(">"):
            return match(required_index + 1, actual_index + 1)
        if token != actual_tokens[actual_index]:
            return False
        return match(required_index + 1, actual_index + 1)

    return any(match(0, start) for start in range(len(actual_tokens)))


def _matches(rule: AgentRule, raw_target: str, target: str, object_id: str) -> bool:
    if rule.commands:
        normalized = _normalize_command(raw_target)
        for command in rule.commands:
            needle = _normalize_command(command)
            if not needle:
                continue
            if _argv_match(needle, normalized):
                return True
    if rule.paths:
        return any(_path_matches(path, target or "") for path in rule.paths)
    haystack = f"{raw_target}\n{target}\n{object_id}".lower()
    return any(needle in haystack for needle in _needles(rule))


def audit_agents(
    con: duckdb.DuckDBPyConnection,
    repo: str,
    rules: list[AgentRule],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    root: Path | None = None,
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
        not_observed = 0
        undecided_count = 0
        test_outcomes: dict[str, int] = {}
        examples: list[str] = []
        scope = _infer_scope(rule, root)
        for events in sessions.values():
            edit_events = [
                (step, target)
                for _sid, step, action, _raw, target, _object, outcome in events
                if action == "edit" and outcome not in ("err", "timeout")
            ]
            if scope:
                edit_events = [
                    (step, target)
                    for step, target in edit_events
                    if any(_path_matches(path, target) for path in scope)
                ]
            matched = any(
                _matches(rule, raw, target, object_id)
                for _sid, _step, action, raw, target, object_id, _outcome in events
                if action == "exec"
            )
            if matched and rule.category != "test_command":
                observed += 1
            if rule.category == "test_command":
                outcome = evaluate_test_command(
                    rule,
                    events,
                    edit_events,
                    scope,
                    doc_target_re=DOC_TARGET_RE,
                    scoped_test_edits=_scoped_test_edits,
                    matches=_matches,
                    normalize=_normalize_command,
                    argv_match=_argv_match,
                    validation_re=VALIDATION_RE,
                )
                if outcome is None:
                    continue
                applicable += outcome.applicable
                observed += outcome.observed
                not_observed += outcome.not_observed
                compliant += outcome.compliant
                violations += outcome.violations
                failed_validation += outcome.failed_validation
                undecided_count += outcome.undecided
                for key, count in outcome.test_outcomes.items():
                    test_outcomes[key] = test_outcomes.get(key, 0) + count
                examples.extend(outcome.examples)
            elif rule.category == "ordering":
                outcome = evaluate_ordering(
                    rule, events, _normalize_command, _argv_match, TEST_RUN_RE
                )
                if outcome is None:
                    continue
                applicable += outcome.applicable
                observed += outcome.observed
                compliant += outcome.compliant
                violations += outcome.violations
                examples.extend(outcome.examples)
            elif rule.category in {"read_before_edit", "read_hygiene"}:
                if rule.category == "read_before_edit":
                    outcome = evaluate_read_before_edit(
                        rule, events, edit_events, _is_source_read
                    )
                else:
                    outcome = evaluate_read_hygiene(
                        rule, events, root, _read_hygiene
                    )
                if outcome is None:
                    continue
                applicable += outcome.applicable
                observed += outcome.observed
                compliant += outcome.compliant
                violations += outcome.violations
                examples.extend(outcome.examples)
            elif rule.category == "hook_bypass":
                outcome = evaluate_hook_bypass(rule, events)
                if outcome is None:
                    continue
                applicable += outcome.applicable
                observed += outcome.observed
                compliant += outcome.compliant
                violations += outcome.violations
                examples.extend(outcome.examples)
            elif rule.category == "context_hygiene":
                outcome = evaluate_context_hygiene(
                    rule,
                    events,
                    edit_events,
                    matched,
                    matches=_matches,
                    needles=_needles,
                    scratch_re=SCRATCH_RE,
                )
                if outcome is None:
                    continue
                applicable += outcome.applicable
                not_observed += outcome.not_observed
                violations += outcome.violations
            else:
                outcome = evaluate_generic(matched)
                applicable += outcome.applicable
                not_observed += outcome.not_observed

        wording = "conditional" if re.search(r"(?i)\b(?:if|when|unless)\b", rule.text) else "direct"
        stale_paths = _stale_paths(rule, root)
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
            suggestion = f"Not observed in sessions; enforce mechanically with: {enforcement}"
        elif observed == 0:
            suggestion = "No matching tool calls; the rule may be stale or phrased differently."
        else:
            suggestion = "Rule was exercised without detected violations; keep as guidance."
        detector = {
            "test_command": "test-after-change",
            "context_hygiene": "scratch-file-hygiene",
            "architecture_map": "path-ownership",
        }.get(rule.category, "generic")
        measurable = rule.category in {
            "test_command", "hook_bypass", "read_hygiene", "read_before_edit", "ordering", "context_hygiene",
        }
        opportunities = applicable if measurable else 0
        undecided = 0
        if rule.category == "test_command":
            followed = compliant
            undecided = undecided_count
            violations = max(applicable - followed - undecided, 0)
        elif rule.category in {"hook_bypass", "read_hygiene", "read_before_edit", "ordering"}:
            followed = compliant
            undecided = max(applicable - followed - violations, 0)
        elif rule.category == "context_hygiene":
            followed = max(applicable - violations, 0)
        else:
            followed = 0
        if rule.category == "hook_bypass":
            coverage = "partial"
            coverage_reason = "observable pre-commit bypass; other clauses are not scored"
        elif measurable:
            coverage = "measured"
            coverage_reason = "observable command or path evidence"
        elif rule.commands or rule.paths:
            coverage = "unmeasured"
            coverage_reason = "no compliance detector for this rule category"
        elif re.search(
            r"(?i)\b(secrets?|credentials?|approval|attestation|reviewer)\b",
            rule.text,
        ):
            coverage = "dark"
            coverage_reason = "policy is not observable from tool transcripts"
        else:
            coverage = "unmeasured"
            coverage_reason = "no observable command or path in the rule"
        measurable_count = followed + violations
        rate = (
            round(followed / measurable_count, 3)
            if measurable_count >= 20
            else None
        )
        confidence = (
            "none" if not measurable or opportunities == 0
            else "low" if opportunities < 20
            else "medium" if opportunities < 50
            else "high"
        )
        harness_only = {"read_before_edit", "read_hygiene", "ordering"}
        if rule.category in harness_only:
            status, existing = "n/a", ""
            if violations:
                suggestion = (
                    f"{violations} of {applicable} applicable sessions did not satisfy the rule; "
                    "keep it as AGENTS.md guidance"
                    + (
                        ", or fold the prerequisite into one test/Makefile command."
                        if rule.category == "ordering" else "."
                    )
                )
            elif observed == 0:
                suggestion = "Guidance-only rule; no matching tool calls were observed."
        else:
            status, existing = discover_enforcement(root, rule)
        if status == "configured":
            suggestion = (
                f"Enforcement is already configured at {existing}; verify it runs as a required check."
            )
        elif status == "available":
            suggestion = (
                f"{available_text(existing)} exists but is not wired into CI/pre-commit; "
                "make it a required check."
            )
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
                not_observed,
                ", ".join(scope) if scope else "repository",
                existing,
                status,
            )
        )
        audit = audits[-1]
        audit.detector = detector
        audit.measurable = measurable
        audit.coverage = coverage
        audit.coverage_reason = coverage_reason
        audit.opportunities = opportunities
        audit.followed = followed
        audit.undecided = undecided
        audit.rate = rate
        audit.confidence = confidence
        audit.wording = wording
        audit.stale_paths = stale_paths
        audit.test_outcomes = test_outcomes
        audit.examples = tuple(dict.fromkeys(examples))[:3]
        if rule.category in {"read_before_edit", "read_hygiene", "ordering"}:
            audit.required = False
            audit.required_reason = "guidance only; not enforceable by repository checks"
        elif status == "configured":
            audit.required = None
            audit.required_reason = "CI presence only; required checks need branch protection or rulesets"
        elif status == "missing":
            audit.required = False
            audit.required_reason = "no enforcement found"
        else:
            audit.required = None
            audit.required_reason = "not enough repository evidence"
    return audits


def _positive_commands(text: str) -> set[str]:
    return {
        _normalize_command(command)
        for command in re.findall(r"(?i)\b(?:use|run|prefer)\s+`([^`]+)`", text)
    }


def _negative_commands(text: str) -> set[str]:
    return {
        _normalize_command(command)
        for command in re.findall(
            r"(?i)\b(?:never|do not|don't|avoid)\s+(?:use\s+)?`([^`]+)`",
            text,
        )
    }


def _rule_conflicts(rules: list[AgentRule]) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    for index, rule in enumerate(rules):
        positive = _positive_commands(rule.text)
        negative = _negative_commands(rule.text)
        if positive:
            for other in rules[index + 1:]:
                overlap = positive & _negative_commands(other.text)
                if overlap:
                    conflicts.append(
                        {
                            "a": rule.text,
                            "b": other.text,
                            "reason": f"contradictory command usage: {sorted(overlap)[0]}",
                        }
                    )
        if negative:
            for other in rules[index + 1:]:
                overlap = negative & _positive_commands(other.text)
                if overlap:
                    conflicts.append(
                        {
                            "a": rule.text,
                            "b": other.text,
                            "reason": f"contradictory command usage: {sorted(overlap)[0]}",
                        }
                    )
        if re.search(r"(?i)\b(?:read lots|read more|prefer reading|read the codebase)\b", rule.text):
            for other in rules[index + 1:]:
                if other.category == "read_hygiene" and re.search(r"(?i)\b(?:avoid|do not|never|whole[- ]file)\b", other.text):
                    conflicts.append(
                        {
                            "a": rule.text,
                            "b": other.text,
                            "reason": "read-more vs read-restriction",
                        }
                    )
    return conflicts


def available_text(existing: str) -> str:
    return existing if existing else "An existing target"


def resolve_agents(path: str | Path | None = None) -> list[Path]:
    candidates = [Path(path)] if path is not None else [Path("AGENTS.md"), Path("CLAUDE.md")]
    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(candidate)
    return paths


def audit_payload(
    con: duckdb.DuckDBPyConnection,
    repo: str,
    path: str | Path | Sequence[str | Path] | None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    raw_paths = (
        [Path(path)]
        if isinstance(path, (str, Path))
        else [Path(item) for item in path]
        if path is not None
        else resolve_agents()
    )
    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in raw_paths:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            paths.append(candidate)
    rules = [rule for file in paths for rule in parse_agents(file)]
    coverage = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE started_at IS NOT NULL) FROM sessions WHERE repo = ?",
        [repo],
    ).fetchone()
    filters = ["repo = ?"]
    filter_params: list[Any] = [repo]
    if since is not None:
        filters.append("started_at >= ?")
        filter_params.append(_utc_naive(since))
    if until is not None:
        filters.append("started_at < ?")
        filter_params.append(_utc_naive(until))
    filtered_sessions = con.execute(
        f"SELECT count(*) FROM sessions WHERE {' AND '.join(filters)}",
        filter_params,
    ).fetchone()[0]
    return {
        "repo": repo,
        "agents_md": [str(file) for file in paths],
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "sessions_with_timestamps": coverage[1],
        "sessions": coverage[0],
        "filtered_sessions": filtered_sessions,
        "conflicts": _rule_conflicts(rules),
        "rules": [
            audit.as_dict()
            for audit in audit_agents(
                con,
                repo,
                rules,
                since=since,
                until=until,
                root=paths[0].parent if paths else None,
            )
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
    before = audit_agents(con, repo, before_rules, until=committed_at, root=file_path.parent)
    after = audit_agents(con, repo, after_rules, since=committed_at, root=file_path.parent)
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

Definitions:
- `coverage=measured` means the rule has transcript evidence; `partial` means only one clause is measured; `unmeasured` means no detector is implemented; `dark` means the policy is not observable from tool logs.
- `opportunities` are applicable sessions; `followed` and `violations` partition them. `rate` is null below 20 opportunities.

Review these existing rules and return a JSON object with:
- `rules`: only rules that should change, each with:
  - `rule_text`: exact existing rule text
  - `diagnosis`: `unenforced`, `stale`, `ambiguous`, `violated`, or `keep`
  - `evidence`: observed/applicable/compliant/failed/violation counts
  - `enforcement`: concrete CI, pre-commit, lint, or CODEOWNERS check
  - `replacement`: optional stricter wording, or null
- `suggested_new_rules`: rules supported by evidence but missing from AGENTS.md
- Prefer rewriting conditional rules (`if`, `when`, `unless`) as direct instructions.
- Report stale backticked paths; they make the rule look current when it is not.

Do not repeat rules that are already effective. Prefer mechanical enforcement over
soft instructions. If timestamp coverage is zero, state that before/after claims
cannot be verified. Return JSON only.
"""
