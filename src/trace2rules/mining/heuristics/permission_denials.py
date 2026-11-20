from __future__ import annotations

import duckdb

from ..core import _representative_window
from ..models import Finding
from ..error_family import ErrorFamilyConfig, error_family_findings

PERMISSION_RE = r"(?i)(permission denied|operation not permitted|access denied|not authorized|eacces)"


def permission_denials(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for item in error_family_findings(
        con,
        ErrorFamilyConfig(scope="repo_target", regex=PERMISSION_RE, min_sessions=2),
    ):
        findings.append(
            Finding(
                kind="permission_denied",
                repo=item.repo,
                title=item.target,
                sessions=item.sessions,
                tokens=item.tokens,
                detail=f"{item.events} denied calls across {item.sessions} sessions",
                recommendation=(
                    f"Do not retry `{item.target[:100]}` after a permission denial; "
                    "request escalation or fix the entrypoint and file permissions first."
                ),
                cause=(item.error or "")[:240],
                side="agent",
                window=_representative_window(
                    con, item.example_session, item.example_step
                ),
            )
        )
    return findings[:limit] if limit is not None else findings
