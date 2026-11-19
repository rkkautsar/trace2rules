from __future__ import annotations

from pathlib import Path

from rich.progress import Progress

from .db import DB_PATH, connect
from .ingest_normalize import (
    DEFAULT_REPOS,
    OPEN_SWE_FILES,
    OPEN_SWE_SOURCE,
    SOURCE,
    IngestStats,
    _cached_source,
    _classify_shell,
    _error_detail,
    _error_origin,
    _error_site,
    _event_hash,
    _event_outcome,
    _events_for_calls,
    _extract_exit_code,
    _is_control_target,
    _is_path_token,
    _object_id,
    _parameterize,
    _observation_cause,
    _parse_row,
    _remote_url,
    _shell_verb_tokens,
    _stream_rows,
    normalize_path,
)
from .ingest_writer import _flush_sessions, _insert_rows, write_sessions
from .models import Session, ToolCall

__all__ = [
    "DEFAULT_REPOS",
    "IngestStats",
    "OPEN_SWE_FILES",
    "OPEN_SWE_SOURCE",
    "SOURCE",
    "ingest",
    "write_sessions",
]


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
    con.execute(
        "CREATE OR REPLACE TEMP TABLE new_sessions "
        "(session_id VARCHAR, repo VARCHAR, instance_id VARCHAR, resolved BOOLEAN, "
        "total_turns INTEGER, source_ref VARCHAR, started_at TIMESTAMP)"
    )
    stats: list[IngestStats] = []
    classify_cache: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    con.execute("BEGIN TRANSACTION")
    try:
        for prefix in repos:
            matched = 0
            events_for_prefix: list[tuple] = []
            new_sessions_batch: list[dict] = []
            task_id = progress.add_task(f"{prefix}", total=limit or None) if progress else None
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
                "INSERT INTO sessions (session_id, repo, instance_id, resolved, total_turns, source_ref, started_at) "
                "SELECT session_id, repo, instance_id, resolved, total_turns, source_ref, started_at FROM new_sessions"
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
                        "assistant_context",
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
