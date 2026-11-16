from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .cluster import exact_sequences, mine as mine_friction
from .db import DB_PATH, connect
from .ingest import DEFAULT_REPOS, SOURCE, ingest as ingest_traces
from .labels import label_targets
from .patterns import mine_sequences
from .synthesize import synthesize as synthesize_rules

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Mine coding-agent trajectories for AGENTS.md rules.")
console = Console()


def _ingest_command(
    repos: Annotated[
        list[str] | None,
        typer.Option("--repos", "-r", help="instance_id prefixes for swe-rebench."),
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="swe-rebench, swe-chat, or agent-trajectory."),
    ] = "swe-rebench",
    limit: Annotated[int, typer.Option(help="Max sessions (per source where applicable).")] = 200,
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Override repo label for local/SWE-chat sessions."),
    ] = None,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            "-s",
            help="Parquet path, dataset directory, or local session root.",
        ),
    ] = None,
) -> None:
    fmt = format.strip().lower()
    if repos and fmt != "swe-rebench":
        raise typer.BadParameter("--repos is only valid with --format swe-rebench.")
    if fmt == "swe-rebench":
        prefixes = repos or DEFAULT_REPOS
        stats = ingest_traces(prefixes, limit=limit, db_path=db, source=source or SOURCE)
    elif fmt == "swe-chat":
        from .sources.swe_chat import ingest_swe_chat

        try:
            stats = [
                ingest_swe_chat(
                    source or "data/swe-chat",
                    db,
                    limit=limit or None,
                    repo=repo,
                )
            ]
        except (FileNotFoundError, ValueError, RuntimeError) as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            raise typer.Exit(1) from None
    elif fmt in {"agent-trajectory", "agent"}:
        from .sources.agent_trajectory import ingest_agent_sessions

        try:
            stats = ingest_agent_sessions(
                source or str(Path.home()),
                db,
                limit=limit or None,
                repo=repo,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            raise typer.Exit(1) from None
    else:
        raise typer.BadParameter(
            "Unknown format. Use swe-rebench, swe-chat, or agent-trajectory."
        )

    if repos and fmt != "swe-rebench":
        raise typer.BadParameter("--repos is only valid with --format swe-rebench.")

    table = Table(title="Ingest summary", header_style="bold")
    table.add_column("Requested source")
    table.add_column("Matched", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Tool events", justify="right")
    for stat in stats:
        table.add_row(stat.prefix, str(stat.matched), str(stat.sessions), f"{stat.events:,}")
    console.print(table)
    for stat in stats:
        if stat.matched == 0:
            console.print(f"[yellow]No sessions found for {stat.prefix!r}.[/yellow]")
    console.print(f"Database: [bold]{db}[/bold]")


app.command("ingest")(_ingest_command)


def _print_summary(summary: list[dict]) -> None:
    table = Table(title="Corpus", header_style="bold")
    table.add_column("Repo")
    table.add_column("Sessions", justify="right")
    table.add_column("Resolved", justify="right")
    table.add_column("Tool events", justify="right")
    table.add_column("Tokens", justify="right")
    for row in summary:
        rate = row["resolved"] / row["sessions"] if row["sessions"] else 0
        table.add_row(
            row["repo"],
            str(row["sessions"]),
            f"{row['resolved']} ({rate:.0%})",
            f"{row['events']:,}",
            f"{row['tokens']:,}",
        )
    console.print(table)


def _print_detector_tables(mined: dict, limit: int = 10) -> None:
    hotspots = mined["hotspots"][:limit]
    if hotspots:
        table = Table(title="Rediscovery hotspots (read-only)", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Target")
        table.add_column("Sessions", justify="right")
        table.add_column("Wasted tokens", justify="right")
        table.add_column("Coverage")
        for finding in hotspots:
            table.add_row(
                finding.repo,
                finding.title,
                str(finding.sessions),
                f"{finding.tokens:,}",
                finding.detail,
            )
        console.print(table)

    recovery = mined["recovery"][:limit]
    if recovery:
        table = Table(title="Command fail -> recovery loops", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Failed command")
        table.add_column("Recovery command")
        table.add_column("Sessions", justify="right")
        table.add_column("Wasted tokens", justify="right")
        for finding in recovery:
            failed, _, recovered = finding.title.partition("  ->  ")
            table.add_row(finding.repo, failed, recovered, str(finding.sessions), f"{finding.tokens:,}")
        console.print(table)

    blasts = mined["context"][:limit]
    if blasts:
        table = Table(title="Context-blasting calls", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Target")
        table.add_column("Sessions", justify="right")
        table.add_column("Total tokens", justify="right")
        table.add_column("Profile")
        for finding in blasts:
            table.add_row(
                finding.repo,
                finding.title,
                str(finding.sessions),
                f"{finding.tokens:,}",
                finding.detail,
            )
        console.print(table)


def _print_clusters(mined: dict, top: int) -> None:
    clusters = mined["clusters"][:top]
    if not clusters:
        console.print("[yellow]No multi-session friction clusters found.[/yellow]")
        return
    table = Table(
        title=f"Top {len(clusters)} friction clusters",
        header_style="bold",
        expand=True,
        show_lines=True,
    )
    table.add_column("#", width=3)
    table.add_column("Repo", width=22)
    table.add_column("Sessions", justify="right", width=8)
    table.add_column("Wasted tokens", justify="right", width=13)
    table.add_column("Medoid sequence")
    table.add_column("Raw variants", width=40)
    for rank, finding in enumerate(clusters, start=1):
        table.add_row(
            str(rank),
            finding.repo,
            str(finding.sessions),
            f"{finding.tokens:,}",
            finding.sequence,
            "\n".join(finding.variants[:3]),
        )
    console.print(table)


def _mine_command(
    top: Annotated[int, typer.Option(help="Clusters / rows per detector to show.")] = 25,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        from .detectors import session_summary

        _print_summary(session_summary(con))
        mined = mine_friction(con, top=top)
        _print_detector_tables(mined)
        _print_clusters(mined, top)
        console.print(
            f"[dim]{len(mined['hotspots'])} hotspots, {len(mined['recovery'])} recovery loops, "
            f"{len(mined['context'])} context blasts, {len(mined['clusters'])} clusters[/dim]"
        )
    finally:
        con.close()


app.command("mine")(_mine_command)


def _label_command(
    limit: Annotated[int, typer.Option(help="Max pending targets; 0 = all.")] = 0,
    rpm: Annotated[int, typer.Option(help="Requests per minute cap.")] = 270,
    workers: Annotated[int, typer.Option(help="Concurrent requests.")] = 6,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    done, failed = label_targets(db, limit=limit or None, rpm=rpm, workers=workers)
    console.print(f"Labeled [bold]{done}[/bold], failed [bold]{failed}[/bold].")


app.command("label")(_label_command)


def _patterns_command(
    top: Annotated[int, typer.Option(help="Patterns to show.")] = 25,
    min_sessions: Annotated[int, typer.Option(help="Minimum session support.")] = 3,
    max_len: Annotated[int, typer.Option(help="Maximum sequence length.")] = 4,
    min_confidence: Annotated[float, typer.Option(help="Minimum label confidence.")] = 0.4,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        label_count = con.execute("SELECT count(*) FROM target_labels").fetchone()[0]
        if label_count == 0:
            console.print("[red]No labels. Run `trace2rules label` first.[/red]")
            raise typer.Exit(1)
        patterns = mine_sequences(
            con,
            min_sessions=min_sessions,
            max_len=max_len,
            top=top,
            min_confidence=min_confidence,
        )
        table = Table(title="Semantic friction sequences", header_style="bold", expand=True)
        table.add_column("#", width=3)
        table.add_column("Sessions", justify="right", width=8)
        table.add_column("Tokens", justify="right", width=10)
        table.add_column("Repos", width=24)
        table.add_column("Pattern")
        table.add_column("Example", width=45)
        for rank, pattern in enumerate(patterns, start=1):
            table.add_row(
                str(rank),
                str(pattern.sessions),
                f"{pattern.tokens:,}",
                ", ".join(pattern.repos),
                pattern.sequence,
                pattern.example[:220],
            )
        console.print(table)
        console.print(f"[dim]{len(patterns)} patterns shown, min support {min_sessions}[/dim]")
    finally:
        con.close()


app.command("patterns")(_patterns_command)


def _exact_command(
    length: Annotated[int, typer.Option(help="Exact sequence length.")] = 3,
    min_sessions: Annotated[int, typer.Option(help="Minimum session support.")] = 3,
    min_instances: Annotated[int, typer.Option(help="Minimum distinct task instances.")] = 3,
    min_rework: Annotated[int, typer.Option(help="Minimum repeated-error count.")] = 0,
    min_lift: Annotated[float, typer.Option(help="Minimum lift over token marginals.")] = 1.5,
    retry_weight: Annotated[int, typer.Option(help="Token cost assigned per rework retry.")] = 1000,
    top: Annotated[int, typer.Option(help="Sequences to show.")] = 25,
    include_ok: Annotated[bool, typer.Option("--include-ok", help="Include windows without an error.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        findings = exact_sequences(
            con,
            length=length,
            min_sessions=min_sessions,
            top=top,
            require_error=not include_ok,
            min_instances=min_instances,
            min_rework=min_rework,
            min_lift=min_lift,
            retry_weight=retry_weight,
        )
        table = Table(title=f"Exact {length}-step sequences", header_style="bold", expand=True)
        table.add_column("#", width=3)
        table.add_column("Repo", width=22)
        table.add_column("Sessions", justify="right", width=8)
        table.add_column("Lift", justify="right", width=7)
        table.add_column("Rework", justify="right", width=7)
        table.add_column("Setup", justify="right", width=8)
        table.add_column("Cost", justify="right", width=10)
        table.add_column("Sequence")
        table.add_column("Variants", width=42)
        for rank, finding in enumerate(findings, start=1):
            table.add_row(
                str(rank),
                finding.repo,
                str(finding.sessions),
                f"{finding.lift:.1f}",
                str(finding.rework),
                f"{finding.setup_tax:,}",
                f"{finding.cost:,}",
                finding.sequence,
                "\n".join(finding.variants[:3]),
            )
        console.print(table)
        console.print(f"[dim]{len(findings)} exact sequences[/dim]")
    finally:
        con.close()


app.command("exact")(_exact_command)


def _synthesize_command(
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Output path for a single --repo; use '-' for stdout.")] = None,
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Directory for per-repo AGENTS files.")] = Path("."),
    repo: Annotated[str | None, typer.Option("--repo", help="Synthesize only this repo.")] = None,
    top: Annotated[int, typer.Option(help="Findings to pass to synthesis.")] = 25,
    offline: Annotated[bool, typer.Option("--offline", help="Skip the LLM and use deterministic rules.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    if out is not None and repo is None:
        raise typer.BadParameter("--out requires --repo; use --out-dir for multiple repos.")
    cmd_console = Console(stderr=True) if out is not None and str(out) == "-" else console
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            cmd_console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        try:
            results = synthesize_rules(
                con,
                out_path=out,
                out_dir=out_dir,
                repo=repo,
                top=top,
                offline=offline,
                console=cmd_console,
            )
        except ValueError as error:
            cmd_console.print(f"[red]{escape(str(error))}[/red]")
            raise typer.Exit(1) from None
        for path, synthesis in results:
            cmd_console.print(
                f"Wrote [bold]{path}[/bold] with {len(synthesis.rules)} rules for {synthesis.repo}."
            )
    finally:
        con.close()


app.command("synthesize")(_synthesize_command)


if __name__ == "__main__":
    app()
