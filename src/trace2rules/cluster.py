from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import duckdb
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_distances

from .detectors import Finding, context_blasts, edit_churn, feedback_loops, global_patterns, hotspots, noop_retries, orientation, permission_denials, premature_completion, low_validation, repeated_reads, site_edit_mismatch, recovery_loops, selector_misses, stuck_loops, workaround_scripts


@dataclass
class _Window:
    repo: str
    encoded: str
    sessions: set[str] = field(default_factory=set)
    wasted_tokens: int = 0
    examples: list[str] = field(default_factory=list)
    tokens: tuple[str, ...] = field(default_factory=tuple)
    keys: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)


def _encode(window: list[tuple]) -> str:
    parts = []
    for position, event in enumerate(window, start=1):
        _session, _repo, _step, _tool, action, verb, object_id, outcome = event[:8]
        parts.append(f"s{position}:{action}:{verb}:{object_id}:{outcome}")
    return " | ".join(parts)


def _tokens(window: list[tuple]) -> tuple[str, ...]:
    return tuple(
        f"{event[4]}:{event[5]}:{event[6]}:{event[7]}" for event in window
    )


def _example(window: list[tuple]) -> str:
    return " -> ".join(
        f"{event[4]}:{(event[11] or event[10]).replace(chr(0), ' ')[:70]}" for event in window
    )


def friction_windows(
    con: duckdb.DuckDBPyConnection,
    min_session_count: int = 2,
    lengths: tuple[int, ...] = (3, 4),
) -> list[_Window]:
    hotspot_targets: dict[str, set[str]] = defaultdict(set)
    for finding in hotspots(con):
        hotspot_targets[finding.repo].add(finding.title)
    blast_targets = {(f.repo, f.title) for f in context_blasts(con)}

    rows = con.execute(
        """
        SELECT session_id, repo, step_idx, tool_name, action_type, verb, object_id,
               outcome, error_signature, approx_out_tokens, target, raw_target,
               segment_idx
        FROM tool_events
        WHERE target <> '' AND action_type <> 'other'
        ORDER BY session_id, step_idx
        """
    ).fetchall()
    sessions: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[row[0]].append(row)

    aggregates: dict[tuple[str, str], _Window] = {}
    for session_id, events in sessions.items():
        for k in lengths:
            for start in range(len(events) - k + 1):
                window = events[start : start + k]
                if len({event[12] for event in window}) > 1:
                    continue
                relevant = any(event[7] == "err" for event in window)
                if not relevant:
                    relevant = any(
                        event[4] in ("read", "search") and event[10] in hotspot_targets[event[1]]
                        for event in window
                    )
                if not relevant:
                    relevant = any(
                        event[9] >= 1500
                        or (event[1], event[10]) in blast_targets
                        for event in window
                    )
                if not relevant:
                    continue

                encoded = _encode(window)
                key = (window[0][1], encoded)
                agg = aggregates.get(key)
                if agg is None:
                    agg = aggregates[key] = _Window(
                        repo=window[0][1],
                        encoded=encoded,
                        tokens=_tokens(window),
                        keys=tuple(
                            (event[3], event[4], event[10]) for event in window
                        ),
                    )
                agg.sessions.add(session_id)
                agg.wasted_tokens += sum(
                    event[9]
                    for event in window
                    if event[7] == "err"
                    or event[9] >= 1500
                    or (event[4] in ("read", "search") and event[10] in hotspot_targets[event[1]])
                )
                example = _example(window)
                if len(agg.examples) < 3 and example not in agg.examples:
                    agg.examples.append(example)

    return [w for w in aggregates.values() if len(w.sessions) >= 1]


