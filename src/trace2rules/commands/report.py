from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from ..agents import audit_payload, resolve_agents
from ..cli_common import _ensure_local_sessions, _repo_matches_cwd, console
from ..db import DB_PATH, connect
from ..patterns import mine_sequences
from ..redact import redact_value
from ..synthesis.models import DEFAULT_MAX_RULES


def _report_command(
    top: Annotated[int, typer.Option(help="Rows per detector / pattern cap.")] = 25,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Optional repository label filter; omit to report every detected repo.",
        ),
    ] = None,
    prompt: Annotated[bool, typer.Option("--prompt", help="Emit the evidence prompt for an external agent.")] = False,
    agents: Annotated[
        Path | None,
        typer.Option("--agents", help="Existing AGENTS.md to audit and avoid duplicating."),
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Write one prompt per repo into this directory."),
    ] = None,
    min_support: Annotated[
        int,
        typer.Option("--min-support", min=1, max=100, help="Minimum sessions per prompt finding."),
    ] = 3,
    max_rules: Annotated[
        int,
        typer.Option(
            "--max-rules",
            min=1,
            max=50,
            help="Maximum rules requested per prompt.",
        ),
    ] = DEFAULT_MAX_RULES,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    if out_dir is not None and not prompt:
        raise typer.BadParameter("--out-dir requires --prompt.")
    con = connect(db)
    try:
        session_count, instance_count = con.execute(
            "SELECT count(*), count(DISTINCT instance_id) FROM sessions"
        ).fetchone()
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        repo_rows = con.execute(
            "SELECT repo, count(*) FROM sessions GROUP BY repo ORDER BY repo"
        ).fetchall()
        if agents is not None and repo is None:
            from ..ingest.sources.agent_trajectory import _repo_from_cwd

            inferred = _repo_from_cwd(str(agents.parent))
            matches = [
                name
                for name, _count in repo_rows
                if name == inferred or name.rsplit("/", 1)[-1] == inferred
            ]
            if len(matches) == 1:
                repo = matches[0]

        from ..mining.sequences import exact_sequences
        from ..mining.mine import mine as mine_friction
        from ..synthesis.models import EXACT_LENGTHS
        from ..synthesis.prompt import _finding_confidence, _prompt
        from ..synthesis.selection import select_findings

        mined = mine_friction(con, top=top)
        findings = [
            finding
            for key in ("hotspots", "recovery", "context", "orientation", "selector_miss", "workaround", "stuck_loop", "noop_retry", "premature_completion", "low_validation", "site_edit_mismatch", "feedback", "failure_loop", "edit_rejection", "generated_edit", "dependency_drift", "blind_edit", "environment_thrash", "destructive_command", "no_reproduction", "false_completion", "correction_loop", "test_regression", "test_shrink")
            for finding in mined[key]
        ]
        exact = exact_sequences(
            con,
            lengths=EXACT_LENGTHS,
            min_sessions=min_support,
            top=100,
            min_instances=2,
            min_lift=1.5,
        )
        findings.extend(exact)
        if repo:
            findings = [finding for finding in findings if finding.repo == repo]
            repo_rows = [row for row in repo_rows if row[0] == repo]
            exact = [finding for finding in exact if finding.repo == repo]
        if prompt:
            from ..mining import prompt_context

            explicit_agents = agents is not None
            agent_paths = resolve_agents(agents)
            chunks: list[str] = []

            def agents_for(repo_name: str):
                if agent_paths and (
                    _repo_matches_cwd(repo_name)
                    or (explicit_agents and repo is not None)
                ):
                    return agent_paths
                if explicit_agents:
                    return None
                cwd_rows = con.execute(
                    "SELECT DISTINCT cwd FROM sessions WHERE repo = ? AND cwd <> '' LIMIT 5",
                    [repo_name],
                ).fetchall()
                for (cwd,) in cwd_rows:
                    candidate = Path(cwd)
                    found = resolve_agents(candidate)
                    if found:
                        return found
                return None
            for repo_name, _count in repo_rows:
                selected_findings = select_findings(
                    exact,
                    mined,
                    repo_name,
                    limit=max(20, max_rules),
                    min_sessions=min_support,
                )
                selected_findings = [
                    finding
                    for finding in selected_findings
                    if min_support < 3 or _finding_confidence(finding) != "low"
                ]
                repo_agents = agents_for(repo_name)
                existing = audit_payload(con, repo_name, repo_agents) if repo_agents else None
                repo_root = None
                if repo_agents:
                    repo_root = Path(repo_agents[0]).resolve().parent
                else:
                    cwd_row = con.execute(
                        "SELECT cwd FROM sessions WHERE repo = ? AND cwd <> '' LIMIT 1",
                        [repo_name],
                    ).fetchone()
                    if cwd_row and Path(cwd_row[0]).is_dir():
                        repo_root = Path(cwd_row[0])
                safe_repo = repo_name.replace("\n", " ").replace("\r", " ")
                chunks.append(
                    f"# Repository: {safe_repo}\n\n"
                    + _prompt(
                        selected_findings,
                        repo_name,
                        max_rules=max_rules,
                        context=prompt_context(con, repo_name, repo_root),
                        existing=existing,
                    )
                )
            if out_dir is None:
                sys.stdout.write("\n\n---\n\n".join(chunks))
            else:
                for parent in (out_dir, *out_dir.parents):
                    if parent.is_symlink():
                        raise typer.BadParameter(
                            "--out-dir must not contain symlink components"
                        )
                out_dir.mkdir(parents=True, exist_ok=True)
                for repo_name, _count in repo_rows:
                    safe_repo = repo_name.replace("\n", " ").replace("\r", " ")
                    safe = repo_name.replace("/", "__").replace("\\", "__").replace("\n", " ").replace("\r", " ")
                    path = out_dir / f"{safe}.md"
                    prompt_text = next(
                        chunk for chunk in chunks if chunk.startswith(f"# Repository: {safe_repo}\n")
                    )
                    fd, temporary = tempfile.mkstemp(
                        prefix=f".{path.name}.", dir=out_dir
                    )
                    os.close(fd)
                    Path(temporary).write_text(prompt_text, encoding="utf-8")
                    os.replace(temporary, path)
                    print(f"wrote {path}", file=sys.stderr)
            return
        patterns = [
            pattern
            for pattern in mine_sequences(con, top=top)
            if not repo or repo in pattern.repos
        ]
        payload = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(db),
            "sessions": session_count,
            "instances": instance_count,
            "repos": [{"repo": name, "sessions": count} for name, count in repo_rows],
            "findings": [
                {**asdict(finding), "evidence": finding.evidence}
                for finding in findings
            ],
            "global_patterns": [
                {**asdict(finding), "evidence": finding.evidence}
                for finding in mined.get("global", [])
            ],
            "patterns": [asdict(pattern) for pattern in patterns],
        }
        typer.echo(json.dumps(redact_value(payload), indent=2))
    finally:
        con.close()



def _run_command(
    source: Annotated[
        Path,
        typer.Option("--source", help="Local agent session directory."),
    ] = Path.home(),
    limit: Annotated[int, typer.Option(help="Sessions to ingest if the database is empty.")] = 200,
    agents: Annotated[
        Path | None,
        typer.Option("--agents", help="Existing AGENTS.md to audit and include in the prompt."),
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Write one prompt per detected repo."),
    ] = None,
    min_support: Annotated[
        int,
        typer.Option("--min-support", min=1, max=100, help="Minimum sessions per prompt finding."),
    ] = 3,
    max_rules: Annotated[
        int,
        typer.Option(
            "--max-rules",
            min=1,
            max=50,
            help="Maximum rules requested per prompt.",
        ),
    ] = DEFAULT_MAX_RULES,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    _ensure_local_sessions(db, source, limit)
    _report_command(
        top=25,
        repo=None,
        prompt=True,
        agents=agents,
        out_dir=out_dir,
        min_support=min_support,
        max_rules=max_rules,
        db=db,
    )


