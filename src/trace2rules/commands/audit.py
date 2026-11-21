from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ..agents import _agents_root, audit_commit, audit_payload, latest_agents_commit, resolve_agents
from ..cli_common import _auto_ingest, _resolve_repo_label, console
from ..db import DB_PATH, connect
from ..redact import redact_text, redact_value

def _violations(rule: dict) -> str:
    opportunities = rule.get("opportunities", 0)
    if not opportunities:
        return "-"
    violations = rule.get("violation_sessions", 0)
    return f"{violations}/{opportunities} ({violations / opportunities:.0%})"


def _enforcement(rule: dict) -> str:
    if rule.get("category") in {"read_before_edit", "read_hygiene", "ordering"}:
        return "guidance only"
    if rule.get("existing_enforcement"):
        return rule["existing_enforcement"]
    return rule.get("enforcement_status", "unknown")


def _audit_command(
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Optional repository label filter; inferred from the checkout when omitted.",
        ),
    ] = None,
    agents: Annotated[
        Path | None,
        typer.Option("--agents", help="AGENTS.md or CLAUDE.md path; auto-detects both when omitted."),
    ] = None,
    proposal: Annotated[
        Path | None,
        typer.Option("--proposal", help="Validate a proposed rules file before applying it."),
    ] = None,
    since: Annotated[str | None, typer.Option("--since", help="Only sessions at/after ISO timestamp.")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Only sessions before ISO timestamp.")] = None,
    latest_commit: Annotated[
        bool,
        typer.Option(
            "--latest-commit/--all-sessions",
            help="Use the latest AGENTS.md commit as the default session cutoff.",
        ),
    ] = True,
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Compare rule effectiveness before/after a commit."),
    ] = None,
    prompt_output: Annotated[
        bool,
        typer.Option("--prompt", help="Emit a prompt for a fresh-context review agent."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Session root or parquet path; repeat for multiple."),
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Ingest format: agent-trajectory, swe-rebench, open-swe, or swe-chat."),
    ] = "agent-trajectory",
    limit: Annotated[
        int,
        typer.Option(help="Newest sessions per source when auto-ingesting; 0 for all."),
    ] = 200,
    refresh_cache: Annotated[
        bool,
        typer.Option("--refresh-cache", help="Re-download remote parquet while auto-ingesting."),
    ] = False,
    split_compactions: Annotated[
        bool,
        typer.Option(
            "--split-compactions/--no-split-compactions",
            help="Treat each local compaction segment as its own session.",
        ),
    ] = True,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    if proposal is not None:
        if agents is not None:
            console.print("[red]--proposal cannot be combined with --agents[/red]")
            raise typer.Exit(2)
        if commit:
            console.print("[red]--proposal cannot be combined with --commit[/red]")
            raise typer.Exit(2)
        agents = proposal
        latest_commit = False
    agent_paths = resolve_agents(agents)
    if not agent_paths:
        console.print("[red]No AGENTS.md or CLAUDE.md found.[/red]")
        raise typer.Exit(1)
    repo_root = None
    repo_agents: list[Path] = []
    for agent_path in agent_paths:
        candidate = _agents_root(agent_path.resolve().parent)
        if (candidate / ".git").exists():
            repo_root = candidate
            repo_agents.append(agent_path)
    if not repo_agents:
        repo_agents = [agent_paths[0]]
        repo_root = _agents_root(agent_paths[0].resolve().parent)
    if proposal is not None:
        cwd_root = _agents_root(Path.cwd())
        if (cwd_root / ".git").exists() and (
            repo_root is None or not (repo_root / ".git").exists()
        ):
            repo_root = cwd_root
    agents = repo_agents[0]
    def parse_time(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise typer.BadParameter(f"Invalid timestamp: {value}") from error

    if commit and (since or until):
        console.print("[red]--commit cannot be combined with --since/--until[/red]")
        raise typer.Exit(2)
    try:
        _auto_ingest(
            db,
            source=source,
            format=format,
            limit=limit,
            repo=repo,
            refresh_cache=refresh_cache,
            split_compactions=split_compactions,
        )
    except Exception as error:
        console.print(f"[yellow]Could not auto-ingest sessions: {error}[/yellow]")
    con = connect(db)
    try:
        if con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        repoNames = (
            (Path.cwd().name, repo_root.name if repo_root else "", agents.parent.name)
            if proposal is not None
            else (agents.parent.name, agents.parent.resolve().name)
        )
        repo = _resolve_repo_label(con, repo, *repoNames)
        if commit:
            if len(agent_paths) > 1:
                console.print("[red]--commit supports one AGENTS.md/CLAUDE.md; pass --agents.[/red]")
                raise typer.Exit(2)
            try:
                payload = audit_commit(con, repo, agents, commit)
            except (subprocess.CalledProcessError, OSError, ValueError) as error:
                console.print(f"[red]Could not audit commit {commit}: {error}[/red]")
                raise typer.Exit(1)
        else:
            since_dt = parse_time(since)
            until_dt = parse_time(until)
            if latest_commit:
                try:
                    sha, committed_at = max(
                        (latest_agents_commit(path) for path in repo_agents),
                        key=lambda item: item[1],
                    )
                    coverage = con.execute(
                        "SELECT count(*), count(*) FILTER (WHERE started_at IS NOT NULL) "
                        "FROM sessions WHERE repo = ?",
                        [repo],
                    ).fetchone()
                    if coverage[1]:
                        if since_dt is None:
                            since_dt = committed_at
                        payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt, root=repo_root, source_scope="repo" if proposal is not None else None)
                        payload["commit"] = sha
                        payload["committed_at"] = committed_at.isoformat()
                        if coverage[1] < coverage[0]:
                            payload["excluded_no_timestamp"] = coverage[0] - coverage[1]
                            payload["warning"] = (
                                f"{coverage[0] - coverage[1]} sessions have no timestamp and were excluded."
                            )
                    else:
                        payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt, root=repo_root, source_scope="repo" if proposal is not None else None)
                        payload["commit"] = sha
                        payload["committed_at"] = committed_at.isoformat()
                        payload["warning"] = (
                            "No session timestamps for this repo; audit used all sessions "
                            "instead of the latest AGENTS.md commit cutoff."
                        )
                except (subprocess.CalledProcessError, OSError, ValueError) as error:
                    payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt, root=repo_root, source_scope="repo" if proposal is not None else None)
                    payload["warning"] = f"Could not resolve latest AGENTS.md commit: {error}"
            else:
                payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt, root=repo_root, source_scope="repo" if proposal is not None else None)
    finally:
        con.close()
    if prompt_output:
        from ..agents import audit_prompt

        sys.stdout.write(audit_prompt(payload))
        return
    if json_output:
        typer.echo(json.dumps(redact_value(payload), indent=2))
        return
    if commit:
        console.print(
            f"[bold]{payload['commit']}[/bold] committed {payload['committed_at']}"
        )
        console.print(f"Added rules: {len(payload['added'])}; removed: {len(payload['removed'])}")
        for label, key in (("After", "after"), ("Before", "before")):
            table = Table(title=f"{label} commit audit: {repo}")
            for column in ("Rule", "Coverage", "Followed", "Violations", "Confidence", "Enforcement"):
                table.add_column(column)
            for rule in payload[key]:
                table.add_row(
                    redact_text(rule["text"][:90]),
                    rule.get("coverage", "unmeasured"),
                    str(rule.get("followed", 0)),
                    _violations(rule),
                    rule.get("confidence", "none"),
                    _enforcement(rule),
                )
            console.print(table)
        return
    filtered = payload.get("filtered_sessions", 0)
    total = payload.get("sessions", 0)
    cutoff = payload.get("commit")
    console.print(
        f"Audited [bold]{filtered}/{total}[/bold] sessions"
        + (f" since {cutoff[:12]}" if cutoff else "")
    )
    if total < 10:
        console.print(
            "[yellow]Fewer than 10 local sessions; treat these rates as weak evidence.[/yellow]"
        )
    if filtered == 0:
        console.print(
            "[yellow]No sessions matched this cutoff; pass --all-sessions to audit full history.[/yellow]"
        )
    table = Table(title=f"AGENTS.md audit: {repo}")
    for column in ("Rule", "Verdict", "Coverage", "Followed", "Violations", "Confidence", "Enforcement"):
        table.add_column(column)
    for rule in payload["rules"]:
        table.add_row(
            redact_text(rule["text"][:90]),
            rule.get("verdict", "keep"),
            rule.get("coverage", "unmeasured"),
            str(rule.get("followed", 0)),
            _violations(rule),
            rule.get("confidence", "none"),
            _enforcement(rule),
        )
    console.print(table)
    if payload.get("global_rules"):
        global_table = Table(title=f"Global AGENTS.md: {repo}")
        for column in ("Rule", "Verdict", "Coverage", "Followed", "Violations", "Confidence", "Enforcement"):
            global_table.add_column(column)
        for rule in payload["global_rules"]:
            global_table.add_row(
                redact_text(rule["text"][:90]),
                rule.get("verdict", "keep"),
                rule.get("coverage", "unmeasured"),
                str(rule.get("followed", 0)),
                _violations(rule),
                rule.get("confidence", "none"),
                _enforcement(rule),
            )
        console.print(global_table)
    for conflict in payload.get("conflicts", []):
        console.print(
            f"[yellow]Conflict:[/yellow] {conflict['reason']}: "
            f"{redact_text(conflict['a'][:80])} <> {redact_text(conflict['b'][:80])}"
        )
    if payload.get("warning"):
        console.print(f"[yellow]{payload['warning']}[/yellow]")
    for rule in payload["rules"]:
        console.print(f"[dim]{redact_text(rule['suggestion'])}[/dim]")

