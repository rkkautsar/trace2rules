from __future__ import annotations

import re
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from .mining.models import Finding
from .synthesis_models import MAX_CHARS, MAX_LINES, MAX_RULES, EXACT_LENGTHS, Rule, Synthesis
from .synthesis_prompt import _client, _finding_confidence, _llm_synthesis, _make_client, _prompt
from .synthesis_selection import (
    GENERATED_VERIFIER_RE,
    _candidate_score,
    _generated_verifier,
    _is_subsequence,
    _is_path_target,
    _sequence_tokens,
    _truncate,
    _usable_exact,
    annotate_findings,
    label_summary,
    print_findings,
    select_findings,
)


from .synthesis_offline import (
    _allowed_sources,
    _attach_rule_provenance,
    _dedupe_templates,
    _evidence_quotes,
    _filter_junk_rules,
    _is_doc_target,
    _offline_synthesis,
    _source_refs,
    _template_id,
    _validate_sources,
)


def render(synthesis: Synthesis, repo: str, sessions: int) -> str:
    lines = []
    for rule in synthesis.rules:
        lines.append(f"## {rule.category}")
        lines.append(f"- **Rule:** {rule.rule_markdown}")
        lines.append(f"  **Evidence:** {rule.evidence_summary}")
        if rule.enforceable and rule.enforcement:
            lines.append(f"  **Enforcement:** {rule.enforcement}")
        if rule.support_sessions:
            lines.append(
                f"  **Support:** {rule.support_sessions} sessions, "
                f"{rule.support_instances} instances ({rule.confidence})"
            )
        if rule.finding_kinds:
            lines.append(f"  **Finding types:** {', '.join(rule.finding_kinds)}")
        for quote in rule.evidence_quotes:
            lines.append(f"  **Evidence quote:** {quote}")
        if rule.sources:
            lines.append(f"  **Sources:** {'; '.join(rule.sources)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def enforce_caps(
    synthesis: Synthesis, repo: str, sessions: int, max_rules: int = MAX_RULES
) -> tuple[Synthesis, str]:
    max_lines = max(MAX_LINES, max_rules * 10)
    max_chars = max(MAX_CHARS, max_rules * 500)
    synthesis = Synthesis(repo=synthesis.repo, rules=synthesis.rules[:max_rules])
    while True:
        text = render(synthesis, repo, sessions)
        if len(text) <= max_chars and len(text.splitlines()) <= max_lines:
            return synthesis, text
        if not synthesis.rules:
            return synthesis, text[:max_chars]
        synthesis = Synthesis(repo=synthesis.repo, rules=synthesis.rules[:-1])


def synthesize(
    con,
    repo: str | None = None,
    top: int = 25,
    offline: bool = False,
    max_rules: int = MAX_RULES,
    min_support: int = 3,
    console: Console | None = None,
    emit: bool = True,
    agents: str | Path | list[str | Path] | None = None,
) -> list[Synthesis]:
    from .mining.sequences import exact_sequences
    from .mining.mine import mine

    console = console or Console()
    repo_rows = _repos_with_sessions(con)
    if repo:
        repo_rows = [row for row in repo_rows if row["repo"] == repo]
        if not repo_rows:
            raise ValueError(f"repo not found in database: {repo}")

    exact: list[Finding] = []
    with console.status("[cyan]Mining exact sequences and friction clusters...[/cyan]"):
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
        mined = mine(con, top=None)
    results: list[Synthesis] = []

    for row in repo_rows:
        repo_name = row["repo"]
        findings = annotate_findings(
            con, select_findings(exact, mined, repo_name, min_sessions=min_support)
        )
        labels = label_summary(con, repo_name)
        print_findings(findings, console)
        if not findings:
            console.print(f"[yellow]No rules for {escape(repo_name)}; skipped.[/yellow]")
            continue

        synthesis: Synthesis | None = None
        llm_failed = False
        llm_used = False
        existing = None
        if agents:
            from .agents import audit_payload

            existing = audit_payload(con, repo_name, agents)
        if not offline:
            try:
                with console.status(
                    f"[cyan]Synthesizing {escape(repo_name)}...[/cyan]"
                ):
                    synthesis = _llm_synthesis(
                        findings, repo_name, labels, max_rules=max_rules, existing=existing
                    )
                    llm_used = synthesis is not None
            except Exception as error:  # noqa: BLE001 - degrade to deterministic output
                llm_failed = True
                console.print(f"[yellow]LLM synthesis unavailable: {escape(str(error))}[/yellow]")
        if synthesis is None:
            synthesis = _offline_synthesis(
                findings, repo_name, max_rules=max_rules, min_support=min_support
            )
            if offline:
                reason = "--offline"
            elif llm_failed:
                reason = "LLM call failed; using deterministic rules"
            else:
                reason = "no LLM credentials"
            console.print(f"[yellow]Deterministic rules for {escape(repo_name)} ({reason}).[/yellow]")

        if existing:
            from .reconcile import reconcile

            kept, actions = reconcile(synthesis.rules, existing.get("rules", []))
            for action in actions:
                if action.action in {"drop", "update"}:
                    console.print(
                        f"[dim]  {action.action}: {escape(action.reason)}[/dim]"
                    )
            synthesis = Synthesis(repo=synthesis.repo, rules=kept)

        synthesis = _validate_sources(
            synthesis, findings, require_sources=llm_used
        )
        synthesis = _attach_rule_provenance(synthesis, findings)
        synthesis = _filter_junk_rules(synthesis)
        synthesis, text = enforce_caps(
            synthesis, repo_name, row["sessions"], max_rules=max_rules
        )
        if not synthesis.rules:
            console.print(f"[yellow]No rules for {escape(repo_name)}; skipped.[/yellow]")
            continue
        if emit:
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
        results.append(synthesis)

    return results


def _repos_with_sessions(con) -> list[dict]:
    rows = con.execute("SELECT repo, count(*) FROM sessions GROUP BY repo ORDER BY repo").fetchall()
    return [{"repo": repo, "sessions": count} for repo, count in rows]
