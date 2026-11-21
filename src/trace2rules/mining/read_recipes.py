from __future__ import annotations

import re
from collections import defaultdict

import duckdb

from .core import _TEST_OR_SCRATCH_RE, _representative_window
from .models import Finding

TEST_RE = re.compile(r"(?i)pytest|unittest|npm test|yarn test|pnpm test|vitest|jest|cargo test|go test|dotnet test|rspec|phpunit|mix test|swift test")
SCRATCH_RE = re.compile(r"(?i)(^|/)(reproduce|repro|scratch|debug|verify|comprehensive|final|test_fix|test_repro)")


def read_recipes(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    """Find files read before an edit that is followed by a successful test."""
    rows = con.execute(
        """
        SELECT e.session_id, s.repo, s.instance_id, e.step_idx, e.action_type,
               e.target, e.outcome, e.is_error, e.approx_out_tokens
        FROM tool_events e JOIN sessions s USING (session_id)
        WHERE e.action_type IN ('read', 'edit', 'exec')
          AND e.target <> ''
        ORDER BY e.session_id, e.step_idx
        """
    ).fetchall()
    sessions: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in rows:
        sessions[(row[0], row[1])].append(row)

    aggregates: dict[tuple[str, str, str], dict] = {}
    for (session, repo), events in sessions.items():
        reads: list[tuple[int, str]] = []
        for index, event in enumerate(events):
            action, target = event[4], event[5]
            if action == "read":
                read_target = target
                if read_target not in {"", "."} and not read_target.startswith(("xd:", "skill:")):
                    reads.append((index, read_target))
                continue
            if action != "edit" or SCRATCH_RE.search(target) or _TEST_OR_SCRATCH_RE.search(target):
                continue
            if not any(
                candidate[4] == "exec"
                and not candidate[7]
                and candidate[6] == "ok"
                and TEST_RE.search(candidate[5] or "")
                for candidate in events[index + 1 : index + 21]
            ):
                continue
            for read_index, read_target in reads[-5:]:
                if read_target == target:
                    continue
                key = (repo, read_target, target)
                item = aggregates.setdefault(
                    key,
                    {
                        "sessions": set(),
                        "instances": set(),
                        "tokens": 0,
                        "example_session": session,
                        "example_step": int(event[3]),
                    },
                )
                item["sessions"].add(session)
                item["instances"].add(event[2] or session)
                item["tokens"] += int(event[8] or 0)
    summaries = [
        (
            repo,
            read_target,
            edit_target,
            len(value["sessions"]),
            int(value["tokens"]),
            len(value["instances"]),
            value["example_session"],
            int(value["example_step"]),
        )
        for (repo, read_target, edit_target), value in aggregates.items()
        if len(value["sessions"]) >= 3 and len(value["instances"]) >= 2
    ]
    summaries.sort(key=lambda item: (item[3], item[4]), reverse=True)
    selected = summaries[:limit] if limit is not None else summaries
    return [
        Finding(
            kind="read_recipe",
            repo=repo,
            title=f"read {read_target} before editing {edit_target}",
            sessions=sessions,
            tokens=tokens,
            paired_instances=instances,
            detail=(
                f"read {read_target[:140]} before {sessions} sessions "
                f"edited {edit_target[:140]} and validated"
            ),
            side="agent",
            recommendation=(
                f"Read `{read_target[:100]}` before editing `{edit_target[:100]}`; "
                "the read preceded a validated edit in these sessions."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, read_target, edit_target, sessions, tokens, instances, example_session, example_step in selected
    ]
