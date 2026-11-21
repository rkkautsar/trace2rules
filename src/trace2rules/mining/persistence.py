from __future__ import annotations

import duckdb

from .core import _representative_window
from .models import Finding

NOISE_FRAGMENTS = (
    "site-packages",
    "dist-packages",
    "opt/conda",
    "<string>",
    "<stdin>",
    "/tmp/",
    "scratch",
    "reproduce",
    "debug_",
)


def persistent_failure_sites(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    """Find error sites that recur across distinct task instances."""
    rows = con.execute(
        """
        SELECT s.repo, e.error_signature, e.error_site,
               count(DISTINCT e.session_id) AS sessions,
               count(DISTINCT s.instance_id) AS instances,
               count(*) AS events,
               sum(e.approx_out_tokens) AS tokens,
               arg_min(e.session_id, e.step_idx) AS example_session,
               arg_min(e.step_idx, e.step_idx) AS example_step
        FROM tool_events e JOIN sessions s USING (session_id)
        WHERE e.is_error AND e.action_type = 'exec'
          AND e.error_site <> ''
          AND e.error_signature <> ''
          AND e.error_signature NOT IN ('text_error', 'timeout', 'err:unknown')
        GROUP BY 1, 2, 3
        HAVING count(DISTINCT s.instance_id) >= 3
           AND count(DISTINCT e.session_id) >= 3
        ORDER BY instances DESC, sessions DESC
        """
    ).fetchall()
    clean = [
        row
        for row in rows
        if not any(fragment in row[2].lower() for fragment in NOISE_FRAGMENTS)
    ]
    selected = clean[:limit] if limit is not None else clean
    return [
        Finding(
            kind="persistent_site",
            evidence_kind="failure",
            repo=repo,
            title=f"{signature} at {site}",
            sessions=int(sessions),
            tokens=int(tokens or 0),
            paired_instances=int(instances),
            detail=(
                f"{events} failures at this site across {instances} instances "
                f"and {sessions} sessions"
            ),
            failure_family=signature,
            side="repo",
            recommendation=(
                "This failure site recurs across distinct task instances; "
                "treat it as repo-level or test-setup evidence, not task-specific noise."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, signature, site, sessions, instances, events, tokens, example_session, example_step in selected
    ]
