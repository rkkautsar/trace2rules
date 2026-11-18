from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .detectors import Finding
from .redact import redact_value

MAX_RULES = 5
MAX_LINES = 60
MAX_CHARS = 2500
EXACT_LENGTHS = (3, 4, 5)

GENERATED_VERIFIER_RE = re.compile(
    r"final_|validation_test|test_fix_verification|test_reproduce_issue|comprehensive_test|"
    r"(?:reproduce|repro|scratch|debug|verif)[\w.-]*\.(?:py|ts|js|go|rs|java|rb|php|sh|c|cpp)"
)


class Rule(BaseModel):
    category: Literal["test_command", "architecture_map", "context_hygiene", "convention"]
    rule_markdown: str
    evidence_summary: str
    estimated_tokens_saved_per_session: int = Field(ge=0)
    sources: list[str] = Field(default_factory=list)
    enforceable: bool = False
    enforcement: str = ""


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
    high_coverage_setup = (
        finding.sessions >= 10 and finding.setup_tax > 0 and finding.delta_rework >= 0
    )
    paired_rework = finding.paired_instances >= 2 and finding.delta_rework >= 1
    return high_coverage_setup or paired_rework


def _sequence_tokens(finding: Finding) -> tuple[str, ...]:
    return tuple(
        part.split(":", 1)[1]
        for part in re.split(r" \| (?=s\d+:)", finding.sequence)
        if ":" in part
    )


def _is_subsequence(inner: tuple[str, ...], outer: tuple[str, ...]) -> bool:
    if not inner or len(inner) > len(outer):
        return False
    return any(outer[index : index + len(inner)] == inner for index in range(len(outer) - len(inner) + 1))


def _candidate_score(finding: Finding) -> float:
    weights = {
        "selector_miss": 1.6,
        "workaround_script": 1.7,
        "stuck_loop": 1.4,
        "premature_completion": 1.8,
        "low_validation": 1.6,
        "noop_retry": 1.2,
        "recovery": 1.0,
        "context": 0.7,
        "hotspot": 0.8,
        "site_edit_mismatch": 1.1,
    }
    support = max(finding.sessions, 1)
    impact = max(finding.tokens, finding.cost, 1)
    return support * math.log1p(impact) * weights.get(finding.kind, 0.8)


def select_findings(
    exact: list[Finding],
    mined: dict[str, list[Finding]],
    repo: str,
    limit: int = 20,
) -> list[Finding]:
    candidates: list[Finding] = []
    for key in (
        "hotspots",
        "recovery",
        "selector_miss",
        "workaround",
        "stuck_loop",
        "context",
        "noop_retry",
        "premature_completion",
        "low_validation",
        "site_edit_mismatch",
    ):
        candidates.extend(
            finding
            for finding in mined.get(key, [])
            if finding.repo == repo
            and finding.sessions >= 3
            and not GENERATED_VERIFIER_RE.search(finding.title)
            and (finding.recommendation or finding.kind != "context")
            and (
                finding.kind != "hotspot"
                or (_is_path_target(finding.title) and finding.sessions >= 5)
            )
        )
    candidates.sort(key=_candidate_score, reverse=True)
    selected: list[Finding] = []
    per_kind: dict[str, int] = {}
    seen_titles: set[tuple[str, str]] = set()
    validation_family = {"premature_completion", "low_validation"}
    family_selected = 0
    for finding in candidates:
        key = (finding.kind, finding.title)
        if key in seen_titles or per_kind.get(finding.kind, 0) >= 2:
            continue
        if finding.kind in validation_family:
            if family_selected:
                continue
            family_selected += 1
        seen_titles.add(key)
        per_kind[finding.kind] = per_kind.get(finding.kind, 0) + 1
        selected.append(finding)
        if len(selected) >= limit:
            break

    exact_repo = [finding for finding in exact if finding.repo == repo]
    selected_tokens: list[tuple[str, ...]] = []
    for finding in sorted(
        (finding for finding in exact_repo if _usable_exact(finding)),
        key=lambda finding: (len(_sequence_tokens(finding)), finding.cost),
        reverse=True,
    ):
        tokens = _sequence_tokens(finding)
        if any(
            _is_subsequence(tokens, seen) or _is_subsequence(seen, tokens)
            for seen in selected_tokens
        ):
            continue
        selected_tokens.append(tokens)
        selected.append(finding)
        if len(selected) >= limit:
            break
    return selected[:limit]


