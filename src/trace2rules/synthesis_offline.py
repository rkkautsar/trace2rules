from __future__ import annotations

import re
from typing import Literal

from .detectors import Finding
from .synthesis_models import MAX_RULES, Rule, Synthesis
from .synthesis_selection import _is_path_target, _truncate

INSTALL_RECOVERY_RE = re.compile(
    r"(?i)\b(pip|pip3|uv|poetry|pipenv|bundle|composer|npm|yarn|pnpm|cargo|go|dotnet|mix)\b.*"
    r"\b(install|sync|add|get|ci|restore|deps\.get)\b"
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


