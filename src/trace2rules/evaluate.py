from __future__ import annotations

import re

from .synthesize import Synthesis

_PLACEHOLDER_RE = re.compile(r"<(?:id|n|path|workspace)>|\{\{")


def evaluate_synthesis(synthesis: Synthesis) -> dict[str, object]:
    rules = synthesis.rules
    total = len(rules)
    placeholders = sum(
        1
        for rule in rules
        if _PLACEHOLDER_RE.search(rule.rule_markdown)
        or _PLACEHOLDER_RE.search(rule.evidence_summary)
    )
    templates = {
        re.sub(r"`[^`]*`", "`<x>`", re.sub(r"\b\d+\b", "<n>", rule.rule_markdown.lower()))
        for rule in rules
    }
    cited = sum(1 for rule in rules if rule.sources)
    return {
        "repo": synthesis.repo,
        "rules": total,
        "placeholder_rate": placeholders / total if total else 0.0,
        "template_redundancy": 1 - len(templates) / total if total else 0.0,
        "citation_rate": cited / total if total else 0.0,
    }
