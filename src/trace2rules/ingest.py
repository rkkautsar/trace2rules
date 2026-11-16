from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .db import DB_PATH, connect
from .models import Session, ToolCall

SOURCE = "hf://datasets/nebius/SWE-rebench-openhands-trajectories/trajectories.parquet"
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

ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
CD_PREFIX_RE = re.compile(r"^cd\s+[^&;]+\s*(?:&&|;)\s*")
EXPORT_PREFIX_RE = re.compile(r"^export\s+[^&;]+\s*(?:&&|;)\s*")
EDIT_VERBS = {"apply_patch", "patch"}
READ_VERBS = {"cat", "head", "tail", "less", "more", "wc", "nl", "ls", "tree", "stat", "file", "du", "sed", "awk"}
SEARCH_VERBS = {"grep", "rg", "ag", "ack", "fd", "find"}
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
    return bool(re.search(r"\.(py|pyi|txt|md|rst|toml|cfg|ini|yaml|yml|json)$", token))


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


def _strip_shell_prefix(command: str) -> str:
    command = command.strip()
    while True:
        before = command
        command = ENV_PREFIX_RE.sub("", command)
        command = CD_PREFIX_RE.sub("", command)
        command = EXPORT_PREFIX_RE.sub("", command)
        if command == before:
            return command


def _first_verb(command: str) -> tuple[str, list[str]]:
    command = _strip_shell_prefix(command)
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
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
    if action == "exec" and target and " " not in target:
        return _basename(target)
    return ""


def _extract_exit_code(observation: str) -> int | None:
    match = EXIT_RE.search(observation) or EXIT_FALLBACK_RE.search(observation)
    return int(match.group(1)) if match else None


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
) -> str:
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
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()

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
    trajectory_id, instance_id, repo, resolved, trajectory = row
    messages = list(trajectory or [])
    obs_by_id = {
        m["tool_call_id"]: (m["content"] or "") for m in messages if m["role"] == "tool"
    }
    calls: list[dict] = []
    step = 0
    for index, msg in enumerate(messages):
        for tool_call in msg.get("tool_calls") or []:
            fn = tool_call["function"]
            raw_target, _ = _tool_call_parts(fn["name"], fn.get("arguments", ""))
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
                }
            )
            step += 1
    session = {
        "session_id": trajectory_id,
        "repo": repo,
        "instance_id": instance_id,
        "resolved": bool(resolved),
        "total_turns": step,
    }
    return session, calls


def _stream_rows(source: str, prefix: str, limit: int):
    reader = duckdb.connect(
        config={
            "memory_limit": "4500MB",
            "threads": "1",
            "preserve_insertion_order": "false",
        }
    )
    try:
        if source.startswith(("hf://", "http://", "https://")):
            try:
                reader.execute("LOAD httpfs")
            except duckdb.Error:
                reader.execute("INSTALL httpfs")
                reader.execute("LOAD httpfs")
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        query = f"""
            SELECT trajectory_id, instance_id, repo, resolved, trajectory
            FROM read_parquet(?)
            WHERE starts_with(instance_id, ?)
            {limit_sql}
        """
        cursor = reader.execute(query, [source, prefix])
        while True:
            batch = cursor.fetchmany(16)
            if not batch:
                return
            yield from batch
    finally:
        reader.close()


