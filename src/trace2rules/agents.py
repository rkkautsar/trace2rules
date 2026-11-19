"""Compatibility facade for the audit implementation.

New code should import from ``trace2rules.audit.*``.
"""

from .audit.enforcement import discover_enforcement
from .audit.engine import (
    RuleAudit,
    _infer_scope,
    _matches,
    _rule_conflicts,
    audit_agents,
    audit_commit,
    audit_payload,
    audit_prompt,
    available_text,
    latest_agents_commit,
    resolve_agents,
)
from .audit.models import AgentRule
from .audit.parsing import parse_agents, parse_agents_text

__all__ = [
    "AgentRule",
    "RuleAudit",
    "audit_agents",
    "audit_commit",
    "audit_payload",
    "audit_prompt",
    "available_text",
    "discover_enforcement",
    "latest_agents_commit",
    "parse_agents",
    "parse_agents_text",
    "resolve_agents",
    "_infer_scope",
    "_matches",
    "_rule_conflicts",
]
