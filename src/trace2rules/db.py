from __future__ import annotations

import os
import tempfile
from pathlib import Path

import duckdb

DB_PATH = Path(
    os.environ.get(
        "TRACE2RULES_DB",
        Path(tempfile.gettempdir()) / "trace2rules" / "trace2rules.duckdb",
    )
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       VARCHAR PRIMARY KEY,
    repo             VARCHAR NOT NULL,
    instance_id      VARCHAR NOT NULL,
    resolved         BOOLEAN NOT NULL,
    total_turns      INTEGER NOT NULL,
    source           VARCHAR DEFAULT '',
    source_ref       VARCHAR DEFAULT '',
    agent            VARCHAR DEFAULT '',
    success_score    DOUBLE,
    started_at       TIMESTAMP,
    cwd              VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tool_events (
    event_id          VARCHAR PRIMARY KEY,
    session_id        VARCHAR NOT NULL,
    repo              VARCHAR NOT NULL,
    step_idx          INTEGER NOT NULL,
    segment_idx       INTEGER NOT NULL DEFAULT 0,
    tool_name         VARCHAR NOT NULL,
    action_type       VARCHAR NOT NULL,
    verb              VARCHAR NOT NULL,
    object_id         VARCHAR NOT NULL,
    target            VARCHAR NOT NULL,
    raw_target        VARCHAR NOT NULL,
    outcome           VARCHAR NOT NULL,
    exit_code         INTEGER,
    error_signature   VARCHAR NOT NULL,
    is_error          BOOLEAN NOT NULL,
    approx_out_tokens INTEGER NOT NULL,
    obs_excerpt       VARCHAR NOT NULL,
    obs_cause         VARCHAR DEFAULT '',
    error_detail      VARCHAR DEFAULT '',
    error_site        VARCHAR DEFAULT '',
    error_message     VARCHAR DEFAULT '',
    error_family      VARCHAR DEFAULT '',
    error_origin      VARCHAR DEFAULT '',
    assistant_context VARCHAR DEFAULT '',
    event_hash        VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS test_results (
    session_id        VARCHAR NOT NULL,
    repo              VARCHAR NOT NULL,
    step_idx          INTEGER NOT NULL,
    command           VARCHAR NOT NULL,
    total             INTEGER NOT NULL,
    passed            INTEGER NOT NULL,
    failed            INTEGER NOT NULL,
    errors            INTEGER NOT NULL,
    skipped           INTEGER NOT NULL,
    outcome           VARCHAR NOT NULL,
    approx_out_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_events (
    session_id        VARCHAR NOT NULL,
    step_idx          INTEGER NOT NULL,
    kind              VARCHAR NOT NULL,
    text              VARCHAR NOT NULL,
    keys              VARCHAR NOT NULL,
    repo              VARCHAR DEFAULT ''
);

"""


def connect(path: str | Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(
        str(path),
        config={
            "memory_limit": "2048MB",
            "threads": "1",
            "preserve_insertion_order": "false",
        },
    )
    con.execute(SCHEMA)
    for ddl in (
        "ALTER TABLE sessions ADD COLUMN source VARCHAR DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN agent VARCHAR DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN source_ref VARCHAR DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN success_score DOUBLE",
        "ALTER TABLE sessions ADD COLUMN started_at TIMESTAMP",
        "ALTER TABLE sessions ADD COLUMN cwd VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN verb VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN object_id VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN outcome VARCHAR DEFAULT 'unknown'",
        "ALTER TABLE tool_events ADD COLUMN exit_code INTEGER",
        "ALTER TABLE tool_events ADD COLUMN error_signature VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN obs_excerpt VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN segment_idx INTEGER DEFAULT 0",
        "ALTER TABLE tool_events ADD COLUMN obs_cause VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN error_detail VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN error_site VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN error_message VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN error_family VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN error_origin VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN event_hash VARCHAR DEFAULT ''",
        "ALTER TABLE tool_events ADD COLUMN assistant_context VARCHAR DEFAULT ''",
    ):
        try:
            con.execute(ddl)
        except duckdb.Error:
            pass
    return con
