from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ToolCall:
    """A normalized tool call before it is classified for DuckDB."""

    tool_name: str
    raw_target: str
    observation: str = ""
    action_type: str | None = None
    target: str | None = None
    verb: str | None = None
    object_id: str | None = None
    exit_code: int | None = None
    is_error: bool | None = None
    segment_idx: int = 0
    assistant_context: str = ""


@dataclass(slots=True)
class Feedback:
    text: str
    step_idx: int
    kind: str = "feedback"
    keys: tuple[str, ...] = ()


@dataclass(slots=True)
class Session:
    """A normalized session accepted by the shared DuckDB writer."""

    session_id: str
    repo: str
    instance_id: str = ""
    resolved: bool = False
    calls: list[ToolCall] = field(default_factory=list)
    source: str = ""
    source_ref: str = ""
    agent: str = ""
    success_score: float | None = None
    started_at: datetime | None = None
    feedback: list[Feedback] = field(default_factory=list)
