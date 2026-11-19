from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Rule, Synthesis

_PLACEHOLDER_RE = re.compile(r"<(?:id|n|path|workspace)>|\{\{")


def _template(rule: Rule) -> str:
    text = re.sub(r"`[^`]*`", "`<x>`", rule.rule_markdown.lower())
    return re.sub(r"\b\d+\b", "<n>", text)


def evaluate_synthesis(synthesis: Synthesis, golden: dict | None = None) -> dict[str, object]:
    rules = synthesis.rules
    total = len(rules)
    placeholders = sum(
        1
        for rule in rules
        if _PLACEHOLDER_RE.search(rule.rule_markdown)
        or _PLACEHOLDER_RE.search(rule.evidence_summary)
    )
    templates = {_template(rule) for rule in rules}
    cited = sum(1 for rule in rules if rule.sources)
    result: dict[str, object] = {
        "repo": synthesis.repo,
        "rules": total,
        "placeholder_rate": placeholders / total if total else 0.0,
        "template_redundancy": 1 - len(templates) / total if total else 0.0,
        "citation_rate": cited / total if total else 0.0,
    }
    if golden:
        expected = [
            item
            for item in golden.get("repos", [])
            if item.get("repo") == synthesis.repo
        ]
        if expected:
            spec = expected[0]
            matches = 0
            for exp in spec.get("expected", []):
                for rule in rules:
                    text = f"{rule.rule_markdown} {rule.evidence_summary}".lower()
                    if all(term.lower() in text for term in exp.get("must_mention", [])):
                        matches += 1
                        break
            result["golden_matches"] = matches
            result["golden_expected"] = len(spec.get("expected", []))
            result["precision_at_5"] = (
                matches / min(total, 5) if total else 0.0
            )
            forbidden = [
                item for item in spec.get("forbidden", [])
                if any(term.lower() in f"{r.rule_markdown} {r.evidence_summary}".lower() for r in rules for term in item.get("must_not_mention", []))
            ]
            result["forbidden_hits"] = forbidden
    return result


def load_golden(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
