from trace2rules.reconcile import reconcile
from trace2rules.synthesize import Rule


def _rule(markdown: str, enforceable: bool = False, enforcement: str = "") -> Rule:
    return Rule(
        category="test_command",
        rule_markdown=markdown,
        evidence_summary="test",
        estimated_tokens_saved_per_session=1,
        enforceable=enforceable,
        enforcement=enforcement,
    )


def test_reconcile_drops_duplicate_with_configured_enforcement() -> None:
    kept, actions = reconcile(
        [_rule("Run `make check` before finishing.")],
        [
            {
                "text": "Run `make check` before submitting.",
                "enforcement_status": "configured",
                "enforcement": "CI",
            }
        ],
    )
    assert kept == []
    assert actions[0].action == "drop"


def test_reconcile_updates_soft_rule_with_enforceable_proposal() -> None:
    kept, actions = reconcile(
        [_rule("Run `make check` in CI.", True, "required CI check")],
        [
            {
                "text": "Run `make check` before submitting.",
                "enforcement_status": "missing",
                "enforcement": "",
            }
        ],
    )
    assert len(kept) == 1
    assert actions[0].action == "update"


def test_reconcile_adds_new_rule() -> None:
    kept, actions = reconcile(
        [_rule("Run `pytest tests/test_api.py`.")],
        [],
    )
    assert len(kept) == 1
    assert actions[0].action == "add"


def test_command_detection_ignores_backticked_paths() -> None:
    kept, actions = reconcile(
        [_rule("Keep `src/api.py` free of CLI imports.")],
        [
            {
                "text": "Keep `src/api.py` free of CLI imports.",
                "enforcement_status": "missing",
                "enforcement": "",
            }
        ],
    )
    assert kept == []
    assert actions[0].action == "keep"


def test_reconcile_does_not_conflate_distinct_commands() -> None:
    proposed = [_rule("Run `pytest tests/test_api.py -q` after editing API handlers.")]
    existing = [
        {
            "text": "Run `pytest tests/test_db.py -q` after editing DB handlers.",
            "enforcement_status": "configured",
            "enforcement": "CI",
        }
    ]
    kept, actions = reconcile(proposed, existing)
    assert len(kept) == 1
    assert actions[0].action == "add"


def test_reconcile_upgrades_available_enforcement() -> None:
    kept, actions = reconcile(
        [_rule("Wire `make check` into required CI.", True, "required CI check")],
        [
            {
                "text": "Run `make check` before submitting.",
                "enforcement_status": "available",
                "enforcement": "Makefile: check",
            }
        ],
    )
    assert len(kept) == 1
    assert actions[0].action == "update"
