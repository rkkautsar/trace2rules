from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.progress import Progress

from ..db import DB_PATH, connect
from .normalize import (
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
from .writer import _flush_sessions, _insert_rows, write_sessions
from ..models import Session, ToolCall

__all__ = [
    "DEFAULT_REPOS",
    "IngestStats",
    "OPEN_SWE_FILES",
    "OPEN_SWE_SOURCE",
    "SOURCE",
    "ingest",
    "normalize_path",
    "write_sessions",
]


def _parse_prefix(
    source: str,
    prefix: str,
    limit: int,
    progress: Progress | None,
    source_format: str,
    classify_cache: dict[tuple[str, str], tuple[str, str, str, str]],
    source_label: str,
) -> tuple[int, list[dict], list[tuple]]:
    matched = 0
    sessions: list[dict] = []
    events: list[tuple] = []
    for row in _stream_rows(
        source,
        prefix,
        limit,
        progress=progress,
        refresh=False,
        source_format=source_format,
    ):
        matched += 1
        session, calls = _parse_row(row)
        if not session.get("source_ref"):
            session["source_ref"] = f"{source_label}#trajectory_id={session['session_id']}"
        sessions.append(session)
        events.extend(_events_for_calls(calls, classify_cache))
    return matched, sessions, events


def _store_prefix(
    con,
    prefix: str,
    matched: int,
    sessions: list[dict],
    events: list[tuple],
) -> IngestStats:
    for start in range(0, len(sessions), 64):
        _flush_sessions(con, sessions[start:start + 64])
    con.execute("DELETE FROM tool_events WHERE session_id IN (SELECT session_id FROM new_sessions)")
    con.execute("DELETE FROM sessions WHERE session_id IN (SELECT session_id FROM new_sessions)")
    con.execute(
        "INSERT INTO sessions (session_id, repo, instance_id, resolved, total_turns, source_ref, started_at) "
        "SELECT session_id, repo, instance_id, resolved, total_turns, source_ref, started_at FROM new_sessions"
    )
    if events:
        _insert_rows(
            con,
            "tool_events",
            events,
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
    con.execute("DELETE FROM new_sessions")
    return IngestStats(prefix, matched, session_count, len(events))


def ingest(
    repos: list[str] | None = None,
    limit: int = 200,
    db_path: str | Path = DB_PATH,
    source: str = SOURCE,
    progress: Progress | None = None,
    refresh_cache: bool = False,
    source_format: str = "swe-rebench",
    workers: int | None = None,
) -> list[IngestStats]:
    repos = repos or DEFAULT_REPOS
    workers = workers or min(2, len(repos), os.cpu_count() or 1)
    resolved_source = (
        source if source_format == "open-swe" else _cached_source(source, progress, refresh=refresh_cache)
    )
    con = connect(db_path)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE new_sessions "
        "(session_id VARCHAR, repo VARCHAR, instance_id VARCHAR, resolved BOOLEAN, "
        "total_turns INTEGER, source_ref VARCHAR, started_at TIMESTAMP)"
    )
    stats: list[IngestStats] = []
    classify_cache: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    task_ids: list[int | None] = []
    con.execute("BEGIN TRANSACTION")
    try:
        for prefix in repos:
            task_ids.append(
                progress.add_task(f"{prefix}", total=limit or None) if progress else None
            )
        if workers > 1 and len(repos) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _parse_prefix,
                        resolved_source,
                        prefix,
                        limit,
                        progress,
                        source_format,
                        classify_cache,
                        source,
                    )
                    for prefix in repos
                ]
                parsed = [future.result() for future in futures]
        else:
            parsed = [
                _parse_prefix(
                    resolved_source,
                    prefix,
                    limit,
                    progress,
                    source_format,
                    classify_cache,
                    source,
                )
                for prefix in repos
            ]
        for prefix, task_id, (matched, sessions, events) in zip(repos, task_ids, parsed):
            stats.append(_store_prefix(con, prefix, matched, sessions, events))
            if progress and task_id is not None:
                progress.update(
                    task_id,
                    completed=matched,
                    total=matched or None,
                    description=f"{prefix}  {matched:,} rows",
                )
        con.execute("COMMIT")
        return stats
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
