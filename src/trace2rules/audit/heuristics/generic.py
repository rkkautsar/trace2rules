from __future__ import annotations

from .common import AuditOutcome


def evaluate(matched: bool) -> AuditOutcome:
    return AuditOutcome(applicable=0)
