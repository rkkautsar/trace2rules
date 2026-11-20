from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import duckdb

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .agents import resolve_agents
from .cli_common import _ensure_local_sessions, _repo_matches_cwd, console
from .commands.report import _report_command, _run_command
from .commands.audit import _audit_command
from .commands.holdout import _holdout_command
from .db import DB_PATH, connect
from .ingest import DEFAULT_REPOS, SOURCE, ingest as ingest_traces

load_dotenv(Path.cwd() / ".env")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Mine coding-agent trajectories for AGENTS.md rules.")


def _ingest_command(
    repos: Annotated[
        list[str] | None,
        typer.Option("--repos", "-r", help="instance_id prefixes for swe-rebench."),
    ] = None,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="agent-trajectory (default), swe-rebench, open-swe, or swe-chat.",
        ),
    ] = "agent-trajectory",
    limit: Annotated[int, typer.Option(help="Max sessions (per source where applicable).")] = 200,
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Override repo label for local/SWE-chat sessions."),
    ] = None,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
    refresh_cache: Annotated[
        bool,
        typer.Option(
            "--refresh-cache",
            help="Re-download the remote parquet instead of using the cached copy.",
        ),
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="Parquet path, dataset directory, or local session root; repeat for multiple.",
        ),
    ] = None,
    split_compactions: Annotated[
        bool,
        typer.Option(
            "--split-compactions/--no-split-compactions",
            help="Treat each local compaction segment as its own session.",
        ),
    ] = True,
) -> None:
    fmt = format.strip().lower()
    sources = source or []
    failed_sources: list[tuple[str, Exception]] = []
    if repos and fmt not in {"swe-rebench", "open-swe"}:
        raise typer.BadParameter("--repos is only valid with --format swe-rebench or open-swe.")
    if fmt in {"swe-rebench", "open-swe"}:
        prefixes = repos or DEFAULT_REPOS
        from .ingest import OPEN_SWE_SOURCE

        selected_sources = sources or ([OPEN_SWE_SOURCE] if fmt == "open-swe" else [SOURCE])
        stats = []
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=Console(stderr=True),
            transient=False,
            disable=not sys.stderr.isatty(),
        )
        with progress:
            for selected_source in selected_sources:
                if selected_source.startswith(("hf://", "http://", "https://")):
                    console.print("[dim]Scanning remote parquet; first run downloads metadata/data...[/dim]")
                try:
                    stats.extend(
                        ingest_traces(
                            prefixes,
                            limit=limit,
                            db_path=db,
                            source=selected_source,
                            progress=progress,
                            refresh_cache=refresh_cache,
                            source_format=fmt,
                        )
                    )
                except (duckdb.Error, OSError, RuntimeError) as error:
                    failed_sources.append((selected_source, error))
    elif fmt == "swe-chat":
        from .ingest.sources.swe_chat import ingest_swe_chat

        selected_sources = sources or ["data/swe-chat"]
        stats = []
        for selected_source in selected_sources:
            try:
                stats.append(
                    ingest_swe_chat(
                        selected_source,
                        db,
                        limit=limit or None,
                        repo=repo,
                    )
                )
            except (FileNotFoundError, ValueError, RuntimeError) as error:
                failed_sources.append((selected_source, error))
    elif fmt in {"agent-trajectory", "agent"}:
        from .ingest.sources.agent_trajectory import ingest_agent_sessions

        selected_sources = sources or [str(Path.home())]
        stats = []
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=Console(stderr=True),
            transient=False,
            disable=not sys.stderr.isatty(),
        )
        with progress:
            for selected_source in selected_sources:
                try:
                    stats.extend(
                        ingest_agent_sessions(
                            selected_source,
                            db,
                            limit=limit or None,
                            repo=repo,
                            progress=progress,
                            split_compactions=split_compactions,
                        )
                    )
                except (FileNotFoundError, ValueError, RuntimeError) as error:
                    failed_sources.append((selected_source, error))
    else:
        raise typer.BadParameter(
            "Unknown format. Use swe-rebench, open-swe, swe-chat, or agent-trajectory."
        )

    if repos and fmt not in {"swe-rebench", "open-swe"}:
        raise typer.BadParameter("--repos is only valid with --format swe-rebench or open-swe.")

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
    for failed_source, error in failed_sources:
        console.print(
            f"[red]Failed {escape(str(failed_source))}: {escape(str(error))}[/red]"
        )
    if failed_sources and not stats:
        raise typer.Exit(1)
    console.print(f"Database: [bold]{db}[/bold]")