def exact_sequences(
    con: duckdb.DuckDBPyConnection,
    length: int = 3,
    min_sessions: int = 2,
    top: int = 25,
    require_error: bool = True,
    min_instances: int = 2,
    min_rework: int = 0,
    min_lift: float = 1.5,
    retry_weight: int = 1000,
) -> list[Finding]:
    repo_sessions = dict(
        con.execute("SELECT repo, count(*) FROM sessions GROUP BY repo").fetchall()
    )
    repo_instances = dict(
        con.execute(
            "SELECT repo, count(DISTINCT instance_id) FROM sessions GROUP BY repo"
        ).fetchall()
    )
    session_instances = dict(
        con.execute("SELECT session_id, instance_id FROM sessions").fetchall()
    )
    session_rework: dict[str, int] = defaultdict(int)
    instance_sessions: dict[str, set[str]] = defaultdict(set)
    for session_id, instance_id in session_instances.items():
        instance_sessions[instance_id].add(session_id)
    for session_id, _verb, _object_id, errors in con.execute(
        """
        SELECT session_id, verb, object_id, count(*) FILTER (WHERE outcome = 'err')
        FROM tool_events
        WHERE outcome = 'err' AND object_id <> ''
        GROUP BY 1, 2, 3
        """
    ).fetchall():
        if errors > 1:
            session_rework[session_id] += errors - 1

    session_setup: dict[str, int] = {}
    grouped = defaultdict(list)
    for row in con.execute(
        """
        SELECT session_id, step_idx, action_type, verb, outcome, approx_out_tokens
        FROM tool_events
        WHERE action_type <> 'other'
        ORDER BY session_id, step_idx
        """
    ).fetchall():
        grouped[row[0]].append(row)
    for session_id, events in grouped.items():
        setup = sum(event[5] for event in events)
        for index, event in enumerate(events):
            if (event[2] == "edit" and event[4] == "ok") or (
                event[3] == "pytest" and event[4] == "ok"
            ):
                setup = sum(item[5] for item in events[:index])
                break
        session_setup[session_id] = setup
    token_sessions: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for session_id, repo, action, verb, object_id, outcome in con.execute(
        """
        SELECT session_id, repo, action_type, verb, object_id, outcome
        FROM tool_events
        WHERE target <> '' AND action_type <> 'other'
        """
    ).fetchall():
        token_sessions[repo][f"{action}:{verb}:{object_id}:{outcome}"].add(session_id)

    scored: list[tuple[float, Finding]] = []
    for window in friction_windows(con, lengths=(length,)):
        if require_error and ":err" not in window.encoded:
            continue
        if len(window.sessions) < min_sessions:
            continue
        instances = {
            session_instances.get(session) for session in window.sessions
        } - {None}
        required_instances = min(
            min_instances, repo_instances.get(window.repo, min_instances)
        )
        if len(instances) < required_instances:
            continue
        n = repo_sessions.get(window.repo, 0)
        expected = float(n)
        for token in window.tokens:
            expected *= len(token_sessions[window.repo].get(token, ())) / n if n else 0
        lift = len(window.sessions) / expected if expected else 0.0
        if lift < min_lift:
            continue
        rework = sum(session_rework.get(session, 0) for session in window.sessions)
        if rework < min_rework:
            continue
        setup_tax = sum(session_setup.get(session, 0) for session in window.sessions)
        paired = 0
        rework_deltas: list[float] = []
        setup_deltas: list[float] = []
        for instance in instances:
            present = [
                session
                for session in window.sessions
                if session_instances.get(session) == instance
            ]
            absent = instance_sessions.get(instance, set()) - set(present)
            if not present or not absent:
                continue
            paired += 1
            present_rework = sum(session_rework.get(s, 0) for s in present) / len(present)
            absent_rework = sum(session_rework.get(s, 0) for s in absent) / len(absent)
            present_setup = sum(session_setup.get(s, 0) for s in present) / len(present)
            absent_setup = sum(session_setup.get(s, 0) for s in absent) / len(absent)
            rework_deltas.append(present_rework - absent_rework)
            setup_deltas.append(present_setup - absent_setup)
        if paired < 2 and len(window.sessions) < 10:
            continue
        delta_rework = sum(rework_deltas) / len(rework_deltas) if rework_deltas else 0.0
        delta_setup = sum(setup_deltas) / len(setup_deltas) if setup_deltas else 0.0
        cost = window.wasted_tokens + rework * retry_weight
        finding = Finding(
            kind="exact",
            repo=window.repo,
            title=window.encoded,
            sessions=len(window.sessions),
            tokens=window.wasted_tokens,
            detail=(
                f"exact {length}-step; lift {lift:.1f}; cost {cost:,}; "
                f"rework {rework}; setup {setup_tax:,}; paired {paired}; "
                f"delta_rework {delta_rework:+.1f}; delta_setup {delta_setup:+,.0f}"
            ),
            sequence=window.encoded,
            variants=tuple(window.examples[:3]),
            event_keys=window.keys,
            lift=lift,
            rework=rework,
            setup_tax=setup_tax,
            cost=cost,
            paired_instances=paired,
            delta_rework=delta_rework,
            delta_setup=delta_setup,
        )
        scored.append((cost, finding))
    scored.sort(key=lambda item: (item[0], item[1].sessions, item[1].lift), reverse=True)
    return [finding for _, finding in scored[:top]]


