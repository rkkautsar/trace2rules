from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from ..agents import audit_payload, resolve_agents
from ..cli_common import _ensure_local_sessions, _repo_matches_cwd, console
from ..db import DB_PATH, connect
from ..patterns import mine_sequences

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
        label_count = con.execute("SELECT count(*) FROM target_labels").fetchone()[0]
        repo_rows = con.execute(
            "SELECT repo, count(*) FROM sessions GROUP BY repo ORDER BY repo"
        ).fetchall()

        from ..cluster import exact_sequences, mine as mine_friction
        from ..synthesize import (
            EXACT_LENGTHS,
            _finding_confidence,
            _prompt,
            annotate_findings,
            label_summary,
            select_findings,
        )

        mined = mine_friction(con, top=None if (repo or prompt) else top)
        findings = [
            finding
            for key in ("hotspots", "recovery", "context", "orientation", "selector_miss", "workaround", "stuck_loop", "noop_retry", "premature_completion", "low_validation", "site_edit_mismatch", "feedback")
            for finding in mined[key]
        ]
        exact = []
        for length in EXACT_LENGTHS:
            exact.extend(
                exact_sequences(
                    con,
                    length=length,
                    min_sessions=min_support,
                    top=500,
                    min_instances=2,
                    min_lift=1.5,
                )
            )
        findings.extend(exact)
        if repo:
            findings = [finding for finding in findings if finding.repo == repo]
            repo_rows = [row for row in repo_rows if row[0] == repo]
            exact = [finding for finding in exact if finding.repo == repo]
        if prompt:
            from ..detectors import prompt_context

            agent_paths = resolve_agents(agents)
            chunks: list[str] = []
            for repo_name, _count in repo_rows:
                selected_findings = annotate_findings(
                    con,
                    select_findings(
                        exact, mined, repo_name, min_sessions=min_support
                    ),
                )
                selected_findings = [
                    finding
                    for finding in selected_findings
                    if _finding_confidence(finding) != "low"
                ]
                existing = (
                    audit_payload(con, repo_name, agent_paths)
                    if agent_paths
                    and (repo is not None or _repo_matches_cwd(repo_name))
                    else None
                )
                chunks.append(
                    f"# Repository: {repo_name}\n\n"
                    + _prompt(
                        selected_findings,
                        repo_name,
                        label_summary(con, repo_name),
                        max_rules=5,
                        context=prompt_context(con, repo_name),
                        existing=existing,
                    )
                )
            if out_dir is None:
                sys.stdout.write("\n\n---\n\n".join(chunks))
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                for repo_name, _count in repo_rows:
                    safe = repo_name.replace("/", "__").replace("\\", "__")
                    path = out_dir / f"{safe}.md"
                    prompt_text = next(
                        chunk for chunk in chunks if chunk.startswith(f"# Repository: {repo_name}\n")
                    )
                    path.write_text(prompt_text, encoding="utf-8")
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
            "labels": label_count,
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
        typer.echo(json.dumps(payload, indent=2))
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
        db=db,
    )