def annotate_findings(con, findings: list[Finding]) -> list[Finding]:
    """Attach labels by full event key, falling back to the finding title."""
    targets = sorted(
        {finding.title for finding in findings if finding.title}
        | {
            target
            for finding in findings
            for _tool, _action, target in finding.event_keys
        }
    )
    if not targets:
        return findings
    placeholders = ",".join("?" for _ in targets)
    rows = con.execute(
        f"""
        SELECT tool_name, action_type, target,
               intent, object_type, intent_confidence, object_confidence
        FROM target_labels
        WHERE target IN ({placeholders})
        """,
        targets,
    ).fetchall()

    grouped: dict[tuple[str, str, str], list[tuple[str, str, float, float]]] = defaultdict(list)
    for tool_name, action_type, target, intent, object_type, intent_confidence, object_confidence in rows:
        grouped[(tool_name, action_type, target)].append(
            (intent, object_type, float(intent_confidence), float(object_confidence))
        )

    labels: dict[tuple[str, str, str], tuple[str, str, float, float]] = {}
    for key, entries in grouped.items():
        intents = {entry[0] for entry in entries}
        objects = {entry[1] for entry in entries}
        if len(intents) != 1 or len(objects) != 1:
            continue
        intent, object_type, intent_confidence, object_confidence = max(
            entries, key=lambda entry: (entry[2] + entry[3], entry[3], entry[2])
        )
        if intent_confidence < 0.4 or object_confidence < 0.6:
            continue
        labels[key] = (intent, object_type, intent_confidence, object_confidence)

    title_labels = {
        key[2]: value for key, value in labels.items() if key[0] and key[1]
    }
    annotated = []
    for finding in findings:
        candidates = [
            labels[key] for key in finding.event_keys if key in labels
        ]
        label = (
            max(candidates, key=lambda item: (item[2] + item[3], item[3], item[2]))
            if candidates
            else title_labels.get(finding.title)
        )
        if label is None:
            annotated.append(finding)
            continue
        intent, object_type, intent_confidence, object_confidence = label
        annotated.append(
            replace(
                finding,
                intent=intent,
                object_type=object_type,
                intent_confidence=float(intent_confidence),
                object_confidence=float(object_confidence),
            )
        )
    return annotated


def label_summary(con, repo: str, limit: int = 12) -> list[dict]:
    rows = con.execute(
        """
        SELECT l.intent, l.object_type,
               count(*) AS calls,
               count(DISTINCT e.session_id) AS sessions,
               avg(l.intent_confidence) AS intent_confidence,
               avg(l.object_confidence) AS object_confidence
        FROM tool_events e
        JOIN target_labels l
          ON e.tool_name = l.tool_name
         AND e.action_type = l.action_type
         AND e.target = l.target
        WHERE e.repo = ?
        GROUP BY 1, 2
        HAVING avg(l.intent_confidence) >= 0.4
           AND avg(l.object_confidence) >= 0.6
        ORDER BY calls DESC
        LIMIT ?
        """,
        [repo, limit],
    ).fetchall()
    return [
        {
            "intent": intent,
            "object_type": object_type,
            "calls": calls,
            "sessions": sessions,
            "intent_confidence": round(float(intent_confidence or 0), 2),
            "object_confidence": round(float(object_confidence or 0), 2),
        }
        for intent, object_type, calls, sessions, intent_confidence, object_confidence in rows
    ]


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
            escape(finding.kind),
            escape(finding.repo),
            str(finding.sessions),
            f"{finding.cost or finding.tokens:,}",
            escape(_truncate(finding.title, 110)),
        )
    console.print(table)


def _client() -> tuple[object, str] | None:
    key = os.getenv("TRACE2RULES_LLM_API_KEY") or os.getenv("TRACE2RULES_API_KEY")
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


def _prompt(
    findings: list[Finding],
    repo: str,
    labels: list[dict],
    max_rules: int = MAX_RULES,
    context: dict | None = None,
    existing: dict | None = None,
) -> str:
    payload = redact_value(
        [
        {
            "kind": finding.kind,
            "sessions": finding.sessions,
            "cost": finding.cost or finding.tokens,
            "setup_tax": finding.setup_tax,
            "rework": finding.rework,
            "paired_instances": finding.paired_instances,
            "delta_rework": round(finding.delta_rework, 2),
            "title": finding.title,
            "failure_family": finding.failure_family,
            "detail": finding.detail,
            "sequence": finding.sequence,
            "variants": list(finding.variants),
            "intent": finding.intent,
            "object_type": finding.object_type,
            "cause": finding.cause,
            "cause_support_sessions": finding.cause_support_sessions,
            "side": finding.side,
            "window": list(finding.window),
        }
        for index, finding in enumerate(findings)
        ]
    )
    return f"""Repo: {repo}

Findings from mined agent trajectories for this repo only (JSON):
{json.dumps(payload, indent=2)}

Semantic target labels observed for this repo (clef/decision-model annotations, JSON):
{json.dumps(redact_value(labels), indent=2)}

Command/bootstrap evidence (JSON):
{json.dumps(redact_value(context or {}), indent=2)}

Existing AGENTS.md rules and audit (JSON):
{json.dumps(redact_value(existing or {}), indent=2)}

Write AGENTS.md rules for this repository. Requirements:
- Emit 0-{max_rules} declarative rules. Do not fill the quota: fewer rules are better, and zero is valid when the evidence does not support a rule. Do not emit task-specific issue descriptions.
- Categories: test_command, architecture_map, context_hygiene, convention.
- Do not repeat an existing AGENTS.md rule. If an existing rule is violated or unenforced, propose the concrete enforcement (CI/pre-commit/lint) instead.
- Set `enforceable` to true when a rule can be checked mechanically. Put the exact CI/pre-commit/lint check in `enforcement`; do not leave an enforceable rule as a soft instruction.
- rule_markdown: 1-2 imperative sentences, concrete command or file names.
- evidence_summary: sessions affected and approximate setup/rework token cost.
- No generic platitudes ("be careful", "read the docs").
- Ignore task-specific verifier scripts, one-off filenames, and normal edit -> test-fail loops.
- For `recovery` findings, use the representative window from failure through success to explain the failed -> successful transition; cite the support count. Do not infer intent not shown by the tool calls.
- A window is one illustrative occurrence, not proof of intent. Never encode an identifier that appears in fewer than two sessions.
- Do not use resolution labels or infer success from them. Use `validated_sessions` and `unvalidated_sessions` only.
- The `assistant` field is the agent's reasoning, not observed evidence. Use it to explain a sequence, but never cite it as a source or count it as a finding.
- For `orientation` or `side=repo`, write a repository-side recommendation, not an agent rule.
- Do not mechanically recommend `| head`; distinguish quiet flags, targeted test selection, and repo entrypoints.
- Total output must stay under {max_rules} rules, {max(MAX_LINES, max_rules * 10)} lines, and {max(MAX_CHARS, max_rules * 500):,} characters.

Return JSON only, shaped exactly as:
{{"repo": "{repo}", "rules": [{{"category": "...", "rule_markdown": "...", "evidence_summary": "...", "estimated_tokens_saved_per_session": 0, "sources": ["<session_ref> (step N)"], "enforceable": true, "enforcement": "<CI/pre-commit/lint check>"}}]}}
- Include at least one source copied exactly from a finding window; never invent a source."""


