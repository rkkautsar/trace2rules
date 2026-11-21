from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding

TOOLCHAIN_PATTERN = (
    r"(?i)(?:cargo|rustc)\s+\+nightly|RUSTC_BOOTSTRAP\s*=|rustup\s+override|"
    r"nvm\s+(?:use|install)|uv\s+python\s+pin|pyenv\s+local|"
    r"go\s+env\s+-w\s+GOTOOLCHAIN|asdf\s+|mise\s+use"
)


def toolchain_churn(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = rf"""
        SELECT repo, coalesce(nullif(raw_target, ''), target) AS command,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM tool_events
        WHERE action_type = 'exec' AND regexp_matches(coalesce(raw_target, target), '{TOOLCHAIN_PATTERN}')
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, events DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="toolchain_churn",
            repo=repo,
            title=command,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{events} ad-hoc toolchain selections across {sessions} sessions",
            side="agent",
            recommendation="Declare one toolchain in the repository and use it consistently; do not switch toolchains ad hoc.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, command, sessions, events, tokens, example_session, example_step in con.execute(query).fetchall()
    ]
