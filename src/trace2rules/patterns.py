from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

import duckdb

from .mining.core import normalize_event_token


@dataclass(frozen=True)
class SequencePattern:
    sequence: str
    sessions: int
    occurrences: int
    tokens: int
    repos: tuple[str, ...]
    example: str
    anchored_sessions: int = 0
    failure_rate: float = 0.0


def _token(
    action: str,
    verb: str,
    object_id: str,
    target: str,
    raw_target: str,
    outcome: str,
) -> str:
    return normalize_event_token(
        action, verb, object_id, target, raw_target, outcome
    )

def mine_sequences(
    con: duckdb.DuckDBPyConnection,
    min_sessions: int = 3,
    max_len: int = 4,
    top: int = 25,
) -> list[SequencePattern]:
    rows = con.execute(
        """
        SELECT e.session_id, e.step_idx, e.segment_idx, e.repo,
               e.action_type, e.object_id, e.outcome, e.approx_out_tokens,
               e.raw_target, e.verb, e.target
        FROM tool_events e
        WHERE e.tool_name NOT IN ('think', 'finish', 'task_tracker')
          AND e.action_type <> 'other'
        ORDER BY e.session_id, e.step_idx
        """
    ).fetchall()

    sessions: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[row[0]].append(row)

    aggregates: dict[tuple[str, ...], dict] = {}
    for session_id, events in sessions.items():
        tokens = [
            _token(
                event[4],
                event[9],
                event[5],
                event[10],
                event[8],
                event[6],
            )
            for event in events
        ]
        for length in range(2, max_len + 1):
            for start in range(len(events) - length + 1):
                window = events[start : start + length]
                if any(
                    window[index][1] + 1 != window[index + 1][1]
                    or window[index][2] != window[index + 1][2]
                    for index in range(len(window) - 1)
                ):
                    continue
                window_tokens = tuple(tokens[start : start + length])
                if not any(event[6] == "err" for event in window):
                    continue
                agg = aggregates.setdefault(
                    window_tokens,
                    {
                        "sessions": set(),
                        "anchored_sessions": set(),
                        "occurrences": 0,
                        "anchored": 0,
                        "tokens": 0,
                        "repos": set(),
                        "example": "",
                    },
                )
                agg["sessions"].add(session_id)
                agg["repos"].update(event[3] for event in window)
                agg["occurrences"] += 1
                if window[-1][6] == "err":
                    agg["anchored_sessions"].add(session_id)
                    agg["anchored"] += 1
                agg["tokens"] += sum(
                    event[7] for event in window if event[6] == "err"
                )
                if not agg["example"]:
                    agg["example"] = " -> ".join(
                        (event[8] or event[5] or "").replace("\x00", " ")
                        for event in window
                    )

    candidates = [
        SequencePattern(
            sequence=" -> ".join(tokens),
            sessions=len(value["sessions"]),
            occurrences=value["occurrences"],
            tokens=value["tokens"],
            repos=tuple(sorted(value["repos"])),
            example=value["example"],
            anchored_sessions=len(value["anchored_sessions"]),
            failure_rate=(
                value["anchored"] / value["occurrences"] if value["occurrences"] else 0.0
            ),
        )
        for tokens, value in aggregates.items()
        if len(value["sessions"]) >= min_sessions
        and len(value["anchored_sessions"]) >= min_sessions
    ]

    # Prefer maximal windows; a shorter prefix with the same support is redundant.
    candidates.sort(key=lambda pattern: len(pattern.sequence.split(" -> ")), reverse=True)
    kept: list[SequencePattern] = []
    for pattern in candidates:
        if any(
            pattern.sequence in longer.sequence
            and pattern.sessions <= longer.sessions
            for longer in kept
        ):
            continue
        kept.append(pattern)

    kept.sort(
        key=lambda pattern: (
            pattern.anchored_sessions,
            pattern.failure_rate,
            pattern.sessions,
            pattern.tokens,
        ),
        reverse=True,
    )
    return kept[:top]


def _attribute_token(
    action: str,
    verb: str,
    object_id: str,
    target: str,
    raw_target: str,
    outcome: str,
    error_family: str,
) -> str:
    return normalize_event_token(
        action, verb, object_id, target, raw_target, outcome, error_family
    )


def mine_attribute_patterns(
    con: duckdb.DuckDBPyConnection,
    min_sessions: int = 3,
    max_len: int = 5,
    max_gap: int = 2,
    top: int = 25,
) -> list[SequencePattern]:
    """Mine error-anchored sequences with normalized attributes and small gaps.

    Each pattern ends at an error event. Non-error events may be skipped up to
    max_gap so one extra read does not erase an otherwise shared workflow.
    """
    rows = con.execute(
        """
        SELECT session_id, step_idx, segment_idx, repo, action_type, verb,
               object_id, target, raw_target, outcome, approx_out_tokens,
               error_signature
        FROM tool_events
        WHERE action_type <> 'other' AND target <> ''
        ORDER BY session_id, step_idx
        """
    ).fetchall()
    sessions: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[row[0]].append(row)

    aggregates: dict[tuple[str, ...], dict] = {}
    for session_id, events in sessions.items():
        tokens = [
            _attribute_token(
                event[4], event[5], event[6], event[7], event[8], event[9], event[11]
            )
            for event in events
        ]
        for end, event in enumerate(events):
            if event[9] != "err":
                continue
            for length in range(2, max_len + 1):
                start = max(0, end - (length - 1) * (max_gap + 1))
                for indices in combinations(range(start, end), length - 1):
                    if not all(
                        indices[i] - indices[i - 1] <= max_gap + 1
                        for i in range(1, len(indices))
                    ):
                        continue
                    sequence = tuple(tokens[i] for i in (*indices, end))
                    if len({event[2] for event in (events[i] for i in (*indices, end))}) > 1:
                        continue
                    agg = aggregates.setdefault(
                        sequence,
                        {"sessions": set(), "occurrences": 0, "tokens": 0, "repos": set(), "example": ""},
                    )
                    agg["sessions"].add(session_id)
                    agg["repos"].update(events[i][3] for i in (*indices, end))
                    agg["occurrences"] += 1
                    agg["tokens"] += sum(events[i][10] for i in (*indices, end))
                    if not agg["example"]:
                        agg["example"] = " -> ".join(
                            (events[i][8] or events[i][7] or "").replace("\x00", " ")[:80]
                            for i in (*indices, end)
                        )

    patterns = [
        SequencePattern(
            sequence=" -> ".join(sequence),
            sessions=len(value["sessions"]),
            occurrences=value["occurrences"],
            tokens=value["tokens"],
            repos=tuple(sorted(value["repos"])),
            example=value["example"],
            anchored_sessions=len(value["sessions"]),
            failure_rate=1.0,
        )
        for sequence, value in aggregates.items()
        if len(value["sessions"]) >= min_sessions
    ]
    patterns.sort(
        key=lambda pattern: (pattern.sessions, pattern.tokens), reverse=True
    )
    return patterns[:top]
