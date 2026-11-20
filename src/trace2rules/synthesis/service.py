from __future__ import annotations

import re

from .models import Synthesis, configured_max_rules
from .prompt import _json_contract, _llm_prompt


def render(synthesis: Synthesis, repo: str, sessions: int) -> str:
    lines = []
    for rule in synthesis.rules:
        lines.append(f"## {rule.category}")
        lines.append(f"- **Rule:** {rule.rule_markdown}")
        lines.append(f"  **Evidence:** {rule.evidence_summary}")
        if rule.enforceable and rule.enforcement:
            lines.append(f"  **Enforcement:** {rule.enforcement}")
        if rule.support_sessions:
            lines.append(
                f"  **Support:** {rule.support_sessions} sessions, "
                f"{rule.support_instances} instances ({rule.confidence})"
            )
        if rule.finding_kinds:
            lines.append(f"  **Finding types:** {', '.join(rule.finding_kinds)}")
        for quote in rule.evidence_quotes:
            lines.append(f"  **Evidence quote:** {quote}")
        if rule.sources:
            lines.append(f"  **Sources:** {'; '.join(rule.sources)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def synthesize_report(
    prompt: str,
    repo: str | None = None,
    max_rules: int | None = None,
) -> Synthesis:
    """Call the configured LLM with an existing report prompt.

    This is the thin optional wrapper around `report --prompt`; it does not
    re-mine sessions or rebuild the prompt.
    """
    if repo is None:
        match = re.search(r"(?m)^(?:# Repository|Repo):\s*(.+?)\s*$", prompt)
        if match:
            repo = match.group(1)
    result = _llm_prompt(prompt + _json_contract(repo))
    if result is None:
        raise RuntimeError(
            "No LLM client configured. Set TRACE2RULES_LLM_API_KEY, "
            "GEMINI_API_KEY, GOOGLE_API_KEY, or OPENAI_API_KEY."
        )
    limit = max_rules if max_rules is not None else configured_max_rules()
    return Synthesis(repo=repo or result.repo, rules=result.rules[:limit])
