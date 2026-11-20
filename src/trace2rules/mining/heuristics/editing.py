from __future__ import annotations

import re

import duckdb

from ..core import (
    _BOUNDED_RE,
    _DOC_TARGET_RE,
    _TEST_OR_SCRATCH_RE,
    _context_advice,
    _is_orientation,
    _representative_window,
    _repo_window,
)
from ..models import Finding
from ..repetition import repetition_findings

def stuck_loops(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    findings = [
        Finding(
            kind="stuck_loop",
            repo=item.repo,
            title=item.target,
            sessions=item.sessions,
            tokens=item.tokens,
            detail=(
                f"{item.actions} edits across {item.sessions} sessions; "
                f"recurring family {item.family or 'unknown'}"
            ),
            side="agent",
            recommendation="After two edits leave the same failure family unchanged, stop editing and read the traceback site or failing test before another patch.",
            window=_repo_window(
                con,
                item.repo,
                "action_type = 'edit' AND target = ?",
                (item.target,),
            ),
        )
        for item in repetition_findings(
            con,
            "edit",
            per_session_min=None,
            total_min=3,
            sessions_min=2,
            include_family=True,
        )
    ]
    return findings[:limit] if limit is not None else findings

def site_edit_mismatch(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    rows = con.execute(
        """
        SELECT repo, session_id, step_idx, error_site, approx_out_tokens,
               next_action, next_object
        FROM (
            SELECT e.repo, e.session_id, e.step_idx, e.error_site, e.approx_out_tokens,
                   lead(e.action_type) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_action,
                   lead(e.object_id) OVER (PARTITION BY e.session_id ORDER BY e.step_idx) AS next_object
            FROM tool_events e
        ) e
        WHERE error_site <> ''
        """
    ).fetchall()
    counts: dict[str, int] = {}
    sessions_by_repo: dict[str, set[str]] = {}
    tokens_by_repo: dict[str, int] = {}
    examples: dict[str, str] = {}
    for repo, session, step, site, tokens, next_action, next_object in rows:
        if next_action != "edit" or not next_object:
            continue
        site_file = site.rsplit(":", 1)[0]
        if _TEST_OR_SCRATCH_RE.search(site_file):
            continue
        if site_file.rsplit("/", 1)[-1] == next_object.rsplit("/", 1)[-1]:
            continue
        counts[repo] = counts.get(repo, 0) + 1
        sessions_by_repo.setdefault(repo, set()).add(session)
        tokens_by_repo[repo] = tokens_by_repo.get(repo, 0) + int(tokens or 0)
        examples.setdefault(repo, f"{site} -> {next_object}")
    findings = [
        Finding(
            kind="site_edit_mismatch",
            repo=repo,
            title=examples.get(repo, "traceback site differs from edited file"),
            sessions=len(sessions_by_repo.get(repo, set())),
            tokens=tokens_by_repo.get(repo, 0),
            detail=(
                f"{counts[repo]} traceback sites in "
                f"{len(sessions_by_repo.get(repo, set()))} sessions followed by an edit to a different file"
            ),
            side="agent",
            recommendation="Edit the file named by the traceback or failing assertion; do not patch a different layer or the reproducer.",
        )
        for repo in sorted(
            counts,
            key=lambda key: (len(sessions_by_repo.get(key, set())), tokens_by_repo.get(key, 0)),
            reverse=True,
        )
    ]
    return findings[:limit] if limit is not None else findings

def edit_rejections(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = """
        SELECT repo, target,
               count(DISTINCT session_id) AS sessions,
               count(*) AS rejects,
               sum(approx_out_tokens) AS tokens,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step,
               arg_min(coalesce(nullif(error_message, ''), obs_cause), step_idx) AS cause
        FROM tool_events
        WHERE action_type = 'edit' AND outcome = 'err'
          AND target NOT IN ('', '.')
          AND target NOT LIKE '<%'
        GROUP BY 1, 2
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, rejects DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="edit_rejection",
            repo=repo,
            title=target,
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{rejects} rejected edits across {sessions} sessions",
            cause=(cause or "")[:240],
            side="agent",
            recommendation="After an edit is rejected, re-read the target and recompute the anchor; do not retry the same stale patch.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, target, sessions, rejects, tokens, example_session, example_step, cause in con.execute(query).fetchall()
    ]

def blind_edits(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    columns = {
        row[0]
        for row in con.execute("PRAGMA table_info('tool_events')").fetchall()
    }
    create_filter = "AND verb <> 'create'" if "verb" in columns else ""
    query = f"""
        WITH edits AS (
            SELECT session_id, repo, step_idx, target, approx_out_tokens
            FROM tool_events
            WHERE action_type = 'edit'
              AND target NOT IN ('', '.')
              AND target NOT LIKE '<%'
              AND target NOT LIKE '% %'
              AND target NOT LIKE '%>%'
              AND NOT regexp_matches(target, '^(xd|proc|agent|mcp|https?):')
              AND raw_target NOT LIKE 'create%'
              {create_filter}
        )
        SELECT repo,
               count(*) AS edits,
               count(DISTINCT session_id) AS sessions,
               sum(approx_out_tokens) AS tokens,
               arg_max(target, approx_out_tokens) AS example_target,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM edits e
        WHERE NOT EXISTS (
            SELECT 1 FROM tool_events r
            WHERE r.session_id = e.session_id
              AND r.action_type = 'read'
              AND r.target = e.target
              AND r.step_idx < e.step_idx
        )
        AND NOT EXISTS (
            SELECT 1 FROM tool_events c
            WHERE c.session_id = e.session_id
              AND c.action_type = 'edit'
              AND c.raw_target LIKE 'create%'
              AND c.target = e.target
              AND c.step_idx < e.step_idx
        )
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="blind_edit",
            repo=repo,
            title="edits without a preceding read",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits with no prior read of the target; example {example_target[:120]}",
            side="agent",
            recommendation="Read the target before editing it. For a new file, state that it is new and avoid patching unseen existing content.",
            window=_representative_window(con, example_session, example_step),
        )
        for repo, edits, sessions, tokens, example_target, example_session, example_step in con.execute(query).fetchall()
    ]


def generated_edits(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        SELECT repo,
               count(DISTINCT session_id) AS sessions,
               count(*) AS edits,
               sum(approx_out_tokens) AS tokens,
               arg_max(target, approx_out_tokens) AS example_target,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM tool_events
        WHERE action_type = 'edit'
          AND target NOT IN ('', '.')
          AND target NOT LIKE '<%'
          AND regexp_matches(
              target,
              '(?i)(^|/)(dist|node_modules|vendor|site-packages|target|generated|out|coverage)(/|$)|^build/'
              '|\.min\.(js|css)$|\.generated\.'
          )
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="generated_edit",
            repo=repo,
            title="edits under generated or vendored paths",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} edits across {sessions} sessions; example {example_target[:120]}",
            side="agent",
            recommendation=(
                "Edit the source that generates the artifact, not the generated copy. "
                "Add a CI or pre-commit forbidden-path check for generated/vendored directories."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, sessions, edits, tokens, example_target, example_session, example_step in con.execute(query).fetchall()
    ]


def dependency_drift(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Finding]:
    query = r"""
        WITH manifests AS (
            SELECT session_id, repo, step_idx, target, approx_out_tokens
            FROM tool_events
            WHERE action_type = 'edit'
              AND regexp_matches(
                  target,
                  '(?i)(^|/)(pyproject\.toml|requirements[^/]*\.txt|poetry\.lock|uv\.lock|'
                  'Pipfile|Pipfile\.lock|package\.json|package-lock\.json|pnpm-lock\.yaml|'
                  'yarn\.lock|Cargo\.toml|Cargo\.lock|go\.mod|go\.sum)$'
              )
        ),
        dependency_commands AS (
            SELECT DISTINCT session_id
            FROM tool_events
            WHERE action_type = 'exec'
              AND regexp_matches(
                  raw_target,
                  '(?i)\b(pip3?|uv|poetry|conda|npm|yarn|pnpm|cargo|go|bundle|composer|dotnet|mix)\s+'
                  '(install|add|sync|update|remove|uninstall|upgrade|lock|get|ci)\b'
              )
        )
        SELECT repo, count(DISTINCT session_id) AS sessions,
               count(*) AS edits, sum(approx_out_tokens) AS tokens,
               arg_max(target, approx_out_tokens) AS example_target,
               arg_min(session_id, step_idx) AS example_session,
               arg_min(step_idx, step_idx) AS example_step
        FROM manifests m
        WHERE NOT EXISTS (
            SELECT 1 FROM dependency_commands d WHERE d.session_id = m.session_id
        )
        GROUP BY 1
        HAVING count(DISTINCT session_id) >= 2
        ORDER BY sessions DESC, edits DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return [
        Finding(
            kind="dependency_drift",
            repo=repo,
            title="manifest edits without a dependency operation",
            sessions=sessions,
            tokens=int(tokens or 0),
            detail=f"{edits} manifest/lockfile edits across {sessions} sessions; example {example_target[:120]}",
            side="agent",
            recommendation=(
                "Keep dependency changes intentional. Do not edit manifests or lockfiles unless the task requires it; "
                "add a lockfile and dependency-review check in CI."
            ),
            window=_representative_window(con, example_session, example_step),
        )
        for repo, sessions, edits, tokens, example_target, example_session, example_step in con.execute(query).fetchall()
    ]

