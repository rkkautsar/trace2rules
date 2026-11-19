from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ..agents import audit_commit, audit_payload, latest_agents_commit, resolve_agents
from ..cli_common import _resolve_repo_label, console
from ..db import DB_PATH, connect

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
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    agent_paths = resolve_agents(agents)
    if not agent_paths:
        console.print("[red]No AGENTS.md or CLAUDE.md found.[/red]")
        raise typer.Exit(1)
    agents = agent_paths[0]
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
    con = connect(db)
    try:
        repo = _resolve_repo_label(
            con,
            repo,
            agents.parent.name,
            agents.parent.resolve().name,
        )
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
                        (latest_agents_commit(path) for path in agent_paths),
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
                        payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt)
                        payload["commit"] = sha
                        payload["committed_at"] = committed_at.isoformat()
                        if coverage[1] < coverage[0]:
                            payload["excluded_no_timestamp"] = coverage[0] - coverage[1]
                            payload["warning"] = (
                                f"{coverage[0] - coverage[1]} sessions have no timestamp and were excluded."
                            )
                    else:
                        payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt)
                        payload["commit"] = sha
                        payload["committed_at"] = committed_at.isoformat()
                        payload["warning"] = (
                            "No session timestamps for this repo; audit used all sessions "
                            "instead of the latest AGENTS.md commit cutoff."
                        )
                except (subprocess.CalledProcessError, OSError, ValueError) as error:
                    payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt)
                    payload["warning"] = f"Could not resolve latest AGENTS.md commit: {error}"
            else:
                payload = audit_payload(con, repo, agent_paths, since=since_dt, until=until_dt)
    finally:
        con.close()
    if prompt_output:
        from ..agents import audit_prompt

        sys.stdout.write(audit_prompt(payload))
        return
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
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
                    rule["text"][:90],
                    rule.get("coverage", "unmeasured"),
                    str(rule.get("followed", 0)),
                    _violations(rule),
                    rule.get("confidence", "none"),
                    _enforcement(rule),
                )
            console.print(table)
        return
    table = Table(title=f"AGENTS.md audit: {repo}")
    for column in ("Rule", "Coverage", "Followed", "Violations", "Confidence", "Enforcement"):
        table.add_column(column)
    for rule in payload["rules"]:
        table.add_row(
            rule["text"][:90],
            rule.get("coverage", "unmeasured"),
            str(rule.get("followed", 0)),
            _violations(rule),
            rule.get("confidence", "none"),
            _enforcement(rule),
        )
    console.print(table)
    for conflict in payload.get("conflicts", []):
        console.print(
            f"[yellow]Conflict:[/yellow] {conflict['reason']}: "
            f"{conflict['a'][:80]} <> {conflict['b'][:80]}"
        )
    if payload.get("warning"):
        console.print(f"[yellow]{payload['warning']}[/yellow]")
    for rule in payload["rules"]:
        console.print(f"[dim]{rule['suggestion']}[/dim]")

