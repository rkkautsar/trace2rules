from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import duckdb

from .heuristics import (
    blind_edits,
    command_errors,
    context_blasts,
    correction_loops,
    destructive_commands,
    edit_churn,
    dependency_drift,
    edit_rejections,
    generated_edits,
    environment_thrash,
    failure_loops,
    false_completions,
    feedback_loops,
    global_patterns,
    hotspots,
    low_validation,
    no_reproduction,
    noop_retries,
    orientation,
    permission_denials,
    premature_completion,
    recovery_loops,
    repeated_reads,
    selector_misses,
    site_edit_mismatch,
    regressions_after_green,
    shrinking_test_suites,
    stuck_loops,
    workaround_scripts,
)
from .models import Finding


def mine(
    con: duckdb.DuckDBPyConnection,
    top: int | None = 25,
    workers: int | None = None,
) -> dict[str, list[Finding]]:
    jobs = {
        "hotspots": hotspots,
        "recovery": recovery_loops,
        "context": context_blasts,
        "orientation": orientation,
        "global": global_patterns,
        "selector_miss": selector_misses,
        "workaround": workaround_scripts,
        "stuck_loop": stuck_loops,
        "noop_retry": noop_retries,
        "premature_completion": premature_completion,
        "low_validation": low_validation,
        "site_edit_mismatch": site_edit_mismatch,
        "permission": permission_denials,
        "reread": repeated_reads,
        "edit_churn": edit_churn,
        "command_error": command_errors,
        "feedback": feedback_loops,
        "failure_loop": failure_loops,
        "edit_rejection": edit_rejections,
        "dependency_drift": dependency_drift,
        "generated_edit": generated_edits,
        "blind_edit": blind_edits,
        "environment_thrash": environment_thrash,
        "destructive_command": destructive_commands,
        "no_reproduction": no_reproduction,
        "false_completion": false_completions,
        "test_regression": regressions_after_green,
        "test_shrink": shrinking_test_suites,
        "correction_loop": correction_loops,
    }
    workers = workers or min(2, len(jobs), os.cpu_count() or 1)
    if workers <= 1:
        return {key: fn(con, limit=top) for key, fn in jobs.items()} | {"clusters": []}

    def run(job):
        key, fn = job
        cursor = con.cursor()
        try:
            return key, fn(cursor, limit=top)
        finally:
            cursor.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(run, jobs.items()))
    results["clusters"] = []
    return results
