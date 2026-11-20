from __future__ import annotations

import duckdb

from ..models import Finding
from ..error_family import ErrorFamilyConfig, error_family_findings

def global_patterns(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    findings = [
        Finding(
            kind="global_failure",
            repo="*",
            title=item.family,
            sessions=item.sessions,
            tokens=item.tokens,
            detail=f"{item.instances} instances across {item.repos} repos",
            cause=(item.error or "")[:240],
            side="agent",
            failure_family=item.family,
            variants=tuple((item.repo_names or "").split(", ")),
        )
        for item in error_family_findings(
            con,
            ErrorFamilyConfig(
                scope="cross_repo",
                min_repos=2,
                min_instances=3,
                min_sessions=4,
            ),
        )
    ]
    return findings[:limit] if limit is not None else findings
