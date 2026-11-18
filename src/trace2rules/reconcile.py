from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .agents import PATH_RE
from .synthesize import Rule

COMMAND_RE = re.compile(r"`([^`]+)`")


@dataclass(slots=True)
class ReconcileAction:
    action: Literal["add", "drop", "update", "keep"]
    proposed: str
    existing: str = ""
    reason: str = ""
    enforcement: str = ""
    enforcement_status: str = ""


def _command_signature(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", value)
    value = re.sub(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+",
        "",
        value,
    )
    value = re.sub(r"^(?:timeout\s+\S+\s+)", "", value)
    return value.strip()


def _commands(text: str) -> tuple[str, ...]:
    values = []
    for item in COMMAND_RE.findall(text):
        normalized = _command_signature(item)
        if normalized and not PATH_RE.fullmatch(normalized):
            values.append(normalized)
    return tuple(dict.fromkeys(values))


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
        if token.lower() not in {"that", "this", "with", "from", "when", "then", "should", "never"}
    }


def _text_match(proposed: str, existing: str) -> bool:
    left, right = _tokens(proposed), _tokens(existing)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= 0.65


def reconcile(
    proposed: list[Rule], existing: list[dict]
) -> tuple[list[Rule], list[ReconcileAction]]:
    actions: list[ReconcileAction] = []
    kept: list[Rule] = []
    for rule in proposed:
        commands = _commands(rule.rule_markdown)
        duplicate = None
        for old in existing:
            old_commands = _commands(old.get("text", ""))
            same_command = bool(commands and old_commands and set(commands) & set(old_commands))
            same_text = _text_match(rule.rule_markdown, old.get("text", ""))
            if same_command or same_text:
                duplicate = old
                break
        if duplicate is None:
            kept.append(rule)
            actions.append(ReconcileAction("add", rule.rule_markdown))
            continue
        status = duplicate.get("enforcement_status", "unknown")
        enforcement = duplicate.get("enforcement", "")
        if status in {"configured", "available"}:
            actions.append(
                ReconcileAction(
                    "drop",
                    rule.rule_markdown,
                    duplicate.get("text", ""),
                    f"existing rule has {status} enforcement",
                    enforcement,
                    status,
                )
            )
            continue
        if rule.enforceable and rule.enforcement:
            kept.append(rule)
            actions.append(
                ReconcileAction(
                    "update",
                    rule.rule_markdown,
                    duplicate.get("text", ""),
                    "replace soft existing rule with enforceable version",
                    rule.enforcement,
                    status,
                )
            )
            continue
        actions.append(
            ReconcileAction(
                "keep",
                rule.rule_markdown,
                duplicate.get("text", ""),
                "existing rule exists but is not enforced",
                enforcement,
                status,
            )
        )
    return kept, actions
