from __future__ import annotations

from .common import AuditOutcome


def evaluate(matched: bool) -> AuditOutcome:
    return AuditOutcome(applicable=1, not_observed=0 if matched else 1)
