from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import duckdb
import sys

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .agents import audit_commit, audit_payload, latest_agents_commit, resolve_agents
from .db import DB_PATH, connect
from .ingest import DEFAULT_REPOS, SOURCE, ingest as ingest_traces
from .labels import label_targets
from .patterns import mine_sequences

load_dotenv(Path.cwd() / ".env")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Mine coding-agent trajectories for AGENTS.md rules.")
console = Console()


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
    label: Annotated[
        bool,
        typer.Option(
            "--label",
            help="After ingest, label targets if a decision backend is available.",
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
        from .sources.swe_chat import ingest_swe_chat

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
        from .sources.agent_trajectory import ingest_agent_sessions

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
    if label:
        from .labels import detect_model

        model = detect_model()
        if model is None:
            console.print(
                "[yellow]No decision backend detected; skipping labeling.[/yellow]"
            )
        else:
            try:
                done, failed = label_targets(db, model=model)
                console.print(
                    f"Labeled [bold]{done}[/bold] targets with [bold]{escape(model)}[/bold]"
                    f" ({failed} failed)."
                )
            except RuntimeError as error:
                console.print(f"[yellow]Labeling skipped: {escape(str(error))}[/yellow]")
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
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        from .detectors import session_summary
        from .cluster import mine as mine_friction

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
            f"{len(mined['context'])} context blasts, {len(mined.get('orientation', []))} orientation[/dim]"
        )
    finally:
        con.close()


app.command("mine")(_mine_command)


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
    if len(labels) == 1:
        return labels[0]
    raise typer.BadParameter(
        "Could not infer repository label; pass --repo. Known labels: "
        + ", ".join(labels[:12])
    )


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
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
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

        from .cluster import exact_sequences, mine as mine_friction
        from .synthesize import EXACT_LENGTHS, _prompt, annotate_findings, label_summary, select_findings

        mined = mine_friction(con, top=None if (repo or prompt) else top)
        findings = [
            finding
            for key in ("hotspots", "recovery", "context", "orientation", "selector_miss", "workaround", "stuck_loop", "noop_retry", "premature_completion", "low_validation", "site_edit_mismatch")
            for finding in mined[key]
        ]
        exact = []
        for length in EXACT_LENGTHS:
            exact.extend(
                exact_sequences(
                    con,
                    length=length,
                    min_sessions=3,
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
            from .detectors import prompt_context

            agent_paths = resolve_agents(agents)
            chunks: list[str] = []
            for repo_name, _count in repo_rows:
                selected_findings = annotate_findings(
                    con, select_findings(exact, mined, repo_name)
                )
                existing = (
                    audit_payload(con, repo_name, agent_paths)
                    if agent_paths
                    and (repo is not None or repo_name == Path.cwd().name)
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
            sys.stdout.write("\n\n---\n\n".join(chunks))
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


app.command("report")(_report_command)


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
        from .agents import audit_prompt

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
            for column in ("Rule", "Exercised", "Applicable", "Violations"):
                table.add_column(column)
            for rule in payload[key]:
                table.add_row(
                    rule["text"][:90],
                    str(rule["exercised_sessions"]),
                    str(rule["applicable_sessions"]),
                    str(rule["violation_sessions"]),
                )
            console.print(table)
        return
    table = Table(title=f"AGENTS.md audit: {repo}")
    for column in (
        "Rule", "Category", "Observed", "Applicable", "Compliant",
        "Violations", "Not observed", "Existing enforcement", "Status",
    ):
        table.add_column(column)
    for rule in payload["rules"]:
        table.add_row(
            rule["text"][:90],
            rule["category"],
            str(rule["exercised_sessions"]),
            str(rule["applicable_sessions"]),
            str(rule["compliant_sessions"]),
            str(rule["violation_sessions"]),
            str(rule["not_observed_sessions"]),
            rule.get("existing_enforcement", "") or "-",
            rule.get("enforcement_status", "unknown"),
        )
    console.print(table)
    if payload.get("warning"):
        console.print(f"[yellow]{payload['warning']}[/yellow]")
    for rule in payload["rules"]:
        console.print(f"[dim]{rule['suggestion']}[/dim]")


app.command("audit")(_audit_command)


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
        except OSError:
            node_version = "unavailable"
        checks.append(("Node.js", "ok", node_version or node))

    try:
        from .labels import _wrangler_ready, detect_model

        wrangler_ready = _wrangler_ready()
        checks.append(
            (
                "wrangler",
                "ok" if wrangler_ready else "warn",
                "authenticated" if wrangler_ready else "not authenticated",
            )
        )
        decision_model = detect_model()
        checks.append(
            (
                "decision backend",
                "ok" if decision_model else "warn",
                decision_model or "none detected",
            )
        )
    except Exception as error:  # noqa: BLE001 - doctor must not fail on probes
        checks.append(("decision backend", "warn", str(error)))

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

    from .sources.agent_trajectory import _source_roots

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


def _label_command(
    limit: Annotated[int, typer.Option(help="Max pending targets; 0 = all.")] = 0,
    rpm: Annotated[int, typer.Option(help="Requests per minute cap.")] = 270,
    workers: Annotated[int, typer.Option(help="Concurrent requests.")] = 6,
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help="clef-flash, clef, jev, gemini-decision-1, or gpt-6-luna; auto-detected if omitted.",
        ),
    ] = "",
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    try:
        done, failed = label_targets(
            db,
            limit=limit or None,
            rpm=rpm,
            workers=workers,
            model=model or os.getenv("TRACE2RULES_DECISION_MODEL"),
        )
    except RuntimeError as error:
        console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(1) from None
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
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
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
    include_ok: Annotated[bool, typer.Option("--include-ok", help="Include windows without an error.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    con = connect(db)
    try:
        from .cluster import exact_sequences

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
    repo: Annotated[str | None, typer.Option("--repo", help="Synthesize only this repo.")] = None,
    top: Annotated[int, typer.Option(help="Findings to pass to synthesis.")] = 25,
    agents: Annotated[
        Path | None,
        typer.Option("--agents", help="Existing AGENTS.md to audit and avoid duplicating."),
    ] = None,
    max_rules: Annotated[
        int,
        typer.Option("--max-rules", min=1, max=20, help="Maximum rules per repo."),
    ] = 5,
    offline: Annotated[bool, typer.Option("--offline", help="Skip the LLM and use deterministic rules.")] = False,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    cmd_console = Console(stderr=True)
    con = connect(db)
    try:
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if session_count == 0:
            cmd_console.print("[red]No sessions in the database. Run `trace2rules ingest` first.[/red]")
            raise typer.Exit(1)
        try:
            from .synthesize import synthesize as synthesize_rules

            results = synthesize_rules(
                con,
                repo=repo,
                top=top,
                max_rules=max_rules,
                offline=offline,
                console=cmd_console,
                agents=(
                    resolve_agents(agents)
                    if agents
                    else resolve_agents()
                    if repo
                    else None
                ),
            )
        except ValueError as error:
            cmd_console.print(f"[red]{escape(str(error))}[/red]")
            raise typer.Exit(1) from None
        for synthesis in results:
            cmd_console.print(
                f"[dim]{len(synthesis.rules)} rules for {synthesis.repo}.[/dim]"
            )
    finally:
        con.close()


def _eval_command(
    repo: Annotated[str | None, typer.Option("--repo", help="Evaluate one repo.")] = None,
    max_rules: Annotated[int, typer.Option("--max-rules", min=1, max=20)] = 5,
    offline: Annotated[bool, typer.Option("--offline/--llm", help="Use deterministic rules.")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    golden: Annotated[Path | None, typer.Option("--golden", help="Golden rule set JSON.")] = None,
    db: Annotated[Path, typer.Option(help="DuckDB database path.")] = DB_PATH,
) -> None:
    from .evaluate import evaluate_synthesis, load_golden
    from .synthesize import synthesize as synthesize_rules

    con = connect(db)
    try:
        results = synthesize_rules(
            con,
            repo=repo,
            max_rules=max_rules,
            offline=offline,
            console=Console(stderr=True),
            emit=False,
        )
    finally:
        con.close()
    specs = load_golden(golden) if golden else None
    metrics = [evaluate_synthesis(synthesis, specs) for synthesis in results]
    if json_output:
        console.print_json(data={"metrics": metrics})
        return
    table = Table(title="Synthesis quality")
    table.add_column("Repo")
    table.add_column("Rules", justify="right")
    table.add_column("Placeholders", justify="right")
    table.add_column("Redundancy", justify="right")
    table.add_column("Citations", justify="right")
    table.add_column("Precision@5", justify="right")
    for row in metrics:
        table.add_row(
            escape(str(row["repo"])),
            str(row["rules"]),
            f"{row['placeholder_rate']:.0%}",
            f"{row['template_redundancy']:.0%}",
            f"{row['citation_rate']:.0%}",
            f"{row.get('precision_at_5', 0):.0%}",
        )
    console.print(table)


app.command("eval")(_eval_command)
app.command("synthesize")(_synthesize_command)


if __name__ == "__main__":
    app()
