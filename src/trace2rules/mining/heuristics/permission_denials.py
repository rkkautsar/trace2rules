from __future__ import annotations

import duckdb

from ...detectors_core import Finding, _representative_window

PERMISSION_RE = r"(?i)(permission denied|operation not permitted|access denied|not authorized|eacces)"


def permission_denials(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = f"""
        SELECT repo, target,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step,
               arg_min(error_message, step_idx) AS example_error
        FROM tool_events
        WHERE outcome = 'err' AND regexp_matches(coalesce(error_message, ''), '{PERMISSION_RE}')
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, events DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    findings: list[Finding] = []
    for repo, target, sessions, events, tokens, session, step, error in con.execute(query).fetchall():
        findings.append(
            Finding(
                kind="permission_denied",
                repo=repo,
                title=target,
                sessions=sessions,
                tokens=int(tokens or 0),
                detail=f"{events} denied calls across {sessions} sessions",
                recommendation=(
                    f"Do not retry `{target[:100]}` after a permission denial; "
                    "request escalation or fix the entrypoint and file permissions first."
                ),
                cause=(error or "")[:240],
                side="agent",
                window=_representative_window(con, session, step),
            )
        )
    return findings
