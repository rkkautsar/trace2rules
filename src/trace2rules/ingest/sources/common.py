from __future__ import annotations

import json
from typing import Any

from ..normalize import _classify_shell, _object_id, _parameterize, _shell_verb_tokens, normalize_path

SHELL_TOOLS = {
    "bash",
    "cmd",
    "exec",
    "exec_command",
    "execute",
    "local_shell",
    "powershell",
    "run_command",
    "shell",
    "shell_command",
    "terminal",
}
READ_TOOLS = {"cat", "head", "open", "read", "read_file", "tail", "view"}
EDIT_TOOLS = {
    "apply_patch",
    "create",
    "edit",
    "edit_file",
    "multiedit",
    "patch",
    "replace",
    "str_replace",
    "write",
    "write_file",
}
SEARCH_TOOLS = {"find", "glob", "grep", "ls", "list_directory", "rg", "search"}


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def target_from_args(tool_name: str, args: dict[str, Any], fallback: str = "") -> str:
    """Return the most useful command/path/pattern for a tool call."""
    name = tool_name.lower()
    for key in ("command", "cmd", "command_line", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("file_path", "filePath", "path", "filename", "file"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("pattern", "query", "description", "prompt"):
        value = args.get(key)
        if isinstance(value, str) and value.strip() and name in SEARCH_TOOLS:
            return value.strip()
    if fallback:
        return fallback
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def _path_from_args(args: dict[str, Any]) -> str:
    for key in ("file_path", "filePath", "path", "filename", "file"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def classify_tool_call(tool_name: str, args: dict[str, Any], raw_target: str) -> tuple[str, str, str, str]:
    """Map a normalized tool call to action/target/verb/object_id."""
    name = tool_name.strip().lower()
    if name in SHELL_TOOLS:
        action, target = _classify_shell(raw_target, "")
        verb, tokens = _shell_verb_tokens(raw_target)
        object_id = _object_id(action, verb, tokens, raw_target, target)
        return action, target, verb, object_id

    path = _path_from_args(args)
    if name in READ_TOOLS:
        target = normalize_path(path) if path else raw_target
        return "read", target, name, target or name
    if name in EDIT_TOOLS:
        target = normalize_path(path) if path else normalize_path(".", "")
        object_id = target if path else name
        return "edit", target, name, object_id
    if name in SEARCH_TOOLS:
        target = normalize_path(path) if path else raw_target
        return "search", target, name, target or name

    target = _parameterize(raw_target, "")
    return "other", target, name, target or name
