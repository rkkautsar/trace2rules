from __future__ import annotations

import duckdb

from trace2rules.mining.prevention import prevention_deltas


def test_prevention_delta_requires_setup_before_same_command_success(monkeypatch) -> None:
    import trace2rules.mining.prevention as prevention

    monkeypatch.setattr(prevention, "_representative_window", lambda *args, **kwargs: ())
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE sessions (session_id VARCHAR, repo VARCHAR, instance_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE tool_events ("
        "session_id VARCHAR, step_idx INTEGER, segment_idx INTEGER, action_type VARCHAR, "
        "verb VARCHAR, target VARCHAR, raw_target VARCHAR, outcome VARCHAR, is_error BOOLEAN, "
        "error_signature VARCHAR, approx_out_tokens INTEGER)"
    )
    for index in range(3):
        session = f"s{index}"
        con.execute(
            "INSERT INTO sessions VALUES (?, 'acme/repo', ?)",
            [session, f"i{index}"],
        )
        con.execute(
            "INSERT INTO tool_events VALUES (?, 1, 0, 'exec', 'python', '-c', "
            "'cd /repo && python -c \"import acme\"', 'err', true, "
            "'exception:ModuleNotFoundError', 10)",
            [session],
        )
        con.execute(
            "INSERT INTO tool_events VALUES (?, 2, 0, 'exec', 'pip install', '.', "
            "'cd /repo && pip install -e .', 'ok', false, '', 5)",
            [session],
        )
        con.execute(
            "INSERT INTO tool_events VALUES (?, 3, 0, 'exec', 'python', '-c', "
            "'cd /repo && python -c \"import acme\"', 'ok', false, '', 10)",
            [session],
        )
    findings = prevention_deltas(con)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "prevention_delta"
    assert "pip install -e ." in finding.title
    assert finding.sessions == 3
    assert finding.side == "repo"


def test_prevention_delta_skips_recovery_without_setup_delta(monkeypatch) -> None:
    import trace2rules.mining.prevention as prevention

    monkeypatch.setattr(prevention, "_representative_window", lambda *args, **kwargs: ())
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE sessions (session_id VARCHAR, repo VARCHAR, instance_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE tool_events ("
        "session_id VARCHAR, step_idx INTEGER, segment_idx INTEGER, action_type VARCHAR, "
        "verb VARCHAR, target VARCHAR, raw_target VARCHAR, outcome VARCHAR, is_error BOOLEAN, "
        "error_signature VARCHAR, approx_out_tokens INTEGER)"
    )
    for index in range(3):
        session = f"s{index}"
        con.execute(
            "INSERT INTO sessions VALUES (?, 'acme/repo', ?)",
            [session, f"i{index}"],
        )
        con.execute(
            "INSERT INTO tool_events VALUES (?, 1, 0, 'exec', 'python', '-c', "
            "'cd /repo && python -c \"import acme\"', 'err', true, "
            "'exception:ModuleNotFoundError', 10)",
            [session],
        )
        con.execute(
            "INSERT INTO tool_events VALUES (?, 2, 0, 'exec', 'python', '-c', "
            "'cd /repo && python -c \"import acme\"', 'ok', false, '', 10)",
            [session],
        )
    assert prevention_deltas(con) == []
