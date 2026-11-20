from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from ...models import Session, ToolCall
from .common import classify_tool_call, parse_arguments, target_from_args


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
    from ..normalize import IngestStats
    from ..writer import SessionWriter

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
        batch: list[Session] = []
        current_id: str | None = None
        current: Session | None = None
        current_calls: list[ToolCall] = []
        pending: dict[tuple[str, str], ToolCall] = {}
        committed_sessions = 0
        committed_events = 0

        writer = SessionWriter(db_path)
        try:
            def flush_session() -> None:
                nonlocal current, current_calls, batch
                if current is None:
                    return
                current.calls = current_calls
                batch.append(current)
                current = None
                current_calls = []

            def flush_batch() -> None:
                nonlocal batch, committed_sessions, committed_events
                if not batch:
                    return
                written = writer.write(batch)
                committed_sessions += written.sessions
                committed_events += written.events
                batch = []
            columns = {
                row[0]
                for row in con.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(conversations_path)]
                ).fetchall()
            }
            timestamp_expr = "c.timestamp" if "timestamp" in columns else "NULL"
            cursor = con.execute(
                f"""
                SELECT c.session_id, c.turn_type, c.tool_name, c.tool_call_id,
                       c.tool_input_json, c.command, c.file_path, c.content, {timestamp_expr}
                FROM read_parquet(?) c
                JOIN selected s USING (session_id)
                WHERE c.turn_type IN ('tool_use', 'tool_result', 'assistant_response')
                ORDER BY c.session_id, c.turn_number
                """,
                [str(conversations_path)],
            )
            while True:
                rows = cursor.fetchmany(10_000)
                if not rows:
                    break
                for session_id, turn_type, tool_name, tool_call_id, input_json, command, path, content, stamped in rows:
                    if session_id != current_id:
                        if current is not None and current.session_id != session_id:
                            flush_session()
                            pending = {}
                        if current is None:
                            _, repo_id, agent, success = meta.get(
                                session_id, (session_id, repo or "unknown", "", None)
                            )
                            current = Session(
                                session_id=session_id,
                                repo=repo or repo_id or "unknown",
                                instance_id=session_id,
                                agent=agent or "",
                                source="swe-chat",
                                source_ref=f"{conversations_path}#{session_id}",
                                success_score=float(success) if success not in (None, "") else None,
                                started_at=_timestamp(stamped),
                            )
                            current_id = session_id
                        current_id = session_id
                    if current is None:
                        continue
                    stamped_at = _timestamp(stamped)
                    if stamped_at and (
                        current.started_at is None or stamped_at < current.started_at
                    ):
                        current.started_at = stamped_at
                    if turn_type == "assistant_response":
                        if current_calls:
                            current_calls[-1].assistant_context = str(content or "")[:800]
                        continue
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
                        current_calls.append(call)
                        if tool_call_id:
                            pending[(session_id, tool_call_id)] = call
                    elif tool_call_id:
                        call = pending.pop((session_id, tool_call_id), None)
                        if call is not None:
                            call.observation = content or ""
                    if len(batch) >= 64:
                        flush_batch()
            flush_session()
            flush_batch()
        finally:
            writer.close()
        return IngestStats(
            "swe-chat", committed_sessions, committed_sessions, committed_events
        )
    finally:
        con.close()