def ingest(
    repos: list[str] | None = None,
    limit: int = 200,
    db_path: str | Path = DB_PATH,
    source: str = SOURCE,
) -> list[IngestStats]:
    repos = repos or DEFAULT_REPOS
    con = connect(db_path)
    con.execute("CREATE OR REPLACE TEMP TABLE new_sessions (session_id VARCHAR, repo VARCHAR, instance_id VARCHAR, resolved BOOLEAN, total_turns INTEGER)")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE raw_calls (event_id VARCHAR, session_id VARCHAR, repo VARCHAR, step_idx INTEGER, tool_name VARCHAR, raw_target VARCHAR, obs_content VARCHAR, exit_code INTEGER, text_error BOOLEAN, timeout BOOLEAN, tool_error BOOLEAN, error_signature VARCHAR)"
    )
    stats: list[IngestStats] = []
    try:
        for prefix in repos:
            matched = 0
            rows_for_prefix: list[dict] = []
            new_sessions_batch: list[dict] = []
            for row in _stream_rows(source, prefix, limit):
                matched += 1
                session, calls = _parse_row(row)
                new_sessions_batch.append(session)
                rows_for_prefix.extend(calls)
                if len(rows_for_prefix) >= 2_000:
                    _flush_calls(con, rows_for_prefix)
                    rows_for_prefix.clear()
                if len(new_sessions_batch) >= 64:
                    _flush_sessions(con, new_sessions_batch)
                    new_sessions_batch.clear()
            _flush_calls(con, rows_for_prefix)
            _flush_sessions(con, new_sessions_batch)
            con.execute("DELETE FROM tool_events WHERE session_id IN (SELECT session_id FROM new_sessions)")
            con.execute("DELETE FROM sessions WHERE session_id IN (SELECT session_id FROM new_sessions)")
            con.execute(
                "INSERT INTO sessions (session_id, repo, instance_id, resolved, total_turns) "
                "SELECT session_id, repo, instance_id, resolved, total_turns FROM new_sessions"
            )
            _build_target_map(con)
            rows = con.execute(
                """
                SELECT r.event_id, r.session_id, r.repo, r.step_idx, r.tool_name,
                       m.action_type, m.verb, m.object_id, m.target, r.raw_target,
                       r.exit_code, r.text_error, r.timeout, r.tool_error,
                       r.error_signature, r.obs_content
                FROM raw_calls r JOIN target_map m USING (tool_name, raw_target)
                """
            ).fetchall()
            events = []
            for row in rows:
                (
                    event_id,
                    session_id,
                    event_repo,
                    step_idx,
                    tool_name,
                    action_type,
                    verb,
                    object_id,
                    target,
                    raw_target,
                    exit_code,
                    text_error,
                    timeout,
                    tool_error,
                    error_signature,
                    observation,
                ) = row
                observation = observation or ""
                outcome = _event_outcome(
                    action_type, observation, exit_code, text_error, timeout, tool_error
                )
                if outcome == "err" and not error_signature:
                    error_signature = "err:unknown"
                events.append(
                    (
                        event_id,
                        session_id,
                        event_repo,
                        step_idx,
                        tool_name,
                        action_type,
                        verb,
                        object_id,
                        target,
                        raw_target,
                        outcome,
                        exit_code,
                        error_signature,
                        outcome == "err",
                        (len(observation) + 3) // 4,
                        observation[:400],
                    )
                )
            con.executemany(
                """
                INSERT INTO tool_events (
                    event_id, session_id, repo, step_idx, tool_name, action_type,
                    verb, object_id, target, raw_target, outcome, exit_code,
                    error_signature, is_error, approx_out_tokens, obs_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                events,
            )
            session_count = con.execute("SELECT count(*) FROM new_sessions").fetchone()[0]
            event_count = con.execute("SELECT count(*) FROM raw_calls").fetchone()[0]
            stats.append(IngestStats(prefix, matched, session_count, event_count))
            con.execute("DELETE FROM raw_calls")
            con.execute("DELETE FROM new_sessions")
        return stats
    finally:
        con.close()


def write_sessions(
    db_path: str | Path,
    sessions: Iterable[Session],
) -> IngestStats:
    """Write canonical sessions/events, replacing rows with the same ids."""
    session_list = list(sessions)
    con = connect(db_path)
    event_count = 0
    try:
        for session in session_list:
            con.execute("DELETE FROM tool_events WHERE session_id = ?", [session.session_id])
            con.execute("DELETE FROM sessions WHERE session_id = ?", [session.session_id])
            total_turns = len(session.calls)
            con.execute(
                """
                INSERT INTO sessions (
                    session_id, repo, instance_id, resolved, total_turns,
                    source, agent, success_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    session.session_id,
                    session.repo or "unknown",
                    session.instance_id or session.session_id,
                    session.resolved,
                    total_turns,
                    session.source,
                    session.agent,
                    session.success_score,
                ],
            )
            events = []
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
                    action, observation, exit_code, text_error, timeout, tool_error
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
                    )
                )
            if events:
                con.executemany(
                    """
                    INSERT INTO tool_events (
                        event_id, session_id, repo, step_idx, segment_idx,
                        tool_name, action_type,
                        verb, object_id, target, raw_target, outcome, exit_code,
                        error_signature, is_error, approx_out_tokens, obs_excerpt
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    events,
                )
                event_count += len(events)
        return IngestStats(
            prefix="canonical",
            matched=len(session_list),
            sessions=len(session_list),
            events=event_count,
        )
    finally:
        con.close()


def _flush_sessions(con: duckdb.DuckDBPyConnection, sessions: list[dict]) -> None:
    if not sessions:
        return
    con.executemany(
        "INSERT INTO new_sessions VALUES (?, ?, ?, ?, ?)",
        [
            (s["session_id"], s["repo"], s["instance_id"], s["resolved"], s["total_turns"])
            for s in sessions
        ],
    )


def _flush_calls(con: duckdb.DuckDBPyConnection, calls: list[dict]) -> None:
    if not calls:
        return
    con.executemany(
        "INSERT INTO raw_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                c["event_id"],
                c["session_id"],
                c["repo"],
                c["step_idx"],
                c["tool_name"],
                c["raw_target"],
                c["obs"],
                c["exit_code"],
                c["text_error"],
                c["timeout"],
                c["tool_error"],
                c["error_signature"],
            )
            for c in calls
        ],
    )


def _build_target_map(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE target_map (tool_name VARCHAR, raw_target VARCHAR, target VARCHAR, action_type VARCHAR, verb VARCHAR, object_id VARCHAR)"
    )
    pairs = con.execute("SELECT DISTINCT tool_name, raw_target FROM raw_calls").fetchall()
    if not pairs:
        return
    rows = []
    for tool_name, raw_target in pairs:
        action, target, verb, object_id = _classify(tool_name, raw_target, "")
        rows.append((tool_name, raw_target, target, action, verb, object_id))
    con.executemany("INSERT INTO target_map VALUES (?, ?, ?, ?, ?, ?)", rows)
