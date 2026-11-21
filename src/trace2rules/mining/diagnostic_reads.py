from __future__ import annotations

import re
from collections import defaultdict

import duckdb

from .core import _TEST_OR_SCRATCH_RE, _representative_window
from .models import Finding

TEST_RE = re.compile(
    r"(?i)pytest|unittest|npm test|yarn test|pnpm test|vitest|jest|"
    r"cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test"
)


def diagnostic_reads(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    """Find reads between a failure and a validated edit."""
    rows = con.execute(
        """
        SELECT e.session_id, s.repo, s.instance_id, e.step_idx, e.action_type,
               e.target, e.error_signature, e.is_error, e.approx_out_tokens
        FROM tool_events e JOIN sessions s USING (session_id)
        WHERE e.action_type IN ('read', 'edit', 'exec') AND e.target <> ''
        ORDER BY e.session_id, e.step_idx
        """
    ).fetchall()
    sessions: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[(row[0], row[1])].append(row)

    aggregates: dict[tuple[str, str, str, str], dict] = {}
    for (session, repo), events in sessions.items():
        for index, source in enumerate(events):
            if not source[7] or source[4] != "exec" or not source[6]:
                continue
            read_index = next(
                (
                    j
                    for j in range(index + 1, min(len(events), index + 11))
                    if events[j][4] == "read"
                    and events[j][5] not in {"", "."}
                    and not events[j][5].startswith(("xd:", "skill:"))
                    and re.search(r"\.[A-Za-z0-9]+$", events[j][5])
                ),
                None,
            )
            if read_index is None:
                continue
            read_target = events[read_index][5]
            edit_index = next(
                (
                    j
                    for j in range(read_index + 1, min(len(events), read_index + 16))
                    if events[j][4] == "edit"
                    and events[j][5] not in {"", "."}
                    and not _TEST_OR_SCRATCH_RE.search(events[j][5])
                ),
                None,
            )
            if edit_index is None:
                continue
            edit_target = events[edit_index][5]
            if read_target == edit_target:
                continue
            success = next(
                (
                    j
                    for j in range(edit_index + 1, min(len(events), edit_index + 19))
                    if events[j][4] == "exec"
                    and not events[j][7]
                    and TEST_RE.search(events[j][5] or "")
                ),
                None,
            )
            if success is None:
                continue
            key = (repo, read_target, edit_target, source[6])
            item = aggregates.setdefault(
                key,
                {
                    "sessions": set(),
                    "instances": set(),
                    "tokens": 0,
                    "example_session": session,
                    "example_step": int(source[3]),
                    "example_recovery_step": int(events[success][3]),
                },
            )
            item["sessions"].add(session)
            item["instances"].add(source[2] or session)
            item["tokens"] += sum(
                int(event[8] or 0)
                for event in events[index : success + 1]
            )
    summaries = [
        (
            repo,
            read_target,
            edit_target,
            error,
            len(value["sessions"]),
            len(value["instances"]),
            int(value["tokens"]),
            value["example_session"],
            value["example_step"],
            value["example_recovery_step"],
        )
        for (repo, read_target, edit_target, error), value in aggregates.items()
        if len(value["sessions"]) >= 3 and len(value["instances"]) >= 2
    ]
    summaries.sort(key=lambda item: (item[4], item[6]), reverse=True)
    selected = summaries[:limit] if limit is not None else summaries
    return [
        Finding(
            kind="diagnostic_read",
            repo=repo,
            title=f"read {read_target} before editing {edit_target} after {error}",
            sessions=sessions_count,
            tokens=tokens,
            paired_instances=instances,
            detail=(
                f"{instances} instances read {read_target[:100]} after "
                f"{error[:80]} and before a validated edit to {edit_target[:100]}"
            ),
            failure_family=error,
            side="agent",
            recommendation=(
                f"After `{error[:100]}` fails, read `{read_target[:100]}` before "
                f"editing `{edit_target[:100]}`; this preceded a successful test "
                "in the observed sessions."
            ),
            window=_representative_window(
                con, example_session, example_step, example_recovery_step
            ),
        )
        for repo, read_target, edit_target, error, sessions_count, instances, tokens, example_session, example_step, example_recovery_step in selected
    ]
