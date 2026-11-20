from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import duckdb

from .mining.core import _representative_window, normalize_event_token


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
    repo: str = ""
    lift: float = 0.0
    confidence: float = 0.0
    example_session: str = ""
    example_step: int = 0


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

    aggregates: dict[tuple[str, tuple[str, ...]], dict] = {}
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
                repo = window[0][3]
                agg = aggregates.setdefault(
                    (repo, window_tokens),
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
                agg["repos"].add(repo)
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
            repo=next(iter(value["repos"])),
        )
        for (_repo, tokens), value in aggregates.items()
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


_NOISY_EXEC = {
    "for",
    "set",
    "command",
    "env",
    "sudo",
    "2>/dev/null",
    "printf",
    "test",
}


def _noisy_token(token: str) -> bool:
    if token.endswith((":text_error", ":tool_error", ":err:unknown")):
        return True
    if not token.startswith("exec:"):
        return False
    parts = token.split(":")
    head = parts[1] if len(parts) > 1 else ""
    if head in _NOISY_EXEC or head.startswith(("$", "-")):
        return True
    if len(parts) > 2 and parts[2].startswith("-"):
        return True
    return False


def _specific_error(token: str) -> bool:
    if ":err" not in token and ":timeout" not in token:
        return False
    suffix = token.rsplit(":", 1)[-1]
    return suffix not in {
        "",
        "err",
        "timeout",
        "unknown",
        "text_error",
        "tool_error",
        "exit",
        "err:unknown",
    }


def mine_attribute_patterns(
    con: duckdb.DuckDBPyConnection,
    min_sessions: int = 3,
    max_len: int = 5,
    max_gap: int = 2,
    top: int = 25,
    min_lift: float = 1.5,
) -> list[SequencePattern]:
    """Mine repo-local sequential rules: antecedent => specific error."""
    del max_gap  # kept for CLI compatibility; candidate generation is contiguous

    def iter_sessions():
        cursor = con.execute(
            """
            SELECT session_id, step_idx, segment_idx, repo, action_type, verb,
                   object_id, target, raw_target, outcome, approx_out_tokens,
                   error_signature
            FROM tool_events
            WHERE action_type <> 'other' AND target <> ''
            ORDER BY session_id, step_idx
            """
        )
        current = None
        events: list[tuple] = []
        while True:
            batch = cursor.fetchmany(2048)
            if not batch:
                if current is not None:
                    yield current, events
                return
            for row in batch:
                if current is None:
                    current = row[0]
                elif row[0] != current:
                    yield current, events
                    current = row[0]
                    events = []
                events.append(row)

    max_antecedent = min(3, max(1, max_len - 1))
    antecedent_counts: dict[tuple[str, tuple[str, ...]], tuple[str, int]] = {}
    error_counts: dict[tuple[str, str], tuple[str, int]] = {}
    repo_session_ids: dict[str, set[str]] = defaultdict(set)
    rules: dict[tuple[str, tuple[str, ...], str], dict] = {}

    for session_id, events in iter_sessions():
        tokens = [
            _attribute_token(
                event[4], event[5], event[6], event[7], event[8], event[9], event[11]
            )
            for event in events
        ]
        for end, event in enumerate(events):
            repo = event[3]
            repo_session_ids[repo].add(session_id)
            for antecedent_len in range(1, max_antecedent + 1):
                start = end - antecedent_len
                if start < 0:
                    continue
                if len({events[i][2] for i in range(start, end + 1)}) > 1:
                    continue
                antecedent = tuple(tokens[start:end])
                if any(_noisy_token(token) for token in antecedent):
                    continue
                key = (repo, antecedent)
                previous = antecedent_counts.get(key)
                if previous is None:
                    antecedent_counts[key] = (session_id, 1)
                elif previous[0] != session_id:
                    antecedent_counts[key] = (session_id, previous[1] + 1)

                if event[9] != "err":
                    continue
                error_token = tokens[end]
                if not _specific_error(error_token):
                    continue
                error_key = (repo, error_token)
                previous_error = error_counts.get(error_key)
                if previous_error is None:
                    error_counts[error_key] = (session_id, 1)
                elif previous_error[0] != session_id:
                    error_counts[error_key] = (session_id, previous_error[1] + 1)

                rule_key = (repo, antecedent, error_token)
                rule = rules.setdefault(
                    rule_key,
                    {
                        "sessions": set(),
                        "occurrences": 0,
                        "tokens": 0,
                        "example": "",
                        "example_session": session_id,
                        "example_step": int(event[1]),
                    },
                )
                rule["sessions"].add(session_id)
                rule["occurrences"] += 1
                rule["tokens"] += sum(
                    events[i][10] for i in range(start, end + 1)
                )
                if not rule["example"]:
                    rule["example"] = " -> ".join(
                        (events[i][8] or events[i][7] or "")
                        .replace("\x00", " ")[:80]
                        for i in range(start, end + 1)
                    )

    repo_sessions = {
        repo: len(session_ids) for repo, session_ids in repo_session_ids.items()
    }
    patterns: list[SequencePattern] = []
    for (repo, antecedent, error_token), value in rules.items():
        sessions_count = len(value["sessions"])
        if sessions_count < min_sessions:
            continue
        antecedent_key = (repo, antecedent)
        antecedent_support = antecedent_counts.get(antecedent_key, ("", 0))[1]
        n = repo_sessions.get(repo, 0)
        error_support = error_counts.get((repo, error_token), ("", 0))[1]
        if not n or not antecedent_support or not error_support:
            continue
        confidence = sessions_count / antecedent_support
        error_rate = error_support / n
        lift = confidence / error_rate if error_rate else 0.0
        if confidence < 0.5 or lift < min_lift:
            continue
        patterns.append(
            SequencePattern(
                sequence=" -> ".join((*antecedent, error_token)),
                sessions=sessions_count,
                occurrences=value["occurrences"],
                tokens=value["tokens"],
                repos=(repo,),
                example=value["example"],
                anchored_sessions=sessions_count,
                failure_rate=confidence,
                repo=repo,
                lift=lift,
                confidence=confidence,
                example_session=value["example_session"],
                example_step=value["example_step"],
            )
        )

    patterns.sort(
        key=lambda pattern: (pattern.lift, pattern.confidence, pattern.sessions, pattern.tokens),
        reverse=True,
    )
    kept: list[SequencePattern] = []
    for pattern in patterns:
        if any(
            pattern.repo == longer.repo
            and pattern.sequence in longer.sequence
            and pattern.sessions <= longer.sessions
            for longer in kept
        ):
            continue
        kept.append(pattern)
    return kept[:top]


def attribute_pattern_findings(
    con: duckdb.DuckDBPyConnection,
    limit: int | None = None,
    **kwargs,
):
    """Expose quality attribute patterns as normal mining findings."""
    from .mining.models import Finding

    patterns = mine_attribute_patterns(
        con,
        top=limit or 25,
        **kwargs,
    )
    return [
        Finding(
            kind="attribute_pattern",
            repo=pattern.repo,
            title=pattern.sequence,
            sessions=pattern.sessions,
            tokens=pattern.tokens,
            detail=(
                f"repo-local sequential rule; lift {pattern.lift:.1f}; "
                f"confidence {pattern.confidence:.0%}; "
                f"occurrences {pattern.occurrences}"
            ),
            sequence=pattern.sequence,
            variants=(pattern.example,),
            window=(
                _representative_window(
                    con,
                    pattern.example_session,
                    pattern.example_step,
                    include_scratch=True,
                )
                if pattern.example_session
                else ()
            ),
            lift=pattern.lift,
            side="agent",
        )
        for pattern in patterns
    ]
