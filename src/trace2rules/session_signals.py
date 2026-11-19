"""Backward-compatible facade for session-signal heuristics.

New code should import from ``trace2rules.mining.heuristics``.
"""

from .mining.heuristics import (
    command_errors,
    edit_churn,
    feedback_loops,
    permission_denials,
    repeated_reads,
)

__all__ = [
    "command_errors",
    "edit_churn",
    "feedback_loops",
    "permission_denials",
    "repeated_reads",
]