def _llm_synthesis(
    findings: list[Finding], repo: str, labels: list[dict], max_rules: int = MAX_RULES
) -> Synthesis | None:
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
            {"role": "user", "content": _prompt(findings, repo, labels, max_rules)},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return Synthesis.model_validate(json.loads(content))


def _is_path_target(title: str) -> bool:
    return bool(title) and " " not in title and ("/" in title or "." in title)


DOC_TARGET_RE = re.compile(
    r"(?i)(^|/)(readme|docs?|changelog|contributing|license)(\.|/|$)"
)
INSTALL_RECOVERY_RE = re.compile(
    r"(?i)\b(pip|pip3|uv|poetry|pipenv|bundle|composer|npm|yarn|pnpm|cargo|go|dotnet|mix)\b.*"
    r"\b(install|sync|add|get|ci|restore|deps\.get)\b"
)


def _is_doc_target(title: str) -> bool:
    return bool(DOC_TARGET_RE.search(title))


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
    findings: list[Finding], repo: str, max_rules: int = MAX_RULES
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
    ) -> None:
        key = (category, subject)
        if key in seen:
            return
        seen.add(key)
        rules.append(
            Rule(
                category=category,
                rule_markdown=markdown,
                evidence_summary=evidence,
                estimated_tokens_saved_per_session=max(tokens, 0),
                sources=(sources or [])[:2],
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
        if finding.sessions < 3:
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
            )

        elif finding.kind in {
            "selector_miss",
            "workaround_script",
            "stuck_loop",
            "noop_retry",
            "premature_completion",
            "low_validation",
            "site_edit_mismatch",
        }:
            add(
                "convention",
                f"{finding.kind}:{finding.title}",
                finding.recommendation,
                finding.detail,
                finding.tokens // max(finding.sessions, 1),
                sources=_source_refs(finding),
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


def _validate_sources(synthesis: Synthesis, findings: list[Finding]) -> Synthesis:
    allowed = _allowed_sources(findings)
    rules = []
    for rule in synthesis.rules:
        if rule.sources:
            valid = [source for source in rule.sources if source in allowed]
            if len(valid) != len(rule.sources):
                continue
        rules.append(rule)
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
    console: Console | None = None,
    emit: bool = True,
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
                    min_sessions=3,
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
            con, select_findings(exact, mined, repo_name)
        )
        labels = label_summary(con, repo_name)
        print_findings(findings, console)
        if not findings:
            console.print(f"[yellow]No rules for {escape(repo_name)}; skipped.[/yellow]")
            continue

        synthesis: Synthesis | None = None
        llm_failed = False
        if not offline:
            try:
                with console.status(
                    f"[cyan]Synthesizing {escape(repo_name)}...[/cyan]"
                ):
                    synthesis = _llm_synthesis(
                        findings, repo_name, labels, max_rules=max_rules
                    )
            except Exception as error:  # noqa: BLE001 - degrade to deterministic output
                llm_failed = True
                console.print(f"[yellow]LLM synthesis unavailable: {escape(str(error))}[/yellow]")
        if synthesis is None:
            synthesis = _offline_synthesis(findings, repo_name, max_rules=max_rules)
            if offline:
                reason = "--offline"
            elif llm_failed:
                reason = "LLM call failed; using deterministic rules"
            else:
                reason = "no LLM credentials"
            console.print(f"[yellow]Deterministic rules for {escape(repo_name)} ({reason}).[/yellow]")

        synthesis = _validate_sources(synthesis, findings)
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
