from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from .detectors import Finding

MAX_RULES = 5
MAX_LINES = 60
MAX_CHARS = 2500
OUT_PATH = Path("AGENTS.proposed.md")

GENERATED_VERIFIER_RE = re.compile(
    r"final_|validation_test|test_fix_verification|test_reproduce_issue|comprehensive_test"
)


class Rule(BaseModel):
    category: Literal["test_command", "architecture_map", "context_hygiene", "convention"]
    rule_markdown: str
    evidence_summary: str
    estimated_tokens_saved_per_session: int = Field(ge=0)


class Synthesis(BaseModel):
    repo: str
    rules: list[Rule]


def _generated_verifier(sequence: str) -> bool:
    if GENERATED_VERIFIER_RE.search(sequence):
        return True
    for part in sequence.split(" | "):
        for field in part.split(":"):
            if field.endswith(".py") and "/" not in field and field.startswith("test_"):
                return True
    return False


def _usable_exact(finding: Finding) -> bool:
    if finding.kind != "exact":
        return True
    if _generated_verifier(finding.sequence):
        return False
    high_coverage_setup = finding.sessions >= 10 and finding.setup_tax > 0
    paired_rework = finding.paired_instances >= 2 and finding.delta_rework >= 1
    return high_coverage_setup or paired_rework


def select_findings(
    exact: list[Finding],
    mined: dict[str, list[Finding]],
    repo: str,
    limit: int = 20,
) -> list[Finding]:
    findings: list[Finding] = []
    exact_repo = [finding for finding in exact if finding.repo == repo]
    findings.extend(
        sorted(
            (finding for finding in exact_repo if _usable_exact(finding)),
            key=lambda finding: finding.cost,
            reverse=True,
        )[:10]
    )
    for key in ("hotspots", "context"):
        findings.extend(
            [finding for finding in mined[key] if finding.repo == repo][:2]
        )
    return findings[:limit]


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def print_findings(findings: list[Finding], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="Ranked friction findings", header_style="bold", expand=True)
    table.add_column("#", width=3)
    table.add_column("Kind", width=9)
    table.add_column("Repo", width=22)
    table.add_column("Sessions", justify="right", width=8)
    table.add_column("Cost", justify="right", width=10)
    table.add_column("Finding")
    for rank, finding in enumerate(findings, start=1):
        table.add_row(
            str(rank),
            finding.kind,
            finding.repo,
            str(finding.sessions),
            f"{finding.cost or finding.tokens:,}",
            _truncate(finding.title, 110),
        )
    console.print(table)


def _client() -> tuple[object, str] | None:
    key = os.getenv("TRACE2RULES_API_KEY")
    base = os.getenv("TRACE2RULES_BASE_URL")
    model = os.getenv("TRACE2RULES_MODEL")
    if key:
        return _make_client(key, base, model or "gemini-2.5-pro")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key:
        return _make_client(
            gemini_key,
            base or "https://generativelanguage.googleapis.com/v1beta/openai/",
            model or "gemini-2.5-pro",
        )
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return _make_client(openai_key, base, model or "gpt-5")
    return None


def _make_client(key: str, base: str | None, model: str):
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=base) if base else OpenAI(api_key=key)
    return client, model


def _prompt(findings: list[Finding], repo: str) -> str:
    payload = [
        {
            "kind": finding.kind,
            "sessions": finding.sessions,
            "cost": finding.cost or finding.tokens,
            "setup_tax": finding.setup_tax,
            "rework": finding.rework,
            "paired_instances": finding.paired_instances,
            "delta_rework": round(finding.delta_rework, 2),
            "detail": finding.detail,
            "sequence": finding.sequence,
            "variants": list(finding.variants),
        }
        for finding in findings
    ]
    return f"""Repo: {repo}

Findings from mined agent trajectories for this repo only (JSON):
{json.dumps(payload, indent=2)}

Write AGENTS.md rules for this repository. Requirements:
- Emit 1-5 declarative rules, not task-specific issue descriptions.
- Categories: test_command, architecture_map, context_hygiene, convention.
- rule_markdown: 1-2 imperative sentences, concrete command or file names.
- evidence_summary: sessions affected and approximate setup/rework token cost.
- No generic platitudes ("be careful", "read the docs").
- Ignore task-specific verifier scripts, one-off filenames, and normal edit -> test-fail loops.
- If any finding has `paired_instances >= 2` and `delta_rework >= 1`, include one `convention` rule: read the exception signature; if the same signature repeats after an edit, fix the product code or test scope instead of editing the harness.
- Total output must stay under 5 rules, 60 lines, and 2,500 characters.

Return JSON only, shaped exactly as:
{{"repo": "{repo}", "rules": [{{"category": "...", "rule_markdown": "...", "evidence_summary": "...", "estimated_tokens_saved_per_session": 0}}]}}"""


def _llm_synthesis(findings: list[Finding], repo: str) -> Synthesis | None:
    resolved = _client()
    if resolved is None:
        return None
    client, model = resolved
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "You convert observed coding-agent friction into terse, repo-level AGENTS.md rules.",
            },
            {"role": "user", "content": _prompt(findings, repo)},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return Synthesis.model_validate(json.loads(content))


