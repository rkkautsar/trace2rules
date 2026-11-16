from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class SequencePattern:
    sequence: str
    sessions: int
    occurrences: int
    tokens: int
    repos: tuple[str, ...]
    example: str


def mine_sequences(
    con: duckdb.DuckDBPyConnection,
    min_sessions: int = 3,
    max_len: int = 4,
    top: int = 25,
    min_confidence: float = 0.4,
) -> list[SequencePattern]:
    rows = con.execute(
        """
        SELECT e.session_id, e.step_idx, e.repo, e.action_type, l.intent, l.object_type,
               e.outcome, e.approx_out_tokens, e.raw_target
        FROM tool_events e
        JOIN target_labels l
          ON e.tool_name = l.tool_name
         AND e.action_type = l.action_type
         AND e.target = l.target
        WHERE e.tool_name NOT IN ('think', 'finish', 'task_tracker')
          AND e.action_type <> 'other'
          AND l.intent_confidence >= ?
          AND l.object_confidence >= ?
        ORDER BY e.session_id, e.step_idx
        """,
        [min_confidence, min_confidence],
    ).fetchall()

    sessions: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[row[0]].append(row)

    aggregates: dict[tuple, dict] = {}
    for session_id, events in sessions.items():
        tokens = [
            (
                event[4],
                event[5],
                event[6],
            )
            for event in events
        ]
        for length in range(2, max_len + 1):
            for start in range(len(tokens) - length + 1):
                window = tokens[start : start + length]
                if not any(token[2] == "err" for token in window):
                    continue
                key = (events[start][2], tuple(window))
                agg = aggregates.setdefault(
                    key,
                    {
                        "sessions": set(),
                        "occurrences": 0,
                        "tokens": 0,
                        "repos": set(),
                        "example": "",
                    },
                )
                agg["sessions"].add(session_id)
                agg["repos"].update(event[2] for event in events[start : start + length])
                agg["occurrences"] += 1
                agg["tokens"] += sum(
                    event[7]
                    for event in events[start : start + length]
                    if event[6] == "err"
                )
                if not agg["example"]:
                    agg["example"] = " -> ".join(
                        (event[8] or "").replace("\x00", " ")
                        for event in events[start : start + length]
                    )

    patterns = [
        SequencePattern(
            sequence=" -> ".join(":".join(token) for token in key[1]),
            sessions=len(value["sessions"]),
            occurrences=value["occurrences"],
            tokens=value["tokens"],
            repos=tuple(sorted(value["repos"])),
            example=value["example"],
        )
        for key, value in aggregates.items()
        if len(value["sessions"]) >= min_sessions
    ]
    patterns.sort(key=lambda p: (p.sessions, p.tokens), reverse=True)
    return patterns[:top]
