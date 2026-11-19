from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.markup import escape

from .detectors import Finding
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


DOC_TARGET_RE = re.compile(
    r"(?i)(^|/)(readme|docs?|changelog|contributing|license)(\.|/|$)"
)


def _is_doc_target(title: str) -> bool:
    return bool(DOC_TARGET_RE.search(title))


def _evidence_quotes(finding: Finding) -> list[str]:
    quotes: list[str] = []
    for event in finding.window:
        if event.get("outcome") not in {"err", "timeout"} and not event.get("recovered"):
            continue
        text = event.get("error_message") or ""
        text = " ".join(str(text).split())[:240]
        if text and text not in quotes:
            quotes.append(text)
    return quotes[:2]


def _source_refs(finding: Finding) -> list[str]:
    refs: list[str] = []
    for event in finding.window:
        behavior = finding.kind not in {"recovery", "context"}
        if (
            not behavior
            and event.get("outcome") not in ("err", "timeout")
            and not event.get("recovered")
        ):
            continue
        ref = event.get("session_ref")
        if ref and ref not in refs:
            refs.append(f"{ref} (step {event.get('step')})")
    return refs[:2]


def _offline_synthesis(
    findings: list[Finding], repo: str, max_rules: int = MAX_RULES, min_support: int = 3
) -> Synthesis:
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()
    def add(
        category: str,
        subject: str,
        markdown: str,
        evidence: str,
        tokens: int,
        sources: list[str] | None = None,
        finding: Finding | None = None,
    ) -> None:
        key = (category, subject)
        if key in seen:
            return
        seen.add(key)
        sessions = finding.sessions if finding else 0
        instances = finding.paired_instances if finding else 0
        confidence: Literal["low", "medium", "high"] = (
            "high" if sessions >= 10 and instances >= 2
            else "medium" if sessions >= 3
            else "low"
        )
        provenance = list(sources or [])
        if not provenance and finding is not None:
            provenance = [f"finding:{finding.kind}:{_truncate(finding.title, 80)}"]
        rules.append(
            Rule(
                category=category,
                rule_markdown=markdown,
                evidence_summary=evidence,
                estimated_tokens_saved_per_session=max(tokens, 0),
                sources=provenance[:2],
                support_sessions=sessions,
                support_instances=instances,
                confidence=confidence,
                finding_kinds=[finding.kind] if finding else [],
                evidence_quotes=_evidence_quotes(finding)[:2] if finding else [],
            )
        )

    def priority(finding: Finding) -> int:
        if finding.kind == "exact" and "pip:editable" in finding.sequence:
            return 0
        if finding.kind == "recovery":
            return 1
        if finding.kind == "orientation":
            return 2
        if finding.kind == "context":
            return 3
        if finding.kind == "hotspot":
            return 4
        return 5

    for finding in findings:
        if finding.sessions < min_support:
            continue
        if finding.intent and finding.intent_confidence < 0.4:
            continue

        if finding.kind == "exact" and "pip:editable" in finding.sequence:
            if finding.sessions < 10:
                continue
            add(
                "test_command",
                "editable-install",
                "Install the project and its test extras in development mode before import or test probes; treat pre-install failures as setup failures, not code failures.",
                f"{finding.sessions} sessions; {finding.setup_tax:,} setup tokens; {finding.tokens:,} window tokens.",
                finding.setup_tax // max(finding.sessions, 1),
                finding=finding,
            )

        elif finding.kind == "orientation":
            continue

        elif finding.kind == "recovery":
            recovery = finding.title.split("->", 1)[-1].strip()
            if INSTALL_RECOVERY_RE.search(recovery) and finding.sessions >= 10:
                add(
                    "test_command",
                    "editable-install",
                    "Install the project and its test extras in development mode before import or test probes; treat pre-install failures as setup failures, not code failures.",
                    f"{finding.detail}; {finding.tokens:,} tokens.",
                    finding.tokens // max(finding.sessions, 1),
                    finding=finding,
                )
            elif finding.cause and finding.sessions >= 2:
                cause = finding.cause.splitlines()[0].strip()
                cause = cause[:180]
                add(
                    "convention",
                    f"recovery:{cause[:60]}",
                    f"When `{cause}` recurs after an edit, inspect the failing call and fix the product path or test input; do not only rerun or edit the reproducer.",
                    f"{finding.sessions} sessions; {finding.detail}.",
                    finding.tokens // max(finding.sessions, 1),
                    sources=_source_refs(finding),
                    finding=finding,
                )

        elif finding.kind == "cluster":
            example = finding.variants[0] if finding.variants else ""
            if (
                not example
                or ":err" not in finding.sequence
                or not re.search(
                    r"(?i)(reproduce|repro|scratch|debug|verify)[\w.-]*\.py",
                    example,
                )
            ):
                continue
            add(
                "convention",
                f"sequence:{_truncate(example, 60)}",
                f"A scripted `{_truncate(example, 120)}` loop appears across {finding.sessions} sessions. Inspect the error before editing or re-running the script.",
                f"{finding.sessions} sessions; {finding.tokens:,} tokens.",
                finding.tokens // max(finding.sessions, 1),
                finding=finding,
            )

        elif finding.kind == "hotspot":
            if not _is_path_target(finding.title) or _is_doc_target(finding.title):
                continue
            if finding.edit_sessions > 0:
                # The path is edited somewhere in the repo; do not call it read-only.
                continue
            basename = finding.title.rstrip("/").rsplit("/", 1)[-1]
            if "." in basename:
                markdown = f"Open `{_truncate(finding.title, 70)}` early; it is a read-only reference file."
            else:
                markdown = f"Map `{_truncate(finding.title, 70)}` before editing; it is a read-only reference path."
            add(
                "architecture_map",
                finding.title,
                markdown,
                f"Read-only in {finding.sessions} sessions; {finding.tokens:,} tokens spent.",
                finding.tokens // max(finding.sessions, 1),
                sources=_source_refs(finding),
                finding=finding,
            )

        elif finding.kind in {
            "selector_miss",
            "workaround_script",
            "stuck_loop",
            "noop_retry",
            "premature_completion",
            "low_validation",
            "site_edit_mismatch",
            "permission_denied",
            "reread",
            "edit_churn",
            "command_error",
        }:
            add(
                "convention",
                f"{finding.kind}:{finding.title}",
                finding.recommendation,
                finding.detail,
                finding.tokens // max(finding.sessions, 1),
                sources=_source_refs(finding),
                finding=finding,
            )

        elif finding.kind == "context":
            if finding.intent == "other":
                continue
            if _is_doc_target(finding.title):
                continue
            if not finding.recommendation:
                continue
            category = "context_hygiene" if finding.side != "repo" else "architecture_map"
            add(
                category,
                finding.title,
                finding.recommendation,
                finding.detail,
                finding.tokens // max(finding.sessions, 1),
                sources=_source_refs(finding),
                finding=finding,
            )

    return Synthesis(repo=repo, rules=rules)