def _offline_synthesis(findings: list[Finding], repo: str) -> Synthesis:
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, markdown: str, evidence: str, tokens: int) -> None:
        key = (category, markdown)
        if key in seen or len(rules) >= MAX_RULES:
            return
        seen.add(key)
        rules.append(
            Rule(
                category=category,
                rule_markdown=markdown,
                evidence_summary=evidence,
                estimated_tokens_saved_per_session=max(tokens, 0),
            )
        )

    for finding in findings:
        if finding.kind == "exact" and "pip:editable" in finding.sequence:
            if finding.sessions < 10:
                continue
            add(
                "test_command",
                "Run `pip install -e .` before import, CLI, or pytest probes; treat pre-install failures as setup failures, not code failures.",
                f"{finding.sessions} sessions; {finding.setup_tax:,} setup tokens.",
                finding.setup_tax // max(finding.sessions, 1),
            )
        elif (
            finding.kind == "exact"
            and finding.paired_instances >= 2
            and finding.delta_rework >= 1
        ):
            add(
                "convention",
                "After a command fails, read the exception signature. If the same signature repeats after an edit, change the product code or test scope instead of editing the harness.",
                f"Paired delta +{finding.delta_rework:.1f} rework in {finding.paired_instances} instances.",
                int(finding.delta_rework * 1000),
            )
        elif finding.kind == "hotspot":
            basename = finding.title.rstrip("/").rsplit("/", 1)[-1]
            if "." in basename:
                markdown = f"Open `{_truncate(finding.title, 70)}` early; it is a read-only reference file."
            else:
                markdown = f"Map `{_truncate(finding.title, 70)}` before editing; it is a read-only reference path."
            add(
                "architecture_map",
                markdown,
                f"Read-only in {finding.sessions} sessions; {finding.tokens:,} tokens spent.",
                finding.tokens // max(finding.sessions, 1),
            )
        elif finding.kind == "context":
            if " " not in finding.title:
                continue
            avg = re.search(r"avg (\d+) tokens", finding.detail)
            size = f"{int(avg.group(1)):,}" if avg else "large"
            add(
                "context_hygiene",
                f"Bound `{_truncate(finding.title, 70)}`; scope it to one file or test and avoid unbounded output.",
                f"Average {size} output tokens in {finding.sessions} sessions.",
                finding.tokens // max(finding.sessions, 1),
            )
        elif finding.kind == "recovery":
            continue

    return Synthesis(repo=repo, rules=rules[:MAX_RULES])


def render(synthesis: Synthesis, repo: str, sessions: int) -> str:
    lines = [
        "# AGENTS.md",
        "",
        f"Generated by trace2rules from {sessions} sessions in {repo}.",
        "",
    ]
    for rule in synthesis.rules:
        lines.append(f"## {rule.category}")
        lines.append(f"- **Rule:** {rule.rule_markdown}")
        lines.append(f"  **Evidence:** {rule.evidence_summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def enforce_caps(synthesis: Synthesis, repo: str, sessions: int) -> tuple[Synthesis, str]:
    synthesis = Synthesis(repo=synthesis.repo, rules=synthesis.rules[:MAX_RULES])
    while True:
        text = render(synthesis, repo, sessions)
        if len(text) <= MAX_CHARS and text.count("\n") <= MAX_LINES:
            return synthesis, text
        if not synthesis.rules:
            return synthesis, text[:MAX_CHARS]
        synthesis = Synthesis(repo=synthesis.repo, rules=synthesis.rules[:-1])


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def synthesize(
    con,
    out_path: str | Path | None = None,
    out_dir: str | Path = ".",
    repo: str | None = None,
    top: int = 25,
    offline: bool = False,
    console: Console | None = None,
) -> list[tuple[Path, Synthesis]]:
    from .cluster import exact_sequences, mine

    console = console or Console()
    repo_rows = _repos_with_sessions(con)
    if repo:
        repo_rows = [row for row in repo_rows if row["repo"] == repo]
        if not repo_rows:
            raise ValueError(f"repo not found in database: {repo}")

    exact = exact_sequences(
        con, length=3, min_sessions=3, top=500, min_instances=3, min_lift=1.5
    )
    mined = mine(con, top=top)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, Synthesis]] = []

    for row in repo_rows:
        repo_name = row["repo"]
        findings = select_findings(exact, mined, repo_name)
        print_findings(findings, console)

        synthesis: Synthesis | None = None
        if not offline:
            try:
                synthesis = _llm_synthesis(findings, repo_name)
            except Exception as error:  # noqa: BLE001 - degrade to deterministic output
                console.print(f"[yellow]LLM synthesis unavailable: {error}[/yellow]")
        if synthesis is None:
            synthesis = _offline_synthesis(findings, repo_name)
            console.print(
                f"[yellow]Deterministic rules for {repo_name} (no LLM credentials).[/yellow]"
            )

        synthesis, text = enforce_caps(synthesis, repo_name, row["sessions"])
        if not synthesis.rules:
            console.print(f"[yellow]No rules for {repo_name}; skipped.[/yellow]")
            continue
        if repo and out_path is not None:
            if str(out_path) == "-":
                sys.stdout.write(text)
                if not text.endswith("\n"):
                    sys.stdout.write("\n")
                results.append((Path("-"), synthesis))
                continue
            path = Path(out_path)
        else:
            path = out_dir / f"AGENTS.{_slug(repo_name)}.proposed.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        results.append((path, synthesis))

    return results


def _repos_with_sessions(con) -> list[dict]:
    rows = con.execute("SELECT repo, count(*) FROM sessions GROUP BY repo ORDER BY repo").fetchall()
    return [{"repo": repo, "sessions": count} for repo, count in rows]
