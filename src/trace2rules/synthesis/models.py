from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_MAX_RULES = 10
MAX_LINES = 160
MAX_CHARS = 12_000
EXACT_LENGTHS = (3, 4, 5)


class Rule(BaseModel):
    category: Literal["test_command", "validation", "command", "path_scope", "git_workflow", "commit_hygiene", "architecture_map", "context_hygiene", "convention"]
    rule_markdown: str
    evidence_summary: str
    estimated_tokens_saved_per_session: int = Field(ge=0)
    sources: list[str] = Field(default_factory=list)
    enforceable: bool = False
    enforcement: str = ""
    support_sessions: int = 0
    support_instances: int = 0
    confidence: Literal["low", "medium", "high"] = "low"
    finding_kinds: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
