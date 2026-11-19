from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import duckdb

TEST_RUN_RE = re.compile(
    r"(?i)\b(pytest|unittest|cargo test|go test|npm test|npm run test|pnpm test|"
    r"yarn test|mvn test|gradle test|dotnet test|rspec|phpunit|mix test|"
    r"swift test|make (?:test|check)|python -m (?:pytest|unittest)|"
    r"cargo (?:fmt|clippy))\b"
)
NEGATIVE_RULE_RE = re.compile(r"(?i)\b(never|do not|don't|avoid)\b")
VALIDATION_RE = re.compile(
    r"(?i)\b(test|tests|lint|typecheck|validate|validation|pytest|cargo test|go test|"
    r"npm test|vitest|jest|make (?:unit|test)|check|cargo (?:fmt|clippy))\b"
)
SCRATCH_RE = re.compile(
    r"(?i)\b(?:reproduce|repro|scratch|debug|artifact)[\w.-]*\b"
)
PATH_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+/){1,}[\w.-]+")
DOC_TARGET_RE = re.compile(r"(?i)(^|/)(readme|docs?|changelog|contributing|license)(\.|/|$)")
COMMAND_RE = re.compile(r"`([^`]+)`")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
ACTIONABLE_SECTION_RE = re.compile(
    r"(?i)(guideline|rule|command|test|lint|type|style|quality|workflow|"
    r"development|contribut|requirement|check|performance|convention)"
)
COMMAND_SECTION_RE = re.compile(
    r"(?i)(command|test|lint|type|quality|check|install|benchmark|setup|validation|verification|verify|build|run)"
)
NON_ACTIONABLE_SECTION_RE = re.compile(
    r"(?i)(about|overview|architecture|key concepts|common usage|important files|"
    r"entry points|philosophy)"
)
IMPERATIVE_RE = re.compile(
    r"(?i)^(?:run|use|add|keep|avoid|do not|don't|never|make|update|ensure|"
    r"check|prefer|follow|change|remove|write|read|test|install|validate|set|"
    r"put|create|delete|preserve|scope|inspect|handle|implement|move|rename|"
    r"include|exclude|split|return|raise|call|refactor|document|maintain|commit|"
    r"verify|confirm|open|close|edit|format|reduce|simplify)\b"
)
RULE_CUE_RE = re.compile(r"(?i)\b(must|should|required|never|avoid|do not|don't)\b")
OPTIONAL_RULE_RE = re.compile(r"(?i)^(?:you can|you may|optionally|feel free|consider)\b")


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
    scope: str = "repository"
    existing_enforcement: str = ""
    enforcement_status: str = "unknown"
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


def _category(text: str, paths: tuple[str, ...]) -> str:
    lower = text.lower()
    if re.search(r"(?i)(?:--no-verify|skip(?:ping)? (?:the )?pre-commit|pre-commit hook)", lower):
        return "hook_bypass"
    if re.search(
        r"(?i)(?:whole[- ]file|cat -n|nl -ba|read only.*range|context blast|line ranges|full re-read)",
        lower,
    ):
        return "read_hygiene"
    if SCRATCH_RE.search(lower) and any(
        token in lower for token in ("do not", "don't", "never", "keep", "outside")
    ):
        return "context_hygiene"
    if TEST_RUN_RE.search(lower) and not NEGATIVE_RULE_RE.search(lower):
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
    commands = commands or tuple(
        value for value in COMMAND_RE.findall(text)
        if not PATH_RE.fullmatch(value.strip())
    )
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


def _dedupe_key(rule: AgentRule) -> tuple:
    if rule.category == "test_command" and rule.commands:
        polarity = bool(NEGATIVE_RULE_RE.search(rule.text))
        return (
            rule.source,
            "cmd:" + _normalize_command(rule.commands[0]),
            rule.paths,
            polarity,
        )
    return (rule.source, rule.text)


def parse_agents(path: str | Path) -> list[AgentRule]:
    file_path = Path(path)
    source = file_path.name or "AGENTS.md"
    return parse_agents_text(file_path.read_text(encoding="utf-8"), source)


