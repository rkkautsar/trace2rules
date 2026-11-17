from __future__ import annotations

from trace2rules.ingest import (
    _error_detail,
    _error_site,
    _event_outcome,
    _is_control_target,
    _observation_cause,
)


def test_search_exit_one_is_empty_not_error() -> None:
    assert _event_outcome("search", "", 1, False, False, False) == "empty"
    assert _event_outcome("search", "", 2, False, False, False) == "err"


def test_control_targets_are_dropped() -> None:
    assert _is_control_target("C-c")
    assert _is_control_target("\x03")
    assert not _is_control_target("pytest -q")


def test_error_evidence_extracts_cause_and_site() -> None:
    observation = (
        "running tests\n"
        "FAILED tests/test_parser.py::test_parse - AttributeError: module 'sqlglot' has no attribute 'tokenize'\n"
        "Traceback (most recent call last):\n"
        "  File \"/workspace/tests/test_parser.py\", line 42, in test_parse\n"
    )
    cause = _observation_cause(observation)
    assert "AttributeError" in cause
    assert _error_detail(observation) == "AttributeError: <object> has no attribute 'tokenize'"
    assert _error_site(observation) == "tests/test_parser.py::test_parse"
