from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..normalize import IngestStats
from ..writer import SessionWriter
from ...models import Session, ToolCall
from .common import classify_tool_call, parse_arguments, target_from_args


def find_database(source: str | Path) -> Path | None:
    root = Path(source).expanduser()
    if root.is_file() and root.suffix == ".db":
        return root
    if root.is_dir():
        for name in ("opencode.db",):
            candidate = root / name
            if candidate.exists():
                return candidate
        candidates = sorted(root.glob("opencode*.db"))
        if candidates:
            return candidates[0]
    default = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return default if default.exists() else None


def _timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _tool_call(data: dict) -> ToolCall | None:
    name = data.get("tool") or data.get("name")
    if not isinstance(name, str) or not name:
        return None
    state = data.get("state") or {}
    args = parse_arguments(state.get("input") or data.get("input") or {})
    raw_target = target_from_args(name, args, name)
    action, target, verb, object_id = classify_tool_call(name, args, raw_target)
    output = state.get("output") or state.get("error") or ""
    return ToolCall(
        tool_name=name,
        raw_target=raw_target,
        observation=str(output),
        action_type=action,
        target=target,
        verb=verb,
        object_id=object_id,
        is_error=state.get("status") == "error",
    )


def _v1_calls(con: sqlite3.Connection, session_id: str) -> list[ToolCall]:
    rows = con.execute(
        """
        SELECT m.data AS message_data, p.ordinal, p.data AS part_data
        FROM message m
        LEFT JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ?
        ORDER BY m.time_created, p.ordinal
        """,
        (session_id,),
    ).fetchall()
    calls: list[ToolCall] = []
    segment = 0
    for _message, _ordinal, part_data in rows:
        if not part_data:
            continue
        try:
            data = json.loads(part_data)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "compaction":
            segment += 1
            continue
        if data.get("type") != "tool":
            continue
        call = _tool_call(data)
        if call is not None:
            call.segment_idx = segment
            calls.append(call)
    return calls


def _v2_calls(con: sqlite3.Connection, session_id: str) -> list[ToolCall]:
    rows = con.execute(
        "SELECT data FROM session_message WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    calls: list[ToolCall] = []
    segment = 0
    for (message_data,) in rows:
        try:
            data = json.loads(message_data)
        except json.JSONDecodeError:
            continue
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "compaction":
                segment += 1
                continue
            if part.get("type") != "tool":
                continue
            call = _tool_call(part)
            if call is not None:
                call.segment_idx = segment
                calls.append(call)
    return calls


def ingest_opencode(
    source: str | Path,
    db_path: str | Path,
    *,
    limit: int | None = None,
    repo: str | None = None,
    split_compactions: bool = True,
) -> list[IngestStats]:
    path = find_database(source)
    if path is None or not path.exists():
        raise FileNotFoundError(f"OpenCode database not found under {source}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = _tables(con)
        v2 = "session_v2" in tables
        session_table = "session_v2" if v2 else "session"
        cursor = con.execute(f"SELECT * FROM {session_table} ORDER BY id")
        batch: list[Session] = []
        total_sessions = 0
        total_events = 0
        count = 0
        title = ""
        writer = SessionWriter(db_path, split_compactions=split_compactions)
        try:
            while True:
                rows = cursor.fetchmany(64)
                if not rows:
                    break
                for row in rows:
                    if limit and count >= limit:
                        break
                    session_id = str(row["id"])
                    parent_id = (
                        str(row["parent_id"] or "")
                        if "parent_id" in row.keys()
                        else ""
                    )
                    directory = (
                        str(row["directory"] or "")
                        if "directory" in row.keys()
                        else ""
                    )
                    title = (
                        str(row["title"] or "")
                        if "title" in row.keys()
                        else ""
                    )
                    calls = (
                        _v2_calls(con, session_id)
                        if v2
                        else _v1_calls(con, session_id)
                    )
                    batch.append(
                        Session(
                            session_id=session_id,
                            repo=repo
                            or (Path(directory).name if directory else "unknown"),
                            instance_id=parent_id or session_id,
                            source="opencode",
                            source_ref=f"{path}#{session_id}",
                            agent="opencode",
                            calls=calls,
                            started_at=(
                                _timestamp(row["time_created"])
                                if "time_created" in row.keys()
                                else None
                            ),
                        )
                    )
                    count += 1
                    if len(batch) >= 64:
                        written = writer.write(batch)
                        total_sessions += written.sessions
                        total_events += written.events
                        batch = []
                if limit and count >= limit:
                    break
            if batch:
                written = writer.write(batch)
                total_sessions += written.sessions
                total_events += written.events
        finally:
            writer.close()
        return [
            IngestStats(
                f"opencode:{title}",
                count,
                total_sessions,
                total_events,
            )
        ]
    finally:
        con.close()
