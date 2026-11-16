from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass(slots=True)
class Session:
    """A normalized session accepted by the shared DuckDB writer."""

    session_id: str
    repo: str
    instance_id: str = ""
    resolved: bool = False
    calls: list[ToolCall] = field(default_factory=list)
    source: str = ""
    agent: str = ""
    success_score: float | None = None
