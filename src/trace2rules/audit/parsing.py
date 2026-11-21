from __future__ import annotations

import re
from pathlib import Path

from .models import AgentRule

TEST_RUN_RE = re.compile(
    r"(?i)\b(pytest|unittest|cargo test|go test|npm test|npm run test|pnpm test|"
    r"yarn test|pdm (?:run )?test|mvn test|gradle test|dotnet test|rspec|phpunit|mix test|"
    r"swift test|make (?:test|check|unit)|python -m (?:pytest|unittest)|"
    r"cargo (?:fmt|clippy))\b"
)
VALIDATION_RUN_RE = re.compile(
    r"(?i)\b(pdm run lint|npm run lint|yarn lint|pnpm lint|ruff|eslint|prettier|"
    r"mypy|pyright|tsc|biome|cargo fmt|cargo clippy|make (?:lint|style))\b"
)
COMMAND_TOKENS = {
    "bash", "cargo", "cd", "curl", "docker", "git", "gh", "go", "make",
    "npm", "npx", "pnpm", "pip", "pdm", "python", "pytest", "rg", "sed", "sh",
    "uv", "yarn",
}


def _command_rule(text: str, commands: tuple[str, ...]) -> bool:
    if not commands or not re.search(
        r"(?i)^\s*(?:always\s+)?(?:run|rerun|use|execute|invoke|call|find)\b", text
    ):
        return False
    for command in commands:
        parts = command.strip().split()
        if not parts:
            continue
        token = re.sub(r"^[A-Z_][A-Z0-9_]*=", "", parts[0])
        token = token.lstrip("(").lower()
        if token in COMMAND_TOKENS:
            return True
    return False
NEGATIVE_RULE_RE = re.compile(r"(?i)\b(never|do not|don't|avoid)\b")
LOCKFILE_RE = re.compile(
    r"(?i)(?:^|/)(?:Cargo\.lock|uv\.lock|pdm\.lock|package-lock\.json|"
    r"pnpm-lock\.yaml|yarn\.lock)$"
)
VALIDATION_RE = re.compile(
    r"(?i)\b(test|tests|lint|typecheck|validate|validation|pytest|cargo test|go test|"
    r"npm test|vitest|jest|pdm (?:run )?test|make (?:unit|test)|check|cargo (?:fmt|clippy))\b"
)
SCRATCH_RE = re.compile(r"(?i)\b(?:reproduce|repro|scratch|debug|artifact)[\w.-]*\b")
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
    r"entry points|philosophy|key components|project structure|repository layout|"
    r"module layout)"
)
SYNTHESIS_CATEGORIES = {
    "test_command", "validation", "command", "path_scope", "git_workflow",
    "commit_hygiene", "architecture_map", "context_hygiene", "convention",
}
IMPERATIVE_RE = re.compile(
    r"(?i)^(?:run|use|add|keep|avoid|do not|don't|never|make|update|ensure|"
    r"check|prefer|follow|change|remove|write|read|test|install|validate|set|"
    r"put|create|delete|preserve|scope|inspect|handle|implement|move|rename|"
    r"include|exclude|split|return|raise|call|refactor|document|maintain|commit|"
    r"verify|confirm|open|close|edit|format|reduce|simplify)\b"
)
RULE_CUE_RE = re.compile(r"(?i)\b(must|should|required|never|avoid|do not|don't)\b")
OPTIONAL_RULE_RE = re.compile(r"(?i)^(?:you can|you may|optionally|feel free|consider)\b")


def _normalize_command(command: str) -> str:
    command = command.strip()
    if command.startswith("(") and command.endswith(")"):
        command = command[1:-1].strip()
    command = re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", command)
    command = re.sub(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+",
        "",
        command,
    )
    command = re.sub(r"^(?:timeout\s+\S+\s+)", "", command)
    command = re.sub(r"^(?:uv run|npx|pnpm exec|yarn run|npm run)\s+", "", command)
    return command.strip().lower()