def _split_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[.!?])\s+(?=[A-Z`])", text)
        if len(clause.strip()) >= 12
    ]


def parse_agents_text(text: str, source: str = "AGENTS.md") -> list[AgentRule]:
    rules: list[AgentRule] = []
    seen: set[tuple[str, str]] = set()
    section = ""
    in_code = False
    code_lang = ""
    pending_code = ""
    pending_line = 0
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
                and not NON_ACTIONABLE_SECTION_RE.search(section)
                and COMMAND_SECTION_RE.search(section)
                and stripped
                and not stripped.startswith("#")
            ):
                if stripped.endswith("\\"):
                    pending_code = (pending_code + " " + stripped[:-1]).strip()
                    pending_line = pending_line or line_no
                    continue
                command = (pending_code + " " + stripped).strip()
                pending_code = ""
                rule = _make_rule(
                    f"Run `{command}`",
                    source,
                    pending_line or line_no,
                    section,
                    commands=(command,),
                )
                pending_line = 0
                key = _dedupe_key(rule)
                if key not in seen:
                    seen.add(key)
                    rules.append(rule)
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
            continue
        if not stripped:
            continue
        if section and NON_ACTIONABLE_SECTION_RE.search(section):
            continue
        if stripped[0] in "-*":
            text = stripped[1:].strip()
            if text.startswith(("*", "**")):
                continue
        else:
            numbered = re.match(r"^\d+[.)]\s+(.*)", stripped)
            if numbered:
                text = numbered.group(1).strip()
            else:
                if (
                    not section
                    or NON_ACTIONABLE_SECTION_RE.search(section)
                    or not (
                        IMPERATIVE_RE.search(stripped)
                        or COMMAND_RE.search(stripped)
                        or PATH_RE.search(stripped)
                    )
                ):
                    continue
                text = stripped
        for clause in _split_clauses(text):
            if OPTIONAL_RULE_RE.search(clause):
                continue
            if (
                not IMPERATIVE_RE.search(clause)
                and not RULE_CUE_RE.search(clause)
                and not COMMAND_RE.search(clause)
            ):
                continue
            rule = _make_rule(clause, source, line_no, section)
            key = _dedupe_key(rule)
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
    command = re.sub(r"^(?:uv run|npx|pnpm exec|yarn run|npm run)\s+", "", command)
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


def _enforcement(rule: AgentRule) -> str:
    if rule.category == "test_command":
        command = rule.commands[0] if rule.commands else "the documented validation command"
        return f"CI required check / pre-commit hook running `{command}`."
    if rule.category == "hook_bypass":
        return "Pre-commit hook plus a CI check rejecting `--no-verify` commits."
    if rule.category == "read_hygiene":
        return "A context-budget hook or CI check rejecting whole-file reads of large sources."
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


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _execution_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^\s*(?:-\s*)?(?:run|entry|command):\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        value = match.group(1).strip()
        if value in {"|", ">", "|-", ">-"}:
            index += 1
            body: list[str] = []
            while index < len(lines) and (not lines[index].strip() or lines[index][0].isspace()):
                body.append(lines[index].strip())
                index += 1
            blocks.append("\n".join(body))
            continue
        blocks.append(value)
        index += 1
    return blocks


def _command_in_text(command: str, text: str) -> bool:
    command = command.strip().lower()
    if not command:
        return False
    text = text.lower()
    tokens = command.split()
    patterns = [tokens]
    if len(tokens) > 1 and tokens[0] in {"cargo", "uv", "npm", "pnpm", "yarn", "bun"}:
        patterns.append(tokens[1:])
    for pattern in patterns:
        expression = r"(?<![\w.-])" + r"\s+".join(map(re.escape, pattern)) + r"(?![\w.-])"
        if re.search(expression, text):
            return True
    return False


def discover_enforcement(root: Path | None, rule: AgentRule) -> tuple[str, str]:
    """Return (status, source) for an existing enforcement mechanism.

    configured: CI/pre-commit/commitlint wires the rule up.
    available: a Makefile/package script exists, but no hook or CI reference.
    missing: relevant config exists but no matching mechanism.
    unknown: no inspectable repository or no concrete command.
    """
    if root is None or not root.is_dir():
        return "unknown", ""
    root = root.resolve()
    commands = [
        command
        for command in (_normalize_command(item) for item in rule.commands)
        if command and not PATH_RE.fullmatch(command)
    ]
    makefile = _text(root / "Makefile")
    targets = set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:", makefile, re.M))
    make_blocks: dict[str, str] = {}
    current = ""
    for line in makefile.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if match:
            current = match.group(1)
            make_blocks[current] = ""
        elif current:
            make_blocks[current] += line + "\n"
    available: list[str] = []
    for command in commands:
        if command.startswith("make "):
            target = command.split(None, 1)[1].split()[0]
            if target in targets:
                available.append(f"Makefile: {target}")
        for target, block in make_blocks.items():
            if _command_in_text(command, block):
                available.append(f"Makefile: {target}")
                break
    package = root / "package.json"
    if package.exists():
        try:
            scripts = json.loads(_text(package)).get("scripts", {})
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
        for command in commands:
            for name, value in scripts.items():
                if _command_in_text(command, str(value)):
                    available.append(f"package.json: {name}")
    rule_text = rule.text.lower()
    is_commit_rule = bool(re.search(r"(?i)pr title|commit message|conventional commits", rule_text))
    configured: list[str] = []
    config_files = [
        root / ".pre-commit-config.yaml",
        root / ".pre-commit-config.yml",
        root / "commitlint.config.js",
        root / "commitlint.config.cjs",
        root / ".commitlintrc",
        root / ".commitlintrc.json",
        root / ".commitlintrc.yml",
        root / ".commitlintrc.yaml",
    ]
    workflows = list((root / ".github" / "workflows").glob("*.yml"))
    workflows += list((root / ".github" / "workflows").glob("*.yaml"))
    config_files.extend(workflows)
    for config in config_files:
        content = _text(config).lower()
        if not content:
            continue
        if is_commit_rule and any(
            token in content for token in ("commitlint", "semantic-pull-request", "conventional-commits")
        ):
            configured.append(str(config.relative_to(root)))
            continue
        execution = "\n".join(_execution_blocks(content))
        if not execution:
            continue
        if any(_command_in_text(command, execution) for command in commands):
            configured.append(str(config.relative_to(root)))
        elif any(
            _command_in_text(f"make {source.split(': ', 1)[1]}", execution)
            for source in available
            if source.startswith("Makefile: ")
        ):
            configured.append(str(config.relative_to(root)))
    if configured:
        return "configured", ", ".join(configured[:2])
    if available:
        return "available", ", ".join(available[:2])
    if commands or is_commit_rule:
        return "missing", ""
    return "unknown", ""


_SOURCE_CACHE: dict[Path, list[str]] = {}


def _source_index(root: Path) -> list[str]:
    resolved = root.resolve()
    if resolved in _SOURCE_CACHE:
        return _SOURCE_CACHE[resolved]
    paths: list[str] = []
    suffixes = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
        ".kt", ".cs", ".rb", ".php", ".swift",
    }
    for path in resolved.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        relative = path.relative_to(resolved)
        if "tests" in relative.parts or "test" in relative.parts:
            continue
        paths.append(relative.as_posix())
    _SOURCE_CACHE[resolved] = paths
    return paths


def _is_test_path(path: str) -> bool:
    normalized = Path(path).as_posix()
    parts = Path(normalized).parts
    name = Path(normalized).name
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _infer_scope(rule: AgentRule, root: Path | None) -> tuple[str, ...]:
    normalized_paths = tuple(Path(path).as_posix() for path in rule.paths)
    explicit = tuple(path for path in normalized_paths if not _is_test_path(path))
    if explicit:
        return explicit
    if root is None or not root.is_dir():
        return ()
    source_paths = _source_index(root)
    inferred: list[str] = []
    for path in normalized_paths:
        if not _is_test_path(path):
            continue
        stem = Path(path).stem
        if stem.startswith("test_"):
            stem = stem[5:]
        elif stem.endswith("_test"):
            stem = stem[:-5]
        test_parts = set(Path(path).parent.parts) - {"tests", "test", "."}
        matches = [source for source in source_paths if Path(source).stem == stem]
        if matches:
            inferred.append(
                max(
                    matches,
                    key=lambda source: (
                        len(test_parts & set(Path(source).parent.parts)),
                        -len(Path(source).parts),
                    ),
                )
            )
    return tuple(dict.fromkeys(inferred))


_READ_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
    ".kt", ".cs", ".rb", ".php", ".swift",
}
_LARGE_SOURCE_BYTES = 12_000


def _read_candidate(path: str) -> str:
    return re.sub(r":\d+(?:-\d+)?$", "", path.strip().strip("`"))


def _large_source(path: str, root: Path | None, names: set[str]) -> bool:
    candidate = _read_candidate(path)
    if not candidate or Path(candidate).suffix not in _READ_SUFFIXES:
        return False
    if Path(candidate).name in names:
        return True
    if root is None:
        return False
    file_path = Path(candidate)
    if file_path.is_absolute():
        try:
            file_path = file_path.relative_to(root)
        except ValueError:
            return False
    file_path = root / file_path
    try:
        return file_path.is_file() and file_path.stat().st_size >= _LARGE_SOURCE_BYTES
    except OSError:
        return False


def _scoped_test_edits(rule: AgentRule, edit_events: list[tuple[str, str]]) -> list[tuple[str, str]]:
    lower = rule.text.lower()
    if "<crate>" in lower or ("cargo test" in lower and "crate" in lower):
        return [
            (step, target)
            for step, target in edit_events
            if target.endswith(".rs") and re.search(r"(^|/)crates/", target)
        ]
    if "snapshot" in lower:
        return [
            (step, target)
            for step, target in edit_events
            if re.search(r"(?i)(^|/)(?:tests?|fixtures?|snapshots?)/|\.snap$", target)
        ]
    if "fixture" in lower:
        return [
            (step, target)
            for step, target in edit_events
            if re.search(r"(?i)(^|/)(?:tests?|fixtures?)/", target)
        ]
    return edit_events


def _read_hygiene(events: list[tuple], root: Path | None, rule: AgentRule) -> tuple[list[str], bool]:
    names = {
        Path(name).name
        for name in re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)\b", rule.text)
        if not re.search(r"[*<>]", name)
    }
    reads: list[str] = []
    whole = False
    for _sid, _step, action, raw, target, _object, _outcome in events:
        if action not in {"read", "exec"}:
            continue
        if action == "exec" and re.search(r"\b(?:rg|grep)\b", raw) and not re.search(
            r"\b(?:cat|nl|sed|head|tail)\b", raw
        ):
            continue
        candidates = [target, *re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)", raw)]
        if not any(_large_source(path, root, names) for path in candidates):
            continue
        reads.append(raw)
        bounded = bool(
            re.search(r"\b(?:sed\s+-n|head|tail)\b|:\d+(?:-\d+)?\b", f"{raw} {target}")
        )
        shell_read = bool(re.search(r"\b(?:cat|nl\s+-ba)\b", raw))
        if not bounded and (action == "read" or shell_read):
            whole = True
    return reads, whole


def _stale_paths(rule: AgentRule, root: Path | None) -> tuple[str, ...]:
    if root is None or not root.is_dir():
        return ()
    stale: list[str] = []
    for value in COMMAND_RE.findall(rule.text):
        for candidate in PATH_RE.findall(value):
            candidate = candidate.strip().strip("`")
            if (
                not candidate
                or candidate.startswith(("/", "~"))
                or candidate.startswith(("http://", "https://"))
                or re.search(r"[*<>]", candidate)
                or (
                    len(candidate.split("/")) == 2
                    and not Path(candidate).suffix
                )
            ):
                continue
            if not (root / candidate).exists():
                stale.append(candidate)
    return tuple(dict.fromkeys(stale))


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
                # Documentation-only edits do not create a validation opportunity.
                edit_events = [
                    (step, target) for step, target in edit_events
                    if scope or not DOC_TARGET_RE.search(target or "")
                ]
                edit_events = _scoped_test_edits(rule, edit_events)
                if not edit_events:
                    continue
                edit_steps = [step for step, _target in edit_events]
                last_edit = max(edit_steps)

                def exact(event: tuple) -> bool:
                    _sid, _step, action, raw, target, object_id, _outcome = event
                    return action == "exec" and (
                        _matches(rule, raw, target, object_id)
                        if rule.commands
                        else bool(VALIDATION_RE.search(raw))
                    )

                def family(event: tuple) -> bool:
                    if event[2] != "exec":
                        return False
                    raw = _normalize_command(event[3])
                    if not rule.commands:
                        return bool(VALIDATION_RE.search(raw))
                    for command in rule.commands:
                        tokens = _normalize_command(command).split()
                        if not tokens:
                            continue
                        prefix = tokens[:2] if tokens[0] in {"cargo", "git", "uv", "npm", "pnpm"} else tokens[:1]
                        if _argv_match(" ".join(prefix), raw):
                            return True
                    return False

                post_exact = [event for event in events if event[1] > last_edit and exact(event)]
                post_any = [event for event in events if event[1] > last_edit and family(event)]
                pre_any = [event for event in events if event[1] <= last_edit and (exact(event) or family(event))]
                matched = any(exact(event) for event in events)
                if matched:
                    observed += 1
                applicable += 1
                if not matched:
                    not_observed += 1
                if post_exact:
                    if any(event[6] == "ok" for event in post_exact):
                        compliant += 1
                        outcome = "exact_form"
                    else:
                        violations += 1
                        outcome = "failed_test"
                        failed_validation += 1
                elif post_any:
                    undecided_count += 1
                    outcome = "alternate_form"
                elif pre_any:
                    violations += 1
                    outcome = "before_final_edit"
                else:
                    violations += 1
                    outcome = "no_test"
                test_outcomes[outcome] = test_outcomes.get(outcome, 0) + 1
                examples.extend(event[3][:240] for event in (post_exact or post_any or pre_any)[:2])
            elif rule.category == "read_hygiene":
                reads, whole = _read_hygiene(events, root, rule)
                if not reads:
                    continue
                applicable += 1
                observed += 1
                examples.extend(reads[:2])
                if whole:
                    violations += 1
                else:
                    compliant += 1
            elif rule.category == "hook_bypass":
                commit_events = [
                    raw for _sid, _step, action, raw, _target, _object, _outcome in events
                    if action == "exec" and re.search(r"\bgit\s+commit\b", raw)
                ]
                if not commit_events:
                    continue
                applicable += 1
                observed += 1
                examples.extend(commit_events[:2])
                if any("--no-verify" in raw for raw in commit_events):
                    violations += 1
                else:
                    compliant += 1
            elif rule.category == "context_hygiene":
                if not edit_events and not matched:
                    continue
                applicable += 1
                if not matched:
                    not_observed += 1
                if any(
                    action == "edit"
                    and (
                        _matches(rule, raw, target, object_id)
                        or (not _needles(rule) and SCRATCH_RE.search(object_id or target))
                    )
                    and not target.startswith(("/tmp/", "tmp/"))
                    for _sid, _step, action, raw, target, object_id, _outcome in events
                ):
                    violations += 1
            else:
                applicable += 1
                if not matched:
                    not_observed += 1

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
        measurable = rule.category in {"test_command", "hook_bypass", "read_hygiene", "context_hygiene"}
        opportunities = applicable if measurable else 0
        undecided = 0
        if rule.category == "test_command":
            followed = compliant
            undecided = undecided_count
            violations = max(applicable - followed - undecided, 0)
        elif rule.category in {"hook_bypass", "read_hygiene"}:
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
    return audits


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
