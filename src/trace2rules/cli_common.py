from __future__ import annotations

from pathlib import Path

import duckdb
import typer
from rich.console import Console

console = Console()


def _resolve_repo_label(
    con: duckdb.DuckDBPyConnection,
    repo: str | None,
    *candidates: str,
) -> str:
    labels = [row[0] for row in con.execute("SELECT DISTINCT repo FROM sessions ORDER BY repo").fetchall()]
    if repo:
        return repo
    names = [candidate for candidate in candidates if candidate]
    names.append(Path.cwd().name)
    for name in names:
        if name in labels:
            return name
        matches = [label for label in labels if label.rsplit("/", 1)[-1] == name]
        if len(matches) == 1:
            return matches[0]
    if len(labels) == 1:
        return labels[0]
    raise typer.BadParameter(
        "Could not infer repository label; pass --repo. Known labels: "
        + ", ".join(labels[:12])
    )


def _repo_matches_cwd(repo_name: str) -> bool:
    return repo_name == Path.cwd().name or repo_name.rsplit("/", 1)[-1] == Path.cwd().name


def _ensure_local_sessions(
    db: Path,
    source: Path = Path.home(),
    limit: int = 200,
    *,
    refresh: bool = False,
) -> bool:
    from .db import connect
    from .ingest.sources.agent_trajectory import ingest_agent_sessions
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    con = connect(db)
    try:
        count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
    finally:
        con.close()
    if count and not refresh:
        return False

    cmd_console = Console(stderr=True)
    cmd_console.print(f"[cyan]No sessions in {db}; ingesting local sessions...[/cyan]")
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=cmd_console,
    )
    with progress:
        ingest_agent_sessions(source, db, limit=limit, progress=progress)
    return True
