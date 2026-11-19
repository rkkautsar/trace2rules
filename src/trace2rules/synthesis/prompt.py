from __future__ import annotations

import json
import os
import re

from ..mining.models import Finding
from ..redact import redact_value
from .models import MAX_CHARS, MAX_LINES, MAX_RULES, Synthesis

PROMPT_FINDINGS = 10
PROMPT_WINDOW_EVENTS = 3


def _finding_confidence(finding: Finding) -> str:
    if finding.sessions >= 10 and finding.paired_instances >= 2:
        return "high"
    if finding.sessions >= 3:
        return "medium"
    return "low"


def _clip(value: object, width: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _compact_window(window: tuple[dict, ...]) -> list[dict]:
    return [
        {
            key: _clip(value, 240) if isinstance(value, str) else value
            for key, value in event.items()
        }
        for event in window[:PROMPT_WINDOW_EVENTS]
    ]


def _compact_existing(existing: dict | None) -> dict | None:
    if not existing:
        return existing
    compact = dict(existing)
    compact["rules"] = [
        {
            **rule,
            "text": _clip(rule.get("text", ""), 240),
            "examples": rule.get("examples", [])[:1],
        }
        for rule in existing.get("rules", [])[:40]
    ]
    return compact


def _prompt(
    findings: list[Finding],
    repo: str,
    max_rules: int = MAX_RULES,
    context: dict | None = None,
    existing: dict | None = None,
) -> str:
    payload = redact_value(
        [
            {
                "kind": finding.kind,
                "sessions": finding.sessions,
                "instances": finding.paired_instances,
                "confidence": _finding_confidence(finding),
                "cost": finding.cost or finding.tokens,
                "setup_tax": finding.setup_tax,
                "rework": finding.rework,
                "paired_instances": finding.paired_instances,
                "delta_rework": round(finding.delta_rework, 2),
                "title": finding.title,
                "failure_family": finding.failure_family,
                "detail": finding.detail,
                "sequence": finding.sequence,
                "variants": list(finding.variants),
                "cause": finding.cause,
                "cause_support_sessions": finding.cause_support_sessions,
                "side": finding.side,
                "window": _compact_window(finding.window),
            }
            for finding in findings[:PROMPT_FINDINGS]
        ]
    )
    return f"""Repo: {repo}

Findings from mined agent trajectories for this repo only (JSON):
{json.dumps(payload, indent=2)}

Command/bootstrap evidence (JSON):
{json.dumps(redact_value(context or {}), indent=2)}

Existing AGENTS.md rules and audit (JSON):
{json.dumps(redact_value(_compact_existing(existing) or {}), indent=2)}

Write AGENTS.md rules for this repository. Requirements:
- Emit 0-{max_rules} declarative rules. Do not fill the quota: fewer rules are better, and zero is valid when the evidence does not support a rule. Do not emit task-specific issue descriptions.
- Emit only durable, normative policy. Do not encode memories, historical facts, decisions, dates, owners, task state, or "what happened" summaries. If the evidence is descriptive rather than prescriptive, omit it.
- A valid rule is either enforceable by a CI/pre-commit/lint check or stable policy that should apply to future agents in the same repo. If neither is true, omit it.
- Categories: test_command, architecture_map, context_hygiene, convention.
- Do not repeat an existing AGENTS.md rule. If an existing rule is violated or unenforced, propose the concrete enforcement (CI/pre-commit/lint) instead.
- Set `enforceable` to true only when a repository CI/pre-commit/lint check can actually observe and enforce the rule. Put that exact check in `enforcement`.
- Rules about agent read behavior, tool-call ordering, or context usage are guidance-only: set `enforceable` false and `enforcement` to `"guidance only"`.
- rule_markdown: 1-2 imperative sentences, concrete command or file names.
- evidence_summary: sessions affected and approximate setup/rework token cost.
- No generic platitudes ("be careful", "read the docs").
- Ignore task-specific verifier scripts, one-off filenames, and normal edit -> test-fail loops.
- For `recovery` findings, use the representative window from failure through success to explain the failed -> successful transition; cite the support count. Do not infer intent not shown by the tool calls.
- Prefer `confidence: high` or `medium` findings; `low` findings need corroboration or should be omitted.
- A window is one illustrative occurrence, not proof of intent. Never encode an identifier that appears in fewer than two sessions.
- Do not use resolution labels or infer success from them. Use `validated_sessions` and `unvalidated_sessions` only.
- `outcomes`/audit `rate: null` means fewer than 20 opportunities; do not describe it as a failure.
- The `assistant` field is the agent's reasoning, not observed evidence. Use it to explain a sequence, but never cite it as a source or count it as a finding.
- For `orientation` or `side=repo`, write a repository-side recommendation, not an agent rule.
- Do not mechanically recommend `| head`; distinguish quiet flags, targeted test selection, and repo entrypoints.
- Total output must stay under {max_rules} rules, {max(MAX_LINES, max_rules * 10)} lines, and {max(MAX_CHARS, max_rules * 500):,} characters.

Return JSON only, shaped exactly as:
{{"repo": "{repo}", "rules": [{{"category": "...", "rule_markdown": "...", "evidence_summary": "...", "estimated_tokens_saved_per_session": 0, "sources": ["<session_ref> (step N)"], "enforceable": true, "enforcement": "<CI/pre-commit/lint check>"}}]}}
- Include at least one source copied exactly from a finding window; never invent a source."""


def _client() -> tuple[object, str] | None:
    key = os.getenv("TRACE2RULES_LLM_API_KEY") or os.getenv("TRACE2RULES_API_KEY")
    base = os.getenv("TRACE2RULES_BASE_URL")
    model = os.getenv("TRACE2RULES_MODEL")
    if key:
        return _make_client(key, base, model or "gemini-2.5-pro")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key:
        return _make_client(
            gemini_key,
            base or "https://generativelanguage.googleapis.com/v1beta/openai/",
            model or "gemini-2.5-pro",
        )
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return _make_client(openai_key, base, model or "gpt-5")
    return None


def _make_client(key: str, base: str | None, model: str):
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=base) if base else OpenAI(api_key=key)
    return client, model


def _llm_prompt(prompt: str) -> Synthesis | None:
    resolved = _client()
    if resolved is None:
        return None
    client, model = resolved
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "You convert observed coding-agent friction into terse, durable AGENTS.md rules.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return Synthesis.model_validate(json.loads(content))
