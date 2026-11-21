from __future__ import annotations

import math
import re

from ..mining.models import Finding

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
        "prevention_delta": 2.2,
        "error_transition": 1.4,
        "persistent_site": 1.5,
        "read_recipe": 1.2,
        "selector_effectiveness": 1.5,
        "diagnostic_read": 1.7,
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
        "generated_edit": 1.8,
        "dependency_drift": 1.4,
        "blind_edit": 1.2,
        "environment_thrash": 0.7,
        "destructive_command": 2.0,
        "no_reproduction": 1.3,
        "false_completion": 1.8,
        "correction_loop": 1.8,
        "test_regression": 1.9,
        "test_shrink": 1.9,
    }
    support = max(finding.sessions, 1)
    impact = max(finding.tokens, finding.cost, 1)
    score = support * math.log1p(impact) * weights.get(finding.kind, 0.8)
    if finding.kind in {"attribute_pattern", "prevention_delta"}:
        score *= max(1.0, min(finding.lift, 10.0))
    return score


def _action_key(finding: Finding) -> tuple[str, str, str] | None:
    if finding.kind == "prevention_delta":
        delta = finding.title.split(" before ", 1)[0].strip().lower()
        return finding.repo, "setup", delta
    if finding.kind == "environment_thrash":
        return finding.repo, "setup", "*"
    return None


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
        "generated_edit",
        "dependency_drift",
        "blind_edit",
        "environment_thrash",
        "destructive_command",
        "no_reproduction",
        "false_completion",
        "correction_loop",
        "test_regression",
        "test_shrink",
        "prevention_delta",
        "error_transition",
        "persistent_site",
        "read_recipe",
        "selector_effectiveness",
        "diagnostic_read",
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
    specific_setup = {
        finding.repo for finding in candidates if finding.kind == "prevention_delta"
    }
    selected: list[Finding] = []
    per_kind: dict[str, int] = {}
    seen_titles: set[tuple[str, str]] = set()
    seen_actions: set[tuple[str, str, str]] = set()
    validation_family = {"premature_completion", "low_validation"}
    family_selected = 0
    for finding in candidates:
        key = (finding.kind, finding.title)
        if (
            key in seen_titles
            or per_kind.get(finding.kind, 0) >= 2
            or (
                finding.kind == "environment_thrash"
                and finding.repo in specific_setup
            )
        ):
            continue
        action_key = _action_key(finding)
        if action_key is not None and action_key in seen_actions:
            continue
        if finding.kind in validation_family:
            if family_selected:
                continue
            family_selected += 1
        seen_titles.add(key)
        if action_key is not None:
            seen_actions.add(action_key)
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


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _is_path_target(title: str) -> bool:
    return bool(title) and " " not in title and ("/" in title or "." in title)
