from __future__ import annotations

from pathlib import Path

import duckdb

from ..models import Session, ToolCall
from .common import classify_tool_call, parse_arguments, target_from_args


def _paths(source: str | Path) -> tuple[Path, Path]:
    root = Path(source).expanduser()
    if root.is_file():
        sessions = root
    else:
        sessions = root / "sessions.parquet"
    conversations = sessions.parent / "conversations.parquet"
    return sessions, conversations


def ingest_swe_chat(
    source: str | Path,
    db_path: str | Path,
    *,
    limit: int | None = None,
    repo: str | None = None,
):
    """Import the normalized SALT-NLP/SWE-chat parquet tables."""
    from ..ingest import IngestStats, write_sessions

    sessions_path, conversations_path = _paths(source)
    if not sessions_path.exists() or not conversations_path.exists():
        raise FileNotFoundError(
            f"Expected sessions.parquet and conversations.parquet under {sessions_path.parent}"
        )

    con = duckdb.connect()
    try:
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        con.execute(
            f"""
            CREATE TEMP TABLE selected AS
            SELECT session_id, repo_id, agent, session_success
            FROM read_parquet(?)
            ORDER BY repo_id, session_id
            {limit_sql}
            """,
            [str(sessions_path)],
        )
        meta = {
            row[0]: row
            for row in con.execute(
                "SELECT session_id, repo_id, agent, session_success FROM selected"
            ).fetchall()
        }
        sessions: dict[str, Session] = {}
        calls: dict[str, list[ToolCall]] = {}
        pending: dict[tuple[str, str], ToolCall] = {}
        cursor = con.execute(
            """
            SELECT c.session_id, c.turn_type, c.tool_name, c.tool_call_id,
                   c.tool_input_json, c.command, c.file_path, c.content
            FROM read_parquet(?) c
            JOIN selected s USING (session_id)
            WHERE c.turn_type IN ('tool_use', 'tool_result')
            ORDER BY c.session_id, c.turn_number
            """,
            [str(conversations_path)],
        )
        while True:
            batch = cursor.fetchmany(10_000)
            if not batch:
                break
            for session_id, turn_type, tool_name, tool_call_id, input_json, command, path, content in batch:
                if session_id not in sessions:
                    _, repo_id, agent, success = meta.get(session_id, (session_id, repo or "unknown", "", None))
                    sessions[session_id] = Session(
                        session_id=session_id,
                        repo=repo or repo_id or "unknown",
                        instance_id=session_id,
                        agent=agent or "",
                        source="swe-chat",
                        source_ref=f"{sessions_path}#{session_id}",
                        success_score=float(success) if success not in (None, "") else None,
                    )
                    calls[session_id] = []
                if turn_type == "tool_use":
                    args = parse_arguments(input_json)
                    raw_target = (
                        (command or "").strip()
                        or (path or "").strip()
                        or target_from_args(tool_name or "", args, input_json or tool_name or "")
                    )
                    action, target, verb, object_id = classify_tool_call(
                        tool_name or "", args, raw_target
                    )
                    call = ToolCall(
                        tool_name=tool_name or "",
                        raw_target=raw_target,
                        action_type=action,
                        target=target,
                        verb=verb,
                        object_id=object_id,
                    )
                    calls[session_id].append(call)
                    if tool_call_id:
                        pending[(session_id, tool_call_id)] = call
                elif tool_call_id:
                    call = pending.pop((session_id, tool_call_id), None)
                    if call is not None:
                        call.observation = content or ""
        for session in sessions.values():
            session.calls = calls.get(session.session_id, [])
        write_sessions(db_path, sessions.values())
        events = sum(len(session.calls) for session in sessions.values())
        return IngestStats("swe-chat", len(sessions), len(sessions), events)
    finally:
        con.close()
