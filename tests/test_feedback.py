from pathlib import Path

from trace2rules.feedback import constraint_keys, mine_repeated_steering


def test_constraint_keys_require_negation_near_topic() -> None:
    assert constraint_keys("no caching across requests") == ("caching",)
    assert constraint_keys("the cache warms up quickly") == ()
    assert constraint_keys("limit the cpu and memory first") == ("cpu", "memory")


def test_repeated_steering_requires_two_turns(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"type":"message","message":{"role":"user","content":"limit the cpu first"}}',
                '{"type":"message","message":{"role":"assistant","content":"ok"}}',
                '{"type":"message","message":{"role":"user","content":"no caching is allowed"}}',
                '{"type":"message","message":{"role":"user","content":"no caching across requests"}}',
            ]
        )
    )
    findings = mine_repeated_steering(tmp_path)
    assert [finding.key for finding in findings] == ["caching"]
    assert findings[0].occurrences == 2


def test_constraint_keys_extract_explicit_corrections() -> None:
    assert "pnpm" in constraint_keys("Use pnpm instead of npm.")
    assert "head" in constraint_keys("Don't use `head` to truncate output.")
    assert "src/api.py" in constraint_keys("Do not edit `src/api.py` in this change.")
