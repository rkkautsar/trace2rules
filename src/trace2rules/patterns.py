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


def _token(action: str, object_id: str, outcome: str) -> str:
    obj = object_id or "?"
    obj = " ".join(obj.split())
    if len(obj) > 140:
        obj = obj[:137] + "..."
    return f"{action}:{obj}:{outcome}"

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
               e.raw_target
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
                event[5]
                if event[5] not in {"", "<path>", "unknown", "?"}
                else (event[8] or "?"),
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
                        "occurrences": 0,
                        "tokens": 0,
                        "repos": set(),
                        "example": "",
                    },
                )
                agg["sessions"].add(session_id)
                agg["repos"].update(event[3] for event in window)
                agg["occurrences"] += 1
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
        )
        for tokens, value in aggregates.items()
        if len(value["sessions"]) >= min_sessions
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
        object_ids = {
            token.rsplit(":", 2)[1]
            for token in pattern.sequence.split(" -> ")
        }
        if len(object_ids) == 1 and pattern.sessions < 5:
            continue
        kept.append(pattern)

    kept.sort(key=lambda pattern: (pattern.sessions, pattern.tokens), reverse=True)
    return kept[:top]