PLACEHOLDER_RE = re.compile(r"<(?:id|n|path|workspace)>|\{\{")


def _allowed_sources(findings: list[Finding]) -> set[str]:
    allowed: set[str] = set()
    for finding in findings:
        for event in finding.window:
            ref = event.get("session_ref")
            step = event.get("step")
            if ref and step is not None:
                allowed.add(f"{ref} (step {step})")
    return allowed


def _validate_sources(
    synthesis: Synthesis,
    findings: list[Finding],
    *,
    require_sources: bool = False,
) -> Synthesis:
    allowed = _allowed_sources(findings)
    rules = []
    for rule in synthesis.rules:
        if rule.sources:
            valid = [
                source for source in rule.sources
                if source in allowed or source.startswith("finding:")
            ]
            if len(valid) != len(rule.sources):
                continue
        elif require_sources:
            continue
        rules.append(rule)
    return Synthesis(repo=synthesis.repo, rules=rules)


def _attach_rule_provenance(synthesis: Synthesis, findings: list[Finding]) -> Synthesis:
    by_ref: dict[str, Finding] = {}
    for finding in findings:
        for event in finding.window:
            ref = event.get("session_ref")
            if ref:
                by_ref[ref] = finding
    rules = []
    for rule in synthesis.rules:
        matched = [
            by_ref[ref.split(" (step ", 1)[0]]
            for ref in rule.sources
            if ref.split(" (step ", 1)[0] in by_ref
        ]
        if not matched:
            rules.append(rule)
            continue
        sessions = max(finding.sessions for finding in matched)
        instances = max((finding.paired_instances for finding in matched), default=0)
        confidence: Literal["low", "medium", "high"] = (
            "high" if sessions >= 10 and instances >= 2
            else "medium" if sessions >= 3
            else "low"
        )
        rules.append(
            rule.model_copy(
                update={
                    "support_sessions": sessions,
                    "support_instances": instances,
                    "confidence": confidence,
                    "finding_kinds": sorted({finding.kind for finding in matched}),
                    "evidence_quotes": list(dict.fromkeys(
                        quote
                        for finding in matched
                        for quote in _evidence_quotes(finding)
                    ))[:2],
                }
            )
        )
    return Synthesis(repo=synthesis.repo, rules=rules)


def _template_id(rule: Rule) -> str:
    text = re.sub(r"`[^`]*`", "`<x>`", rule.rule_markdown.lower())
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"{rule.category}:{text[:180]}"


def _dedupe_templates(rules: list[Rule]) -> list[Rule]:
    seen: set[str] = set()
    unique: list[Rule] = []
    for rule in rules:
        template = _template_id(rule)
        if template in seen:
            continue
        seen.add(template)
        unique.append(rule)
    return unique


def _filter_junk_rules(synthesis: Synthesis) -> Synthesis:
    rules = [
        rule
        for rule in synthesis.rules
        if not PLACEHOLDER_RE.search(rule.rule_markdown)
        and not PLACEHOLDER_RE.search(rule.evidence_summary)
    ]
    return Synthesis(repo=synthesis.repo, rules=_dedupe_templates(rules))


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
    from .cluster import exact_sequences, mine

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