def _cluster_windows(windows: list[_Window], distance_threshold: float) -> list[Finding]:
    if len(windows) < 2:
        return []

    corpus = [w.encoded for w in windows]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), lowercase=False)
    try:
        matrix = vectorizer.fit_transform(corpus)
        distances = cosine_distances(matrix)
    except ValueError:
        return []

    labels = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric="precomputed",
        linkage="average",
    ).fit_predict(distances)

    groups: dict[int, list[_Window]] = defaultdict(list)
    for window, label in zip(windows, labels):
        groups[int(label)].append(window)

    findings: list[Finding] = []
    for members in groups.values():
        session_ids: set[str] = set()
        for member in members:
            session_ids.update(member.sessions)
        if len(session_ids) < 2:
            continue

        medoid = max(members, key=lambda m: (len(m.sessions), m.wasted_tokens))
        variants: list[str] = []
        for member in sorted(members, key=lambda m: len(m.sessions), reverse=True):
            for example in member.examples:
                if example not in variants:
                    variants.append(example)
                if len(variants) == 3:
                    break
            if len(variants) == 3:
                break

        findings.append(
            Finding(
                kind="cluster",
                repo=medoid.repo,
                title=medoid.encoded,
                sessions=len(session_ids),
                tokens=sum(member.wasted_tokens for member in members),
                detail=f"{len(members)} distinct windows merged",
                sequence=medoid.encoded,
                variants=tuple(variants),
                event_keys=medoid.keys,
            )
        )
    return findings


def cluster_friction(
    con: duckdb.DuckDBPyConnection,
    top: int | None = 25,
    distance_threshold: float = 0.25,
) -> list[Finding]:
    by_repo: dict[str, list[_Window]] = defaultdict(list)
    for window in friction_windows(con):
        by_repo[window.repo].append(window)

    findings: list[Finding] = []
    for repo_windows in by_repo.values():
        if len(repo_windows) > 4000:
            repo_windows = sorted(
                repo_windows, key=lambda w: (len(w.sessions), w.wasted_tokens), reverse=True
            )[:4000]
        findings.extend(_cluster_windows(repo_windows, distance_threshold))

    findings.sort(key=lambda f: (f.sessions, f.tokens), reverse=True)
    return findings[:top] if top is not None else findings


def mine(
    con: duckdb.DuckDBPyConnection, top: int | None = 25
) -> dict[str, list[Finding]]:
    return {
        "hotspots": hotspots(con, limit=top),
        "recovery": recovery_loops(con, limit=top),
        "context": context_blasts(con, limit=top),
        "orientation": orientation(con, limit=top),
        "global": global_patterns(con, limit=top),
        "selector_miss": selector_misses(con, limit=top),
        "workaround": workaround_scripts(con, limit=top),
        "stuck_loop": stuck_loops(con, limit=top),
        "noop_retry": noop_retries(con, limit=top),
        "premature_completion": premature_completion(con, limit=top),
        "low_validation": low_validation(con, limit=top),
        "site_edit_mismatch": site_edit_mismatch(con, limit=top),
        "permission": permission_denials(con, limit=top),
        "reread": repeated_reads(con, limit=top),
        "edit_churn": edit_churn(con, limit=top),
        "feedback": feedback_loops(con, limit=top),
        "clusters": [],
    }
