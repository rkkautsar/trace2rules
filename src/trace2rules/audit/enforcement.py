from __future__ import annotations

import json
import re
from pathlib import Path

from .models import AgentRule
from .parsing import PATH_RE, _normalize_command


def enforcement_for(rule: AgentRule) -> str:
    if rule.category == "test_command":
        command = rule.commands[0] if rule.commands else "the documented validation command"
        return f"CI required check / pre-commit hook running `{command}`."
    if rule.category == "hook_bypass":
        return "Pre-commit hook plus a CI check rejecting `--no-verify` commits."
    if rule.category == "read_hygiene":
        return "A context-budget hook or CI check rejecting whole-file reads of large sources."
    if rule.category == "read_before_edit":
        return "A pre-edit hook requiring source reads before edits."
    if rule.category == "ordering":
        return "A harness/tool-use hook enforcing agent command order; pre-commit and CI cannot enforce it."
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
