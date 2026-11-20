from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding
from ..error_family import ErrorFamilyConfig, error_family_findings

COMMAND_ERROR_RE = r"(?i)(unrecognized arguments|unrecognized command|unknown option|invalid option|command not found|usage: )"


def command_errors(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for item in error_family_findings(
        con,
        ErrorFamilyConfig(
            scope="repo_target",
            regex=COMMAND_ERROR_RE,
            min_sessions=2,
            action_filter="AND action_type IN ('exec', 'read')",
        ),
    ):
        findings.append(
            Finding(
                kind="command_error",
                repo=item.repo,
                title=item.target,
                sessions=item.sessions,
                tokens=item.tokens,
                detail=f"{item.events} malformed command invocations across {item.sessions} sessions",
                recommendation=(
                    f"Verify the flags for `{item.target[:100]}` with one bounded help call; "
                    "do not retry the same malformed invocation."
                ),
                cause=(item.error or "")[:240],
                side="agent",
                window=_representative_window(
                    con, item.example_session, item.example_step
                ),
            )
        )
    return findings[:limit] if limit is not None else findings
