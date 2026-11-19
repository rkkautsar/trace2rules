from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AuditOutcome:
    applicable: int = 0
    observed: int = 0
    compliant: int = 0
    violations: int = 0
    failed_validation: int = 0
    not_observed: int = 0
    undecided: int = 0
    test_outcomes: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
