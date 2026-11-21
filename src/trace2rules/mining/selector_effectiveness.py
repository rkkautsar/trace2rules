from __future__ import annotations

from collections import defaultdict

import duckdb

from .core import _representative_window
from .models import Finding


def _mode(raw: str) -> str:
    text = (raw or "").lower()
    if "--collect-only" in text:
        return "collect-only"
    if "::" in text:
        return "nodeid"
    if "-k " in text:
        return "keyword"
    return "file"


def selector_effectiveness(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    rows = con.execute(
        """
        SELECT repo, session_id, step_idx, raw_target, is_error,
               approx_out_tokens
        FROM tool_events
        WHERE action_type = 'exec' AND verb = 'pytest'
          AND coalesce(raw_target, '') <> ''
        """
    ).fetchall()
    by_repo: dict[str, dict[str, dict]] = defaultdict(dict)
    repo_sessions: dict[str, set[str]] = defaultdict(set)
    for repo, session, step, raw, is_error, tokens in rows:
        repo_sessions[repo].add(session)
        mode = _mode(raw)
        item = by_repo[repo].setdefault(
            mode,
            {
                "sessions": set(),
                "total": 0,
                "failures": 0,
                "tokens": 0,
                "example": (session, int(step or 0)),
            },
        )
        item["sessions"].add(session)
        item["total"] += 1
        item["failures"] += int(bool(is_error))
        item["tokens"] += int(tokens or 0)
    findings: list[Finding] = []
    for repo, modes in by_repo.items():
        supported = {
            mode: value
            for mode, value in modes.items()
            if mode != "collect-only"
            and len(value["sessions"]) >= 3
            and value["total"] >= 5
        }
        if len(supported) < 2:
            continue
        rates = {
            mode: value["failures"] / value["total"]
            for mode, value in supported.items()
        }
        best = min(rates, key=rates.get)
        worst = max(rates, key=rates.get)
        if rates[worst] - rates[best] < 0.1:
            continue
        worst_value = supported[worst]
        findings.append(
            Finding(
                kind="selector_effectiveness",
                evidence_kind="descriptive",
                repo=repo,
                title=f"{best} selector: {rates[best]:.0%} failures vs {worst}: {rates[worst]:.0%}",
                sessions=len(repo_sessions[repo]),
                tokens=sum(value["tokens"] for value in supported.values()),
                detail=(
                    "; ".join(
                        f"{mode} {rate:.0%} ({supported[mode]['failures']}/"
                        f"{supported[mode]['total']})"
                        for mode, rate in sorted(rates.items(), key=lambda item: item[1])
                    )
                ),
                side="agent",
                recommendation=(
                    f"Prefer `{best}` selection for pytest in this repo; "
                    f"`{worst}` failed at {rates[worst]:.0%} across "
                    f"{len(worst_value['sessions'])} sessions."
                ),
                window=_representative_window(
                    con, worst_value["example"][0], worst_value["example"][1]
                ),
            )
        )
    findings.sort(key=lambda finding: (finding.sessions, finding.tokens), reverse=True)
    return findings[:limit] if limit is not None else findings
