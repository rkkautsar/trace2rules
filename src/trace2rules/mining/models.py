from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    kind: str
    repo: str
    title: str
    sessions: int
    tokens: int
    detail: str = ""
    sequence: str = ""
    variants: tuple[str, ...] = field(default_factory=tuple)
    lift: float = 0.0
    rework: int = 0
    setup_tax: int = 0
    cost: int = 0
    paired_instances: int = 0
    delta_rework: float = 0.0
    delta_setup: float = 0.0
    intent: str = ""
    object_type: str = ""
    intent_confidence: float = 0.0
    object_confidence: float = 0.0
    event_keys: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    cause: str = ""
    failure_family: str = ""
    cause_support_sessions: int = 0
    side: str = ""
    recommendation: str = ""
    window: tuple[dict, ...] = field(default_factory=tuple)
    edit_sessions: int = 0

    @property
    def evidence(self) -> str:
        return f"{self.detail}; ~{self.tokens:,} tokens"


