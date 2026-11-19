from __future__ import annotations

from trace2rules.synthesis.evaluate import evaluate_synthesis
from trace2rules.synthesis.models import Rule, Synthesis


def _synthesis(*rules: Rule) -> Synthesis:
    return Synthesis(repo="acme/repo", rules=list(rules))


def test_eval_penalizes_placeholders_and_redundancy() -> None:
    rule = Rule(
        category="convention",
        rule_markdown="When `<id>` fails, do the same thing.",
        evidence_summary="10 sessions",
        estimated_tokens_saved_per_session=1,
    )
    metrics = evaluate_synthesis(_synthesis(rule, rule))
    assert metrics["placeholder_rate"] == 1.0
    assert metrics["template_redundancy"] == 0.5


def test_eval_matches_golden_and_flags_forbidden() -> None:
    rule = Rule(
        category="test_command",
        rule_markdown="Add a `test-one` target.",
        evidence_summary="10 sessions",
        estimated_tokens_saved_per_session=1,
        sources=["s1 (step 1)"],
    )
    golden = {
        "repos": [
            {
                "repo": "acme/repo",
                "expected": [{"must_mention": ["test-one"]}],
                "forbidden": [{"must_not_mention": ["lean readme"]}],
            }
        ]
    }
    metrics = evaluate_synthesis(_synthesis(rule), golden)
    assert metrics["precision_at_5"] == 1.0
    assert metrics["forbidden_hits"] == []
