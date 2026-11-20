from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ..agents import audit_payload, resolve_agents
from ..cli_common import _resolve_repo_label, console
from ..db import DB_PATH, connect
from ..redact import redact_text, redact_value
from .audit import _enforcement, _violations


def _table(title: str, payload: dict) -> Table:
    table = Table(title=title)
    for column in ("Rule", "Coverage", "Followed", "Violations", "Confidence", "Enforcement"):
        table.add_column(column)
    for rule in payload["rules"]:
        table.add_row(
            redact_text(rule["text"][:90]),
            rule.get("coverage", "unmeasured"),
            str(rule.get("followed", 0)),
            _violations(rule),
            rule.get("confidence", "none"),
            _enforcement(rule),
        )
    return table


def _holdout_command(
    repo: Annotated[str | None, typer.Option("--repo", help="Repository label; inferred when omitted.")] = None,
    agents: Annotated[
        Path | None,
        typer.Option("--agents", help="AGENTS.md or CLAUDE.md path; auto-detects both when omitted."),
    ] = None,
    fraction: Annotated[
        float,
        typer.Option("--fraction", min=0.05, max=0.9, help="Fraction of sessions held out."),
    ] = 0.3,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    paths = resolve_agents(agents)
    if not paths:
        console.print("[red]No AGENTS.md or CLAUDE.md found.[/red]")
        raise typer.Exit(1)
    con = connect(db)
    try:
        if con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        repo = _resolve_repo_label(con, repo, paths[0].parent.name, paths[0].parent.resolve().name)
        timestamps = [
            row[0]
            for row in con.execute(
                "SELECT started_at FROM sessions WHERE repo = ? AND started_at IS NOT NULL ORDER BY started_at",
                [repo],
            ).fetchall()
        ]
        if len(timestamps) < 2:
            console.print("[red]Need at least two timestamped sessions for a holdout split.[/red]")
            raise typer.Exit(1)
        split = max(1, min(len(timestamps) - 1, int(len(timestamps) * (1 - fraction))))
        cutoff = timestamps[split]
        train = audit_payload(con, repo, paths, until=cutoff)
        holdout = audit_payload(con, repo, paths, since=cutoff)
    finally:
        con.close()
    payload = {
        "repo": repo,
        "cutoff": cutoff.isoformat(),
        "fraction": fraction,
        "train": train,
        "holdout": holdout,
    }
    if json_output:
        typer.echo(json.dumps(redact_value(payload), indent=2))
        return
    console.print(
        f"[bold]{repo}[/bold] holdout from {cutoff.isoformat()}: "
        f"train {train['filtered_sessions']} sessions, holdout {holdout['filtered_sessions']} sessions"
    )
    console.print(_table("Train", train))
    console.print(_table("Holdout", holdout))
