from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from functools import lru_cache
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb
from rich.progress import Progress

from .db import DB_PATH, connect
from .models import Session, ToolCall

SOURCE = "hf://datasets/nebius/SWE-rebench-openhands-trajectories/trajectories.parquet"
_OPEN_SWE_BASE = (
    "https://huggingface.co/datasets/nvidia/Open-SWE-Traces/resolve/main/"
    "data/openhands/qwen38_flash_next/swe-rebench-v2/"
)
OPEN_SWE_SOURCE = _OPEN_SWE_BASE + "train-*.parquet"
OPEN_SWE_FILES = [_OPEN_SWE_BASE + f"train-0000{i}-of-00006.parquet" for i in range(6)]
DEFAULT_REPOS = ["django__django", "pytest-dev__pytest"]

EXIT_RE = re.compile(
    r"\[(?:the command completed|command finished) with exit code (-?\d+)\]",
    re.IGNORECASE,
)
EXIT_FALLBACK_RE = re.compile(r"(?:Exit code|exit code):\s*(-?\d+)")
TEXT_ERROR_RE = re.compile(r"Traceback|Error:|ModuleNotFoundError|No such file or directory")
TOOL_ERROR_RE = re.compile(
    r"Invalid\s+`?path`?|File not found|No such file or directory|does not exist|Permission denied",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(r"timed out|Timeout", re.IGNORECASE)
CAUSE_LINE_RE = re.compile(
    r"(?i)(^|\b)(FAILED|ERROR|Traceback|usage:|no tests ran|deselected|"
    r"[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::|\b)"
)
SITE_FILE_RE = re.compile(
    r'(?:File "([^"]+)", line (\d+)|([\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|kts|c|h|cpp|hpp|cs|rb|php|swift|scala|sh)):(\d+))'
)
SITE_TEST_RE = re.compile(r"FAILED\s+([^\s]+::\S+)")
CONTROL_TARGETS = {"C-c", "C-C", "^C", "ctrl-c", "\x03"}

ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
CD_PREFIX_RE = re.compile(r"^cd\s+[^&;]+\s*(?:&&|;)\s*")
EXPORT_PREFIX_RE = re.compile(r"^export\s+[^&;]+\s*(?:&&|;)\s*")
EDIT_VERBS = {"apply_patch", "patch"}
READ_VERBS = {"cat", "head", "tail", "less", "more", "wc", "nl", "ls", "tree", "stat", "file", "du", "sed", "awk"}
SEARCH_VERBS = {"grep", "rg", "ag", "ack", "fd", "find"}
GREP_VERBS = {"grep", "rg", "ag", "ack", "git grep"}
EDITOR_READ_COMMANDS = {"view", "show"}
FLAG_WITH_VALUE = {"-e", "-A", "-B", "-C", "-m", "-g", "-t", "--include", "--exclude", "--glob", "--type", "-name", "-path", "-type"}


@dataclass
class IngestStats:
    prefix: str
    matched: int
    sessions: int
    events: int


def _is_path_token(token: str) -> bool:
    if token.startswith("-") or token in {"=", "&&", ";", "|"}:
        return False
    if token.startswith(("2>", "1>", "&>")) or any(char in token for char in "<>|&"):
        return False
    if token.startswith(("skill:", "xd:")):
        return False
    if "/" in token:
        return True
    return bool(
        re.search(
            r"\.(py|pyi|txt|md|rst|toml|cfg|ini|yaml|yml|json|ts|tsx|js|jsx|go|rs|"
            r"java|kt|kts|c|h|cpp|hpp|cs|rb|php|swift|scala|sh|sql|gradle|csproj|sln)$",
            token,
        )
    )


def normalize_path(path: str, instance_id: str = "") -> str:
    path = (path or "").strip().strip("'\"").rstrip(";,)")
    if not path:
        return ""
    if path.startswith(("skill:", "xd:")):
        return ""
    path = path.replace("\\", "/")
    if path.rstrip("/") in ("/workspace", "/testbed", "/repo", "/app"):
        return "."
    for root in ("/workspace/", "/testbed/", "/repo/", "/app/"):
        if path.startswith(root):
            path = path[len(root) :]
            workspace_root = root == "/workspace/"
            break
    else:
        workspace_root = False
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if workspace_root and parts and (len(parts) > 1 or "__" in parts[0]):
        parts = parts[1:]
    elif parts and ("__" in parts[0] or parts[0] == instance_id):
        parts = parts[1:]
    path = "/".join(parts)
    return path or "."


def _shell_split(command: str) -> list[str]:
    if '"' not in command and "'" not in command and "\\" not in command:
        return command.split()
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _strip_shell_prefix(command: str) -> str:
    command = command.strip()
    while True:
        before = command
        command = ENV_PREFIX_RE.sub("", command)
        command = CD_PREFIX_RE.sub("", command)
        command = EXPORT_PREFIX_RE.sub("", command)
        if command == before:
            return command


@lru_cache(maxsize=100_000)
def _first_verb(command: str) -> tuple[str, list[str]]:
    command = _strip_shell_prefix(command)
    tokens = _shell_split(command)
    if not tokens:
        return "", []
    if tokens[0] == "git" and len(tokens) > 1:
        return f"git {tokens[1]}", tokens[2:]
    return tokens[0], tokens[1:]


def _basename(path: str) -> str:
    path = (path or "").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _shell_verb_tokens(command: str) -> tuple[str, list[str]]:
    verb, tokens = _first_verb(command)
    if verb in ("python", "python3"):
        if "pytest" in tokens:
            return "pytest", tokens
        if "-m" in tokens:
            index = tokens.index("-m")
            module = tokens[index + 1] if index + 1 < len(tokens) else ""
            if module in ("pytest", "pip"):
                return module, tokens
            return module or verb, tokens
    if verb.startswith("git "):
        return "git", [verb.split(" ", 1)[1], *tokens]
    if verb.startswith("pip"):
        return "pip", tokens
    return verb, tokens


def _find_object(tokens: list[str]) -> str:
    path = ""
    name = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-name", "-iname") and index + 1 < len(tokens):
            name = tokens[index + 1]
            index += 2
            continue
        if not path and not token.startswith("-"):
            path = token
        index += 1
    if name:
        if path:
            path = normalize_path(path).rstrip("/")
        return f"{path}/{name}" if path not in ("", ".") else name
    return normalize_path(path) if path else "."


def _object_id(
    action: str, verb: str, tokens: list[str], raw_command: str, target: str
) -> str:
    if action == "search" and verb == "find":
        return _find_object(tokens)
    if action in ("read", "edit", "search") and " " not in target:
        if not target or target == ".":
            return "" if action == "search" else "."
        return _basename(target) if action in ("read", "edit") else target

    if verb == "python" and "-c" in tokens:
        payload = raw_command.split("-c", 1)[1].strip() if "-c" in raw_command else ""
        match = re.search(r"\b(?:import|from)\s+([A-Za-z_][\w.]*)", payload)
        if match:
            return match.group(1)
        return "inline:" + hashlib.sha1(payload.encode()).hexdigest()[:8]

    if verb == "pytest":
        paths = [token for token in tokens if _is_path_token(token)]
        return _basename(paths[-1]) if paths else "pytest"

    if verb == "python":
        if "-m" in tokens:
            index = tokens.index("-m")
            return tokens[index + 1] if index + 1 < len(tokens) else "python"
        for token in tokens:
            if _is_path_token(token):
                return _basename(token)
        return ""

    if verb == "pip" and "install" in tokens:
        args = tokens[tokens.index("install") + 1 :]
        if "-e" in args:
            index = args.index("-e")
            destination = args[index + 1] if index + 1 < len(args) else ""
            return "editable" if destination in (".", "./") else _basename(destination)
        packages = [arg for arg in args if not arg.startswith("-")]
        return ",".join(packages[:2]) or "install"
    if verb == "pip":
        return ""

    if verb == "git":
        return tokens[0] if tokens else ""
    if verb == "export":
        match = re.search(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)", raw_command)
        return match.group(1) if match else ""
    paths = [token for token in tokens if _is_path_token(token)]
    if paths:
        return _basename(paths[-1])
    if action == "exec" and target and " " not in target:
        return _basename(target)
    return ""


def _extract_exit_code(observation: str) -> int | None:
    match = EXIT_RE.search(observation) or EXIT_FALLBACK_RE.search(observation)
    return int(match.group(1)) if match else None


def _is_control_target(raw_target: str) -> bool:
    value = (raw_target or "").strip()
    return not value or value in CONTROL_TARGETS or value.startswith("\x03")


def _observation_cause(observation: str) -> str:
    if not observation:
        return ""
    lines = observation.splitlines()
    for index, line in enumerate(lines):
        if CAUSE_LINE_RE.search(line):
            if line.lstrip().startswith("Traceback"):
                for detail in lines[index + 1 : index + 8]:
                    if CAUSE_LINE_RE.search(detail):
                        return "\n".join([detail, *lines[index + 1 : index + 3]])[:400]
            return "\n".join(lines[index : index + 3])[:400]
    return observation[-400:]


def _error_detail(observation: str) -> str:
    if not observation:
        return ""
    for line in observation.splitlines():
        if line.lstrip().startswith(("raise ", "raise(", "raise\t")):
            continue
        if re.search(r"(?:^|\b)(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::|\b)", line):
            detail = line.strip()
            attribute = re.search(
                r"(?:has no attribute|no attribute) (['\"])([^'\"]+)\1", detail
            )
            module = re.search(r"No module named (['\"])([^'\"]+)\1", detail)
            imported = re.search(
                r"cannot import name (['\"])([^'\"]+)\1 from (['\"])([^'\"]+)\3",
                detail,
            )
            if attribute:
                detail = f"AttributeError: <object> has no attribute '{attribute.group(2)}'"
            elif module:
                detail = f"ModuleNotFoundError: No module named '{module.group(2)}'"
            elif imported:
                detail = (
                    "ImportError: cannot import name "
                    f"'{imported.group(2)}' from '{imported.group(4)}'"
                )
            else:
                detail = re.sub(r"'[^']*'", "'<id>'", detail)
                detail = re.sub(r'"[^"]*"', '"<id>"', detail)
            detail = re.sub(r"/(?:workspace|testbed|repo|app)/[^\s)]+", "<workspace>", detail)
            detail = re.sub(r"\b\d+\b", "<n>", detail)
            return detail[:240]
    return ""


def _error_origin(observation: str) -> str:
    lowered = observation.lower()
    env_markers = (
        "modulenotfounderror",
        "importerror",
        "cannot find module",
        "no such module",
        "module not found",
        "class not found",
        "package not found",
        "could not resolve",
    )
    if any(marker in lowered for marker in env_markers):
        return "env"
    if "assertionerror" in lowered or "failed" in lowered:
        return "repo"
    if "permission denied" in lowered or "no such file" in lowered:
        return "env"
    return "unknown"


def _event_hash(raw_target: str, observation: str) -> str:
    return hashlib.sha1(
        f"{raw_target}\x1f{observation}".encode("utf-8", "replace")
    ).hexdigest()[:16]


def _error_site(observation: str) -> str:
    if not observation:
        return ""
    match = SITE_TEST_RE.search(observation)
    if match:
        return match.group(1)[:200]
    match = SITE_FILE_RE.search(observation)
    if match:
        path = match.group(1) or match.group(3)
        line = match.group(2) or match.group(4)
        path = normalize_path(path, "")
        return f"{path}:{line}"[:200]
    return ""


def _error_signature(
    observation: str,
    exit_code: int | None,
    timeout: bool,
    text_error: bool,
    tool_error: bool,
) -> str:
    if timeout:
        return "timeout"
    lowered = observation.lower()
    if "did not appear verbatim" in lowered:
        return "editor:verbatim"
    if "invalid `path`" in lowered or "invalid path" in lowered:
        return "editor:invalid_path"
    if "error" not in lowered and "exception" not in lowered:
        if exit_code not in (None, 0):
            return f"exit:{exit_code}"
        if text_error:
            return "text_error"
        if tool_error:
            return "tool_error"
        return ""
    matches = re.findall(
        r"(?:^|\n)\s*(?:E\s+)?([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::|\s)",
        observation,
    )
    if matches:
        return f"exception:{matches[-1]}"
    if exit_code not in (None, 0):
        return f"exit:{exit_code}"
    if text_error:
        return "text_error"
    if tool_error:
        return "tool_error"
    return ""


def _event_outcome(
    action: str,
    observation: str,
    exit_code: int | None,
    text_error: bool,
    timeout: bool,
    tool_error: bool,
    verb: str = "",
) -> str:
    if action == "search" and exit_code == 1 and verb in GREP_VERBS:
        return "empty"
    if exit_code is not None:
        return "ok" if exit_code == 0 else "err"
    if timeout:
        return "timeout"
    if action == "edit":
        if tool_error or text_error:
            return "err"
        return "ok" if observation else "unknown"
    if action == "exec" and text_error:
        return "err"
    if action in ("read", "search"):
        if tool_error:
            return "err"
        return "ok" if observation else "unknown"
    return "unknown"


def _path_args(verb: str, tokens: list[str]) -> list[str]:
    if verb == "find":
        for token in tokens:
            if not token.startswith("-"):
                return [token]
        return []
    out: list[str] = []
    skip_next = False
    pattern_token: str | None = None
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in FLAG_WITH_VALUE:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if verb in {"grep", "rg", "ag", "ack", "git grep"} and pattern_token is None:
            pattern_token = token
            continue
        if _is_path_token(token):
            out.append(token)
    return out


def _classify_shell(command: str, instance_id: str) -> tuple[str, str]:
    verb, tokens = _first_verb(command)
    lower = command.lower()
    if verb in EDIT_VERBS or lower.startswith("git apply") or re.search(r"\bsed\s+-i|\bperl\s+-[a-z]*i", lower):
        paths = _path_args(verb, tokens)
        return "edit", normalize_path(paths[-1], instance_id) if paths else _parameterize(command, instance_id)
    if verb in SEARCH_VERBS:
        paths = _path_args(verb, tokens)
        return "search", normalize_path(paths[0], instance_id) if paths else ""
    if verb in READ_VERBS:
        paths = _path_args(verb, tokens)
        return "read", normalize_path(paths[0], instance_id) if paths else "."
    if verb in {"git diff", "git log", "git status", "git show", "git blame"}:
        return "read", verb
    return "exec", _parameterize(command, instance_id)


def _parameterize(command: str, instance_id: str) -> str:
    command = _strip_shell_prefix(" ".join((command or "").split()))
    if not command:
        return ""
    tokens = _shell_split(command)

    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-k" and i + 1 < len(tokens):
            out.extend([token, "<arg>"])
            i += 2
            continue
        if _is_path_token(token):
            out.append("<path>")
        else:
            out.append(token)
        i += 1
    result = " ".join(out)
    return result[:240]


def _classify(tool_name: str, raw_target: str, instance_id: str) -> tuple[str, str, str, str]:
    if tool_name == "str_replace_editor":
        command = raw_target.split("\x00", 1)[0]
        path = raw_target.split("\x00", 1)[1] if "\x00" in raw_target else ""
        action = "read" if command in EDITOR_READ_COMMANDS else "edit"
        target = normalize_path(path, instance_id)
        return action, target, action, _basename(target) if target != "." else "."
    action, target = _classify_shell(raw_target, instance_id)
    verb, tokens = _shell_verb_tokens(raw_target)
    object_id = _object_id(action, verb, tokens, raw_target, target)
    return action, target, verb, object_id


def _tool_call_parts(tool_name: str, arguments: str) -> tuple[str, str]:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return tool_name, ""
    if tool_name == "str_replace_editor":
        command = str(args.get("command") or args.get("action") or "")
        path = str(args.get("path") or args.get("file") or "")
        return f"{command}\x00{path}", path or command
    command = str(args.get("command") or args.get("cmd") or "")
    return command, command


def _observation_for_call(call_id: str, obs_by_id: dict[str, str], rows: list[dict], index: int) -> str:
    if call_id and call_id in obs_by_id:
        return obs_by_id[call_id]
    for msg in rows[index + 1 :]:
        if msg["role"] == "tool":
            return msg["content"] or ""
        if msg["role"] == "assistant":
            break
    return ""


def _parse_row(row: tuple) -> tuple[dict, list[dict]]:
    trajectory_id, instance_id, repo, resolved, trajectory = row[:5]
    source_file = row[5] if len(row) > 5 else ""
    messages = list(trajectory or [])
    obs_by_id = {
        m["tool_call_id"]: (m["content"] or "")
        for m in messages
        if m["role"] == "tool" and m.get("tool_call_id")
    }
    calls: list[dict] = []
    step = 0
    for index, msg in enumerate(messages):
        for tool_call in msg.get("tool_calls") or []:
            fn = tool_call["function"]
            raw_target, _ = _tool_call_parts(fn["name"], fn.get("arguments", ""))
            if _is_control_target(raw_target):
                continue
            observation = _observation_for_call(tool_call.get("id", ""), obs_by_id, messages, index)
            exit_code = _extract_exit_code(observation)
            text_error = bool(TEXT_ERROR_RE.search(observation))
            timeout = bool(TIMEOUT_RE.search(observation))
            tool_error = len(observation) < 500 and bool(TOOL_ERROR_RE.search(observation))
            calls.append(
                {
                    "event_id": f"{trajectory_id}:{step}",
                    "session_id": trajectory_id,
                    "repo": repo,
                    "step_idx": step,
                    "tool_name": fn["name"],
                    "raw_target": raw_target,
                    "obs": observation,
                    "exit_code": exit_code,
                    "text_error": text_error,
                    "timeout": timeout,
                    "tool_error": tool_error,
                    "error_signature": _error_signature(
                        observation, exit_code, timeout, text_error, tool_error
                    ),
                    "obs_cause": _observation_cause(observation),
                    "error_detail": _error_detail(observation),
                    "error_site": _error_site(observation),
                    "error_message": observation[:1200],
                    "error_family": _error_detail(observation)
                    or _error_signature(observation, exit_code, timeout, text_error, tool_error),
                    "error_origin": _error_origin(observation),
                    "event_hash": _event_hash(raw_target, observation),
                }
            )
            step += 1
    session = {
        "session_id": trajectory_id,
        "repo": repo,
        "source_ref": f"{source_file}#trajectory_id={trajectory_id}" if source_file else "",
        "instance_id": instance_id,
        "resolved": bool(resolved),
        "total_turns": step,
    }
    return session, calls


def _remote_url(source: str) -> str | None:
    if source.startswith("hf://datasets/"):
        rest = source[len("hf://datasets/") :]
        parts = rest.split("/")
        if len(parts) < 3:
            return None
        dataset = "/".join(parts[:2])
        path = "/".join(parts[2:])
        return f"https://huggingface.co/datasets/{dataset}/resolve/main/{path}"
    if source.startswith(("http://", "https://")):
        return source
    return None


def _valid_parquet(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            handle.seek(-4, os.SEEK_END)
            tail = handle.read(4)
    except (OSError, ValueError):
        return False
    return head == b"PAR1" and tail == b"PAR1"


def _cached_source(
    source: str, progress: Progress | None = None, refresh: bool = False
) -> str:
    url = _remote_url(source)
    if url is None:
        return source
    cache_root = Path(
        os.getenv("TRACE2RULES_CACHE_DIR", Path.home() / ".cache" / "trace2rules")
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.parquet"
    if not refresh and cache_path.exists() and _valid_parquet(cache_path):
        return str(cache_path)
    if cache_path.exists():
        cache_path.unlink()
    part = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.part")
    task_id = None
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if progress:
                task_id = progress.add_task(
                    "Downloading parquet", total=total or None
                )
            with part.open("wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    if progress and task_id is not None:
                        progress.update(task_id, advance=len(chunk))
        if total and part.stat().st_size != total:
            raise RuntimeError(
                f"incomplete parquet download: {part.stat().st_size} of {total} bytes"
            )
        if not _valid_parquet(part):
            raise RuntimeError("downloaded file is not a parquet file")
        part.replace(cache_path)
        return str(cache_path)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    finally:
        if progress and task_id is not None:
            progress.remove_task(task_id)


def _stream_rows(
    source: str,
    prefix: str,
    limit: int,
    progress: Progress | None = None,
    refresh: bool = False,
    source_format: str = "swe-rebench",
):
    reader = duckdb.connect(
        config={
            "memory_limit": "4500MB",
            "threads": "1",
            "preserve_insertion_order": "false",
        }
    )
    try:
        local_source = (
            OPEN_SWE_FILES
            if source_format == "open-swe" and source == OPEN_SWE_SOURCE
            else source
            if source_format == "open-swe"
            else _cached_source(source, progress, refresh=refresh)
        )
        if source.startswith(("http://", "https://")) and source_format == "open-swe":
            try:
                reader.execute("SET allow_asterisks_in_http_paths = true")
            except duckdb.Error:
                pass
            try:
                reader.execute("LOAD httpfs")
            except duckdb.Error:
                reader.execute("INSTALL httpfs")
                reader.execute("LOAD httpfs")
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        if source_format == "open-swe":
            query = f"""
                SELECT trajectory_id, instance_id, repo, resolved, messages AS trajectory, filename
                FROM read_parquet(?, filename = true)
                WHERE repo = ?
                {limit_sql}
            """
        else:
            query = f"""
                SELECT trajectory_id, instance_id, repo, resolved, trajectory
                FROM read_parquet(?)
                WHERE starts_with(instance_id, ?)
                {limit_sql}
            """
        cursor = reader.execute(query, [local_source, prefix])
        while True:
            batch = cursor.fetchmany(16)
            if not batch:
                return
            yield from batch
    finally:
        reader.close()


def _events_for_calls(calls: list[dict], cache: dict[tuple[str, str], tuple[str, str, str, str]]) -> list[tuple]:
    events: list[tuple] = []
    for call in calls:
        key = (call["tool_name"], call["raw_target"])
        classification = cache.get(key)
        if classification is None:
            classification = _classify(call["tool_name"], call["raw_target"], "")
            cache[key] = classification
        action_type, target, verb, object_id = classification
        observation = call["obs"] or ""
        outcome = _event_outcome(
            action_type,
            observation,
            call["exit_code"],
            call["text_error"],
            call["timeout"],
            call["tool_error"],
            verb,
        )
        error_signature = call["error_signature"]
        if outcome == "err" and not error_signature:
            error_signature = "err:unknown"
        events.append(
            (
                call["event_id"],
                call["session_id"],
                call["repo"],
                call["step_idx"],
                call["tool_name"],
                action_type,
                verb,
                object_id,
                target,
                call["raw_target"],
                outcome,
                call["exit_code"],
                error_signature,
                outcome == "err",
                (len(observation) + 3) // 4,
                observation[:400],
                call["obs_cause"],
                call["error_detail"],
                call["error_site"],
                call["error_message"],
                call["error_family"],
                call["error_origin"],
                call["event_hash"],
            )
        )
    return events


def ingest(
    repos: list[str] | None = None,
    limit: int = 200,
    db_path: str | Path = DB_PATH,
    source: str = SOURCE,
    progress: Progress | None = None,
    refresh_cache: bool = False,
    source_format: str = "swe-rebench",
) -> list[IngestStats]:
    repos = repos or DEFAULT_REPOS
    con = connect(db_path)
    con.execute("CREATE OR REPLACE TEMP TABLE new_sessions (session_id VARCHAR, repo VARCHAR, instance_id VARCHAR, resolved BOOLEAN, total_turns INTEGER, source_ref VARCHAR)")
    stats: list[IngestStats] = []
    classify_cache: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    con.execute("BEGIN TRANSACTION")
    try:
        for prefix in repos:
            matched = 0
            events_for_prefix: list[tuple] = []
            new_sessions_batch: list[dict] = []
            task_id = (
                progress.add_task(f"{prefix}", total=limit or None)
                if progress
                else None
            )
            for row in _stream_rows(
                source,
                prefix,
                limit,
                progress=progress,
                refresh=refresh_cache,
                source_format=source_format,
            ):
                matched += 1
                if progress and task_id is not None and matched % 25 == 0:
                    progress.update(
                        task_id,
                        completed=matched,
                        description=f"{prefix}  {matched:,} rows",
                    )
                session, calls = _parse_row(row)
                if not session.get("source_ref"):
                    session["source_ref"] = f"{source}#trajectory_id={session['session_id']}"
                new_sessions_batch.append(session)
                events_for_prefix.extend(_events_for_calls(calls, classify_cache))
                if len(new_sessions_batch) >= 64:
                    _flush_sessions(con, new_sessions_batch)
                    new_sessions_batch.clear()
            _flush_sessions(con, new_sessions_batch)
            con.execute("DELETE FROM tool_events WHERE session_id IN (SELECT session_id FROM new_sessions)")
            con.execute("DELETE FROM sessions WHERE session_id IN (SELECT session_id FROM new_sessions)")
            con.execute(
                "INSERT INTO sessions (session_id, repo, instance_id, resolved, total_turns, source_ref) "
                "SELECT session_id, repo, instance_id, resolved, total_turns, source_ref FROM new_sessions"
            )
            if events_for_prefix:
                _insert_rows(
                    con,
                    "tool_events",
                    events_for_prefix,
                    columns=[
                        "event_id", "session_id", "repo", "step_idx", "tool_name",
                        "action_type", "verb", "object_id", "target", "raw_target",
                        "outcome", "exit_code", "error_signature", "is_error",
                        "approx_out_tokens", "obs_excerpt", "obs_cause",
                        "error_detail", "error_site", "error_message",
                        "error_family", "error_origin", "event_hash",
                    ],
                )
            session_count = con.execute("SELECT count(*) FROM new_sessions").fetchone()[0]
            event_count = len(events_for_prefix)
            if progress and task_id is not None:
                progress.update(
                    task_id,
                    completed=matched,
                    total=matched or None,
                    description=f"{prefix}  {matched:,} rows",
                )
            stats.append(IngestStats(prefix, matched, session_count, event_count))
            con.execute("DELETE FROM new_sessions")
        con.execute("COMMIT")
        return stats
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def write_sessions(
    db_path: str | Path,
    sessions: Iterable[Session],
) -> IngestStats:
    """Write canonical sessions/events, replacing rows with the same ids."""
    session_list = list({session.session_id: session for session in sessions}.values())
    con = connect(db_path)
    con.execute("BEGIN TRANSACTION")
    try:
        if session_list:
            session_ids = [session.session_id for session in session_list]
            placeholders = ",".join("?" for _ in session_ids)
            con.execute(
                f"DELETE FROM tool_events WHERE session_id IN ({placeholders})",
                session_ids,
            )
            con.execute(
                f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            )
            con.executemany(
                """
                INSERT INTO sessions (
                    session_id, repo, instance_id, resolved, total_turns,
                    source, source_ref, agent, success_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session.session_id,
                        session.repo or "unknown",
                        session.instance_id or session.session_id,
                        session.resolved,
                        len(session.calls),
                        session.source,
                        session.source_ref,
                        session.agent,
                        session.success_score,
                    )
                    for session in session_list
                ],
            )

        events = []
        for session in session_list:
            for step_idx, call in enumerate(session.calls):
                if call.action_type:
                    action = call.action_type
                    target = call.target or call.raw_target
                    verb = call.verb or call.tool_name
                    object_id = call.object_id or target
                else:
                    action, target, verb, object_id = _classify(
                        call.tool_name, call.raw_target, ""
                    )
                observation = call.observation or ""
                exit_code = (
                    call.exit_code
                    if call.exit_code is not None
                    else _extract_exit_code(observation)
                )
                text_error = bool(TEXT_ERROR_RE.search(observation))
                timeout = action == "exec" and bool(TIMEOUT_RE.search(observation))
                tool_error = (
                    len(observation) < 500 and bool(TOOL_ERROR_RE.search(observation))
                )
                error_signature = _error_signature(
                    observation, exit_code, timeout, text_error, tool_error
                )
                outcome = _event_outcome(
                    action, observation, exit_code, text_error, timeout, tool_error, verb
                )
                if call.is_error is True:
                    outcome = "err"
                elif call.is_error is False and outcome == "unknown":
                    outcome = "ok"
                if outcome == "err" and not error_signature:
                    error_signature = "err:unknown"
                events.append(
                    (
                        f"{session.session_id}:{step_idx}",
                        session.session_id,
                        session.repo or "unknown",
                        step_idx,
                        call.segment_idx,
                        call.tool_name,
                        action,
                        verb,
                        object_id,
                        target,
                        call.raw_target,
                        outcome,
                        exit_code,
                        error_signature,
                        outcome == "err",
                        (len(observation) + 3) // 4,
                        observation[:400],
                        _observation_cause(observation),
                        _error_detail(observation),
                        _error_site(observation),
                        observation[:1200],
                        _error_detail(observation) or error_signature,
                        _error_origin(observation),
                        _event_hash(call.raw_target, observation),
                    )
                )

        if events:
            con.executemany(
                """
                INSERT INTO tool_events (
                    event_id, session_id, repo, step_idx, segment_idx,
                    tool_name, action_type,
                    verb, object_id, target, raw_target, outcome, exit_code,
                    error_signature, is_error, approx_out_tokens, obs_excerpt,
                    obs_cause, error_detail, error_site, error_message,
                    error_family, error_origin, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                events,
            )
        con.execute("COMMIT")
        return IngestStats(
            prefix="canonical",
            matched=len(session_list),
            sessions=len(session_list),
            events=len(events),
        )
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

def _insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: list[tuple],
    columns: list[str] | None = None,
) -> None:
    if not rows:
        return
    appender = con.appender(table, columns=columns)
    for row in rows:
        appender.append_row(*row)
    appender.flush()


def _flush_sessions(con: duckdb.DuckDBPyConnection, sessions: list[dict]) -> None:
    if not sessions:
        return
    _insert_rows(
        con,
        "new_sessions",
        [
            (s["session_id"], s["repo"], s["instance_id"], s["resolved"], s["total_turns"], s.get("source_ref", ""))
            for s in sessions
        ],
    )
