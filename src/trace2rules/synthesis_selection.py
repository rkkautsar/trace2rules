from __future__ import annotations

import math
import re
from collections import defaultdict

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .mining.models import Finding

GENERATED_VERIFIER_RE = re.compile(
    r"final_|validation_test|test_fix_verification|test_reproduce_issue|comprehensive_test|"
    r"(?:reproduce|repro|scratch|debug|verif)[\w.-]*\.(?:py|ts|js|go|rs|java|rb|php|sh|c|cpp)"
)


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
        "feedback_loop": 1.3,
        "permission_denied": 1.5,
        "reread": 1.1,
        "edit_churn": 1.2,
        "command_error": 1.4,
        "failure_loop": 1.6,
        "edit_rejection": 1.7,
        "blind_edit": 1.2,
        "environment_thrash": 0.7,
        "destructive_command": 2.0,
        "no_reproduction": 1.3,
        "false_completion": 1.8,
    }
    support = max(finding.sessions, 1)
    impact = max(finding.tokens, finding.cost, 1)
    return support * math.log1p(impact) * weights.get(finding.kind, 0.8)


def select_findings(
    exact: list[Finding],
    mined: dict[str, list[Finding]],
    repo: str,
    limit: int = 20,
    min_sessions: int = 3,
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
        "feedback",
        "permission",
        "reread",
        "edit_churn",
        "command_error",
        "failure_loop",
        "edit_rejection",
        "blind_edit",
        "environment_thrash",
        "destructive_command",
        "no_reproduction",
        "false_completion",
    ):
        candidates.extend(
            finding
            for finding in mined.get(key, [])
            if finding.repo == repo
            and (
                finding.sessions >= min_sessions
                or finding.kind in {"feedback_loop", "destructive_command"}
            )
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
        candidates = [labels[key] for key in finding.event_keys if key in labels]
        label = (
            max(candidates, key=lambda item: (item[2] + item[3], item[3], item[2]))
            if candidates
            else title_labels.get(finding.title)
        )
        if label is None:
            annotated.append(finding)
            continue
        intent, object_type, intent_confidence, object_confidence = label
        from dataclasses import replace

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


def _is_path_target(title: str) -> bool:
    return bool(title) and " " not in title and ("/" in title or "." in title)
