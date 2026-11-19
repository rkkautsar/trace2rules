from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentRule:
    text: str
    category: str
    commands: tuple[str, ...]
    paths: tuple[str, ...]
    keywords: tuple[str, ...]
    source: str = "AGENTS.md"
    line: int = 0
    section: str = ""