def _category(text: str, paths: tuple[str, ...]) -> str:
    lower = text.lower()
    if re.search(r"(?i)(?:--no-verify|skip(?:ping)? (?:the )?pre-commit|pre-commit hook)", lower):
        return "hook_bypass"
    if re.search(
        r"(?i)(?:force[- ]push|--force(?:-with-lease)?\b|git reset --hard|git clean|"
        r"publish (?:packages?|releases?)|npm publish|cargo publish|pdm publish|"
        r"poetry publish|twine upload|gh release)",
        lower,
    ):
        return "git_workflow"
    if re.search(
        r"(?i)\bcommit\b.*\b(?:frequent|frequently|small|batch|early|often)\b|"
        r"\bcommit (?:your|the) work\b",
        lower,
    ):
        return "commit_hygiene"
    if (
        paths
        and NEGATIVE_RULE_RE.search(lower)
        and any(LOCKFILE_RE.search(path) for path in paths)
    ):
        return "lockfile"
    if re.search(
        r"(?i)(?:whole[- ]file|cat -n|nl -ba|read only.*range|context blast|line ranges|full re-read)",
        lower,
    ):
        return "read_hygiene"
    if re.search(
        r"(?i)(?:read (?:the )?codebase|read (?:lots of |more )?source|"
        r"verify assumptions.*read|read(?:ing)? (?:source|fixtures)|"
        r"read (?:the )?(?:target|file).*before (?:editing|edit)|"
        r"before (?:editing|edit).*\bread\b)",
        lower,
    ):
        return "read_before_edit"
    commands = COMMAND_RE.findall(text)
    if len(commands) >= 2 and re.search(r"(?i)\bonly after\b", lower):
        return "ordering"
    if commands and re.search(r"(?i)\bbefore (?:running |running the )?tests?\b", lower):
        return "ordering"
    if SCRATCH_RE.search(lower) and any(
        token in lower for token in ("do not", "don't", "never", "keep", "outside")
    ):
        return "context_hygiene"
    if VALIDATION_RUN_RE.search(lower) and not NEGATIVE_RULE_RE.search(lower):
        return "validation"
    if TEST_RUN_RE.search(lower) and not NEGATIVE_RULE_RE.search(lower):
        return "test_command"
    if (
        paths
        and not NEGATIVE_RULE_RE.search(lower)
        and re.match(r"(?i)^(?:create|add|register|write|update|modify|put)\b", lower)
    ):
        return "path_scope"
    if _command_rule(text, tuple(COMMAND_RE.findall(text))) and not paths:
        return "command"
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
    source_path: str = "",
    source_scope: str = "repo",
) -> AgentRule:
    commands = commands or tuple(
        value for value in COMMAND_RE.findall(text)
        if not PATH_RE.fullmatch(value.strip())
    )
    lockfiles = tuple(
        value.strip()
        for value in COMMAND_RE.findall(text)
        if LOCKFILE_RE.fullmatch(value.strip())
    )
    paths = paths or tuple(
        dict.fromkeys((*PATH_RE.findall(text), *lockfiles))
    )
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
        source_path,
        source_scope,
    )


def _dedupe_key(rule: AgentRule) -> tuple:
    if rule.category in {"test_command", "validation", "command"} and rule.commands:
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
    if file_path.is_symlink() or not file_path.is_file():
        return []
    try:
        if file_path.stat().st_size > 1_000_000:
            return []
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    return parse_agents_text(text, source, str(file_path.resolve()))


def _split_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[.!?])\s+(?=[A-Z`])", text)
        if len(clause.strip()) >= 12
    ]


def parse_agents_text(
    text: str,
    source: str = "AGENTS.md",
    source_path: str = "",
    source_scope: str = "repo",
) -> list[AgentRule]:
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
                    source_path=source_path,
                    source_scope=source_scope,
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
        if (
            section
            and section.lower() not in SYNTHESIS_CATEGORIES
            and NON_ACTIONABLE_SECTION_RE.search(section)
        ):
            continue
        if stripped.startswith(("**Evidence:**", "**Enforcement:**", "**Source:**")):
            continue
        explicit_rule = False
        if stripped[0] in "-*":
            text = stripped[1:].strip()
            explicit_rule = text.startswith("**Rule:**")
            if explicit_rule:
                text = text[len("**Rule:**"):].strip()
            elif text.startswith(("*", "**")):
                continue
        else:
            numbered = re.match(r"^\d+[.)]\s+(.*)", stripped)
            if numbered:
                text = numbered.group(1).strip()
            else:
                if (
                    not section
                    or (
                        section.lower() not in SYNTHESIS_CATEGORIES
                        and NON_ACTIONABLE_SECTION_RE.search(section)
                    )
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
                not explicit_rule
                and not IMPERATIVE_RE.search(clause)
                and not RULE_CUE_RE.search(clause)
                and not COMMAND_RE.search(clause)
            ):
                continue
            rule = _make_rule(
                clause,
                source,
                line_no,
                section,
                source_path=source_path,
                source_scope=source_scope,
            )
            key = _dedupe_key(rule)
            if key in seen:
                continue
            seen.add(key)
            rules.append(rule)
    return rules