app.command("ingest")(_ingest_command)


def _print_summary(summary: list[dict]) -> None:
    table = Table(title="Corpus", header_style="bold")
    table.add_column("Repo")
    table.add_column("Sessions", justify="right")
    table.add_column("Tool events", justify="right")
    table.add_column("Tokens", justify="right")
    for row in summary:
        table.add_row(
            escape(row["repo"]),
            str(row["sessions"]),
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
                escape(finding.repo),
                escape(finding.title),
                str(finding.sessions),
                f"{finding.tokens:,}",
                escape(finding.detail),
            )
        console.print(table)

    orientation_findings = mined.get("orientation", [])[:limit]
    if orientation_findings:
        table = Table(title="Orientation cost (repo-side)", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Target")
        table.add_column("Sessions", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Advice")
        for finding in orientation_findings:
            table.add_row(
                escape(finding.repo),
                escape(finding.title),
                str(finding.sessions),
                f"{finding.tokens:,}",
                escape(finding.recommendation),
            )
        console.print(table)

    recovery = mined["recovery"][:limit]
    if recovery:
        table = Table(title="Command fail -> recovery loops", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Failed command")
        table.add_column("Recovery command")
        table.add_column("Cause")
        table.add_column("Sessions", justify="right")
        table.add_column("Wasted tokens", justify="right")
        for finding in recovery:
            failed, _, recovered = finding.title.partition("  ->  ")
            table.add_row(
                escape(finding.repo),
                escape(failed),
                escape(recovered),
                escape(finding.cause or finding.detail),
                str(finding.sessions),
                f"{finding.tokens:,}",
            )
        console.print(table)

    failure_loops = mined.get("failure_loop", [])[:limit]
    if failure_loops:
        table = Table(title="Failure loops (same failure after edits)", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Failure")
        table.add_column("Cause")
        table.add_column("Sessions", justify="right")
        table.add_column("Tokens", justify="right")
        for finding in failure_loops:
            table.add_row(
                escape(finding.repo),
                escape(finding.title),
                escape(finding.cause or finding.detail),
                str(finding.sessions),
                f"{finding.tokens:,}",
            )
        console.print(table)

    signal_rows = []
    for key, label in (
        ("edit_rejection", "rejected edit"),
        ("generated_edit", "generated edit"),
        ("dependency_drift", "dependency drift"),
        ("blind_edit", "blind edit"),
        ("environment_thrash", "environment thrash"),
        ("destructive_command", "destructive command"),
        ("no_reproduction", "no reproduction"),
        ("false_completion", "false completion"),
        ("correction_loop", "correction loop"),
        ("test_regression", "test regression"),
        ("test_shrink", "test shrink"),
    ):
        for finding in mined.get(key, [])[:limit]:
            signal_rows.append((label, finding))
    if signal_rows:
        table = Table(title="Agent failure signals", header_style="bold", expand=True)
        table.add_column("Signal")
        table.add_column("Repo")
        table.add_column("Target")
        table.add_column("Sessions", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Detail")
        for label, finding in signal_rows:
            table.add_row(
                label,
                escape(finding.repo),
                escape(finding.title[:100]),
                str(finding.sessions),
                f"{finding.tokens:,}",
                escape(finding.detail),
            )
        console.print(table)

    feedback = mined.get("feedback", [])[:limit]
    if feedback:
        table = Table(title="Repeated human steering", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Constraint")
        table.add_column("Mentions", justify="right")
        table.add_column("Sessions", justify="right")
        table.add_column("Example")
        for finding in feedback:
            table.add_row(
                escape(finding.repo),
                escape(finding.title),
                str(finding.tokens),
                str(finding.sessions),
                escape(finding.cause or finding.detail),
            )
        console.print(table)

    blasts = mined["context"][:limit]
    if blasts:
        table = Table(title="Context-blasting calls", header_style="bold", expand=True)
        table.add_column("Repo")
        table.add_column("Target")
        table.add_column("Sessions", justify="right")
        table.add_column("Total tokens", justify="right")
        table.add_column("Profile")
        table.add_column("Recommendation")
        for finding in blasts:
            table.add_row(
                escape(finding.repo),
                escape(finding.title),
                str(finding.sessions),
                f"{finding.tokens:,}",
                escape(finding.detail),
                escape(finding.recommendation),
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
            escape(finding.repo),
            str(finding.sessions),
            f"{finding.tokens:,}",
            escape(finding.sequence),
            escape("\n".join(finding.variants[:3])),
        )
    console.print(table)


def _mine_command(
    top: Annotated[int, typer.Option(help="Clusters / rows per detector to show.")] = 25,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    _ensure_local_sessions(db)
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        from .mining.core import session_summary
        from .mining.mine import mine as mine_friction

        _print_summary(session_summary(con))
        mined = mine_friction(con, top=top)
        _print_detector_tables(mined)
        global_findings = mined.get("global", [])[:10]
        if global_findings:
            table = Table(title="Cross-repo failure families", header_style="bold", expand=True)
            table.add_column("Failure")
            table.add_column("Repos", justify="right")
            table.add_column("Sessions", justify="right")
            table.add_column("Tokens", justify="right")
            for finding in global_findings:
                table.add_row(
                    escape(finding.title[:100]),
                    str(len(finding.variants)),
                    str(finding.sessions),
                    f"{finding.tokens:,}",
                )
            console.print(table)
        console.print(
            f"[dim]{len(mined['hotspots'])} hotspots, {len(mined['recovery'])} recovery loops, "
            f"{len(mined['context'])} context blasts, {len(mined.get('orientation', []))} orientation, "
            f"{len(mined.get('feedback', []))} repeated steering[/dim]"
        )
    finally:
        con.close()


app.command("mine")(_mine_command)


app.command("report")(_report_command)


app.command("run")(_run_command)


app.command("audit")(_audit_command)
app.command("holdout")(_holdout_command)


def _doctor_command(
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    checks: list[tuple[str, str, str]] = []

    py_ok = sys.version_info >= (3, 12)
    checks.append(("Python", "ok" if py_ok else "fail", platform.python_version()))

    try:
        package_version = version("trace2rules")
        checks.append(("trace2rules", "ok", package_version))
    except PackageNotFoundError:
        checks.append(("trace2rules", "warn", "not installed as a package"))

    node = shutil.which("node")
    if not node:
        checks.append(("Node.js", "warn", "not found; non-OpenCode local sources need it"))
    else:
        try:
            node_version = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            node_version = "unavailable"
        checks.append(("Node.js", "ok", node_version or node))

    for key in (
        "TRACE2RULES_LLM_API_KEY",
        "TRACE2RULES_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    ):
        checks.append(
            (key, "ok" if os.getenv(key) else "warn", "set" if os.getenv(key) else "unset")
        )

    from .ingest.sources.agent_trajectory import _source_roots

    try:
        roots = _source_roots(Path.home())
        detail = ", ".join(f"{name}:{path}" for name, path in roots) or "none found"
        checks.append(
            ("local sources", "ok" if roots else "warn", detail)
        )
    except OSError as error:
        checks.append(("local sources", "warn", str(error)))

    parent = Path(db).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        writable = os.access(parent, os.W_OK)
    except OSError:
        writable = False
    checks.append(
        ("database directory", "ok" if writable else "fail", str(parent))
    )
    if Path(db).exists():
        try:
            con = duckdb.connect(str(db), read_only=True)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT table_name FROM information_schema.tables"
                    ).fetchall()
                }
                columns = {
                    row[1]
                    for row in con.execute("PRAGMA table_info('tool_events')").fetchall()
                }
                if "tool_events" not in tables:
                    status, detail = "fail", "missing tool_events"
                elif "segment_idx" not in columns:
                    status, detail = "warn", "schema needs re-ingest/migration"
                else:
                    status, detail = "ok", "schema ready"
                checks.append(("database", status, detail))
            finally:
                con.close()
        except Exception as error:  # noqa: BLE001 - report lock/permission issues
            checks.append(("database", "warn", str(error)))
    else:
        checks.append(("database", "warn", f"not created yet: {db}"))

    table = Table(title="trace2rules doctor", header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for check, status, detail in checks:
        style = {"ok": "green", "warn": "yellow", "fail": "red"}.get(status, "")
        table.add_row(check, f"[{style}]{status}[/{style}]", escape(detail))
    console.print(table)

    if not py_ok or not writable:
        raise typer.Exit(1)


app.command("doctor")(_doctor_command)


def _patterns_command(
    top: Annotated[int, typer.Option(help="Patterns to show.")] = 25,
    min_sessions: Annotated[int, typer.Option(help="Minimum session support.")] = 3,
    max_len: Annotated[int, typer.Option(help="Maximum sequence length.")] = 5,
    max_gap: Annotated[int, typer.Option(help="Maximum skipped events between pattern steps.")] = 2,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        from .patterns import mine_attribute_patterns

        patterns = mine_attribute_patterns(
            con,
            min_sessions=min_sessions,
            max_len=max_len,
            max_gap=max_gap,
            top=top,
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
                escape(", ".join(pattern.repos)),
                escape(pattern.sequence),
                escape(pattern.example[:220]),
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
    per_repo: Annotated[int, typer.Option(help="Maximum sequences per repo; 0 disables balancing.")] = 3,
    include_ok: Annotated[bool, typer.Option("--include-ok", help="Include windows without an error.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        from .mining.sequences import exact_sequences

        findings = exact_sequences(
            con,
            length=length,
            min_sessions=min_sessions,
            require_error=not include_ok,
            min_instances=min_instances,
            min_rework=min_rework,
            min_lift=min_lift,
            retry_weight=retry_weight,
            top=None if per_repo else top,
        )
        if per_repo:
            counts: dict[str, int] = {}
            balanced = []
            for finding in findings:
                if counts.get(finding.repo, 0) >= per_repo:
                    continue
                counts[finding.repo] = counts.get(finding.repo, 0) + 1
                balanced.append(finding)
                if len(balanced) >= top:
                    break
            findings = balanced
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
    report: Annotated[
        Path,
        typer.Option(
            "--report",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Report prompt emitted by `trace2rules run` or `report --prompt`.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit the parsed synthesis as JSON.")] = False,
    max_rules: Annotated[
        int | None,
        typer.Option(
            "--max-rules",
            min=1,
            max=50,
            help="Maximum rules to keep; defaults to TRACE2RULES_MAX_RULES or 10.",
        ),
    ] = None,
) -> None:
    from .synthesis import synthesize_report
    from .synthesis.service import render

    try:
        synthesis = synthesize_report(report.read_text(encoding="utf-8"), max_rules=max_rules)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(1) from None
    if json_output:
        typer.echo(json.dumps(synthesis.model_dump(), indent=2))
        return
    typer.echo(render(synthesis, synthesis.repo, 0))


app.command("synthesize")(_synthesize_command)


if __name__ == "__main__":
    app()
