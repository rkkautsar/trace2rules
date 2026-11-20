from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import duckdb

from ..db import connect
from ..redact import redact_text
from .normalize import (
    TEXT_ERROR_RE,
    TIMEOUT_RE,
    TOOL_ERROR_RE,
    IngestStats,
    _classify,
    _error_detail,
    _error_origin,
    _error_site,
    _error_signature,
    _event_hash,
    _event_outcome,
    _extract_exit_code,
    _observation_cause,
)
from ..models import Session
from .test_results import parse_test_result

def _split_compactions(sessions: list[Session]) -> list[Session]:
    """Treat each compaction segment as its own canonical session."""
    split: list[Session] = []
    for session in sessions:
        segments = sorted({call.segment_idx for call in session.calls})
        if len(segments) <= 1:
            split.append(session)
            continue
        step_segments = [call.segment_idx for call in session.calls]
        feedback_by_segment: dict[int, list] = {segment: [] for segment in segments}
        for item in session.feedback:
            index = max(0, min(item.step_idx, len(step_segments)))
            segment = (
                step_segments[index]
                if index < len(step_segments)
                else step_segments[-1]
            )
            local_idx = sum(1 for value in step_segments[:index] if value == segment)
            feedback_by_segment[segment].append(
                replace(item, step_idx=local_idx)
            )
        for segment in segments:
            split.append(
                replace(
                    session,
                    session_id=f"{session.session_id}#seg{segment}",
                    calls=[
                        replace(call, segment_idx=0)
                        for call in session.calls
                        if call.segment_idx == segment
                    ],
                    feedback=feedback_by_segment[segment],
                )
            )
    return split


def write_sessions(
    db_path: str | Path,
    sessions: Iterable[Session],
    *,
    split_compactions: bool = False,
) -> IngestStats:
    """Write canonical sessions/events, replacing rows with the same ids."""
    raw_sessions = list({session.session_id: session for session in sessions}.values())
    session_list = _split_compactions(raw_sessions) if split_compactions else raw_sessions
    con = connect(db_path)
    con.execute("BEGIN TRANSACTION")
    try:
        if session_list:
            session_ids = {session.session_id for session in session_list}
            for session in raw_sessions:
                session_ids.add(session.session_id)
                session_ids.update(
                    row[0]
                    for row in con.execute(
                        "SELECT session_id FROM sessions WHERE starts_with(session_id, ?)",
                        [f"{session.session_id}#seg"],
                    ).fetchall()
                )
            session_ids = list(session_ids)
            placeholders = ",".join("?" for _ in session_ids)
            con.execute(
                f"DELETE FROM tool_events WHERE session_id IN ({placeholders})",
                session_ids,
            )
            con.execute(
                f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            )
            con.execute(
                f"DELETE FROM feedback_events WHERE session_id IN ({placeholders})",
                session_ids,
            )
            con.execute(
                f"DELETE FROM test_results WHERE session_id IN ({placeholders})",
                session_ids,
            )
            _insert_rows(
                con,
                "sessions",
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
                        session.started_at,
                        session.cwd,
                    )
                    for session in session_list
                ],
                columns=[
                    "session_id", "repo", "instance_id", "resolved", "total_turns",
                    "source", "source_ref", "agent", "success_score", "started_at", "cwd",
                ],
            )

        events = []
        test_rows: list[tuple] = []
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
                observation = redact_text(call.observation or "")
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
                outcome = _event_outcome(
                    action, observation, exit_code, text_error, timeout, tool_error, verb
                )
                if call.is_error is True:
                    outcome = "err"
                elif call.is_error is False:
                    outcome = "ok"
                if outcome == "err":
                    error_signature = _error_signature(
                        observation, exit_code, timeout, text_error, tool_error
                    ) or "err:unknown"
                    detail = _error_detail(observation)
                    family = detail or error_signature
                    error_message = observation[:1200]
                    cause = _observation_cause(observation)
                    site = _error_site(observation)
                    origin = _error_origin(observation)
                else:
                    error_signature = ""
                    detail = family = error_message = cause = site = origin = ""
                parsed = parse_test_result(
                    call.raw_target or target or "",
                    observation,
                )
                if parsed is not None:
                    test_rows.append(
                        (
                            session.session_id,
                            session.repo or "unknown",
                            step_idx,
                            (call.raw_target or target or "")[:500],
                            *parsed,
                            outcome,
                            (len(observation) + 3) // 4,
                        )
                    )
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
                        cause,
                        detail,
                        site,
                        error_message,
                        family,
                        origin,
                        _event_hash(call.raw_target, observation),
                        call.assistant_context or "",
                    )
                )

        feedback_rows = [
            (
                session.session_id,
                feedback.step_idx,
                feedback.kind,
                feedback.text[:2000],
                ",".join(feedback.keys),
                session.repo or "unknown",
            )
            for session in session_list
            for feedback in session.feedback
        ]
        if feedback_rows:
            _insert_rows(
                con,
                "feedback_events",
                feedback_rows,
                columns=["session_id", "step_idx", "kind", "text", "keys", "repo"],
            )

        if test_rows:
            _insert_rows(
                con,
                "test_results",
                test_rows,
                columns=[
                    "session_id", "repo", "step_idx", "command", "total",
                    "passed", "failed", "errors", "skipped", "outcome",
                    "approx_out_tokens",
                ],
            )
        if events:
            _insert_rows(
                con,
                "tool_events",
                events,
                columns=[
                    "event_id", "session_id", "repo", "step_idx", "segment_idx",
                    "tool_name", "action_type", "verb", "object_id", "target",
                    "raw_target", "outcome", "exit_code", "error_signature",
                    "is_error", "approx_out_tokens", "obs_excerpt", "obs_cause",
                    "error_detail", "error_site", "error_message", "error_family",
                    "error_origin", "event_hash", "assistant_context",
                ],
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
            (
                s["session_id"], s["repo"], s["instance_id"], s["resolved"],
                s["total_turns"], s.get("source", ""), s.get("source_ref", ""),
                s.get("started_at"),
            )
            for s in sessions
        ],
    )
