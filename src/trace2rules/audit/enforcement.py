from __future__ import annotations

import json
import re
from pathlib import Path

from .models import AgentRule
from .parsing import PATH_RE, _normalize_command


def enforcement_for(rule: AgentRule) -> str:
    if rule.category == "command":
        command = rule.commands[0] if rule.commands else "the documented command"
        return f"CI/pre-commit check running `{command}`."
    if rule.category == "path_scope":
        return "Import-boundary lint, CODEOWNERS, or a required path check."
    if rule.category in {"test_command", "validation"}:
        command = rule.commands[0] if rule.commands else "the documented validation command"
        return f"CI required check / pre-commit hook running `{command}`."
    if rule.category == "hook_bypass":
        return "Pre-commit hook plus a CI check rejecting `--no-verify` commits."
    if rule.category == "read_hygiene":
        return "No mechanical enforcement; harness/context-budget hooks can observe reads."
    if rule.category == "read_before_edit":
        return "No mechanical enforcement; harness hooks can observe reads before edits."
    if rule.category == "ordering":
        return "No mechanical enforcement; command order is harness-only guidance."
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
    if path.is_symlink():
        return ""
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


_IGNORED_FLAGS = {"-q", "--quiet", "-v", "--verbose"}


def _command_in_text(command: str, text: str) -> bool:
    command = command.strip().lower()
    if not command:
        return False
    text = text.lower()
    tokens = [token for token in command.split() if token not in _IGNORED_FLAGS]
    patterns = [tokens]
    if len(tokens) > 1 and tokens[0] in {"cargo", "uv", "npm", "pnpm", "yarn", "bun"}:
        patterns.append(tokens[1:])
    for pattern in patterns:
        expression = r"(?<![\w.-])" + r"\s+".join(map(re.escape, pattern)) + r"(?![\w.-])"
        if re.search(expression, text):
            return True
    return False


def _config_files(root: Path) -> list[Path]:
    files = [
        root / ".gitlab-ci.yml",
        root / ".circleci" / "config.yml",
        root / "azure-pipelines.yml",
        root / ".travis.yml",
        root / ".buildkite" / "pipeline.yml",
        root / ".buildkite" / "pipeline.yaml",
        root / "Jenkinsfile",
        root / ".drone.yml",
        root / "lefthook.yml",
        root / ".husky" / "pre-commit",
        root / ".git" / "hooks" / "pre-commit",
        root / ".git" / "hooks" / "pre-push",
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
    return files + workflows


_COMMAND_START_RE = re.compile(
    r"(?i)^(?:uv|python3?|pytest|tox|nox|make|just|task|npm|pnpm|yarn|bun|"
    r"cargo|go|mvn|gradle|dotnet|bundle|rspec|phpunit|mix|swift|"
    r"pre-commit|ruff|eslint|mypy|tsc|git)\b"
)


def _config_commands(text: str) -> list[str]:
    commands: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-").strip()
        line = re.sub(
            r"(?i)^(?:run|script|command|entry|steps?):\s*",
            "",
            line,
        ).strip()
        if not line or line.startswith("#"):
            continue
        if _COMMAND_START_RE.match(line):
            commands.append(line[:300])
    return list(dict.fromkeys(commands))[:20]


def enforcement_inventory(root: Path | None) -> dict[str, object]:
    if root is None or not root.is_dir():
        return {}
    root = root.resolve()
    configs = []
    for path in _config_files(root):
        text = _text(path)
        if not text:
            continue
        commands = _config_commands(text)
        if commands:
            configs.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "commands": commands,
                }
            )
    makefile = _text(root / "Makefile")
    make_targets = sorted(
        set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:", makefile, re.M))
    )
    scripts: list[str] = []
    package = root / "package.json"
    if package.exists():
        try:
            scripts = sorted(json.loads(_text(package)).get("scripts", {}).keys())
        except (json.JSONDecodeError, AttributeError):
            scripts = []
    return {
        "configs": configs,
        "make_targets": make_targets,
        "package_scripts": scripts,
        "hooks": [
            path.relative_to(root).as_posix()
            for path in (root / ".git" / "hooks").glob("*")
            if path.is_file() and not path.name.endswith(".sample")
        ],
    }


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
    for command in commands:
        if command == "tox" or command.startswith("tox "):
            if (root / "tox.ini").is_file():
                available.append("tox.ini")
        if command == "nox" or command.startswith("nox "):
            if (root / "noxfile.py").is_file():
                available.append("noxfile.py")
        if command == "just" or command.startswith("just "):
            if (root / "justfile").is_file() or (root / "Justfile").is_file():
                available.append("justfile")
        if command == "task" or command.startswith("task "):
            if (root / "Taskfile.yml").is_file() or (root / "Taskfile.yaml").is_file():
                available.append("Taskfile")
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
    config_files = _config_files(root)
    for config in config_files:
        content = _text(config).lower()
        if not content:
            continue
        if is_commit_rule and any(
            token in content for token in ("commitlint", "semantic-pull-request", "conventional-commits")
        ):
            configured.append(str(config.relative_to(root)))
            continue
        is_github_workflow = ".github/workflows" in config.as_posix()
        execution = "\n".join(_execution_blocks(content)) if is_github_workflow else content
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
