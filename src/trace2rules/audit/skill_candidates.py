from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROCEDURE_RE = re.compile(
    r"(?i)\b(?:release|deploy|publish|migration|runbook|incident|troubleshoot|"
    r"onboard|adding|updating|mechanical|procedure)\b"
)
BROAD_RE = re.compile(r"(?i)\b(?:policy|instructions|about|overview|contributing)\b")
TASK_RE = re.compile(
    r"(?i)\b(?:regression|adding|updating|release|deploy|publish|migration|"
    r"mechanical|onboard|troubleshoot|fixture|test guide|code review|"
    r"bug fixes?|new features?|minimum.*version)\b"
)
COMMAND_HEADING_RE = re.compile(
    r"(?i)^(?:commands?|testing|tests?|build|lint|type(?:check|s)?|"
    r"installation|setup|development commands|verification)\b"
)
COMPLETION_RE = re.compile(
    r"(?i)\b(?:verify|confirm|check|ensure|before finishing|done|completion|"
    r"expected|pass(?:es|ing)?|green)\b"
)
ORDER_RE = re.compile(r"(?i)\b(?:first|then|next|after|before|finally|step \d+)\b")
ORDERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S")
BULLET_RE = re.compile(r"^\s*[-*]\s+\S")


@dataclass(frozen=True)
class SkillCandidate:
    source_path: str
    source_scope: str
    line_start: int
    line_end: int
    heading: str
    signals: tuple[str, ...]
    excerpt: str
    suggestion: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "source_scope": self.source_scope,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "heading": self.heading,
            "signals": list(self.signals),
            "excerpt": self.excerpt,
            "suggestion": self.suggestion,
        }


def _excerpt(lines: list[str], limit: int = 180) -> str:
    text = " ".join(line.strip().lstrip("-*0123456789.) ") for line in lines if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _slug(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "workflow"


def _sections(text: str) -> list[tuple[int, int, str, list[str], list[str], int]]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    fenced = False
    for index, line in enumerate(lines, start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    if not headings:
        return [(1, len(lines), "", lines, lines, 0)]
    sections = []
    for index, (start, level, heading) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _next_heading in headings[index + 1 :]:
            if next_level <= level:
                end = next_start - 1
                break
        direct_end = end
        for next_start, next_level, _next_heading in headings[index + 1 :]:
            if next_level > level:
                direct_end = next_start - 1
                break
        content = lines[start:end]
        sections.append((start, end, heading, content, lines[start:direct_end], level))
    return sections


def scan_text(text: str, *, source_path: str = "", source_scope: str = "repo") -> list[SkillCandidate]:
    candidates: list[SkillCandidate] = []
    for start, end, heading, content, direct, level in _sections(text):
        heading_text = heading.strip()
        if not heading_text or "|" in heading_text:
            continue
        ordered = sum(bool(ORDERED_STEP_RE.match(line)) for line in direct)
        bullets = sum(bool(BULLET_RE.match(line)) for line in direct)
        command_text = "\n".join(direct)
        commands = len(re.findall(r"`[^`\n]+`", command_text))
        has_completion = bool(COMPLETION_RE.search(command_text))
        has_order = bool(ORDER_RE.search(command_text))
        procedural = bool(PROCEDURE_RE.search(heading_text))
        task_specific = bool(TASK_RE.search(heading_text))
        command_heading = bool(COMMAND_HEADING_RE.match(heading_text))
        signals: list[str] = []
        if ordered >= 2:
            signals.append(f"{ordered} ordered steps")
        if procedural:
            signals.append("procedure heading")
        if commands >= 2:
            signals.append(f"{commands} commands")
        if has_completion:
            signals.append("completion check")
        if has_order:
            signals.append("explicit order")
        qualifies = False
        if ordered >= 3 and (procedural or task_specific):
            qualifies = True
        elif task_specific and bullets >= 3 and (commands >= 2 or has_completion):
            qualifies = True
            signals.append(f"{bullets} task-specific bullets")
        if not qualifies or command_heading:
            continue
        if level <= 1 and not procedural:
            continue
        if level == 2 and BROAD_RE.search(heading_text) and not procedural:
            continue
        slug = _slug(heading_text)
        candidates.append(
            SkillCandidate(
                source_path=source_path,
                source_scope=source_scope,
                line_start=start,
                line_end=end,
                heading=heading_text,
                signals=tuple(signals),
                excerpt=_excerpt(direct if any(line.strip() for line in direct) else content),
                suggestion=(
                    f"Move this procedure to skills/{slug}/SKILL.md with a clear name and description. "
                    "Skills are auto-discovered; add an AGENTS.md pointer only if routing is ambiguous."
                ),
            )
        )
    candidates.sort(key=lambda item: (item.line_start, -item.line_end))
    deduped: list[SkillCandidate] = []
    for candidate in candidates:
        if any(
            other is not candidate
            and other.line_start <= candidate.line_start
            and other.line_end >= candidate.line_end
            and (other.line_start < candidate.line_start or other.line_end > candidate.line_end)
            for other in candidates
        ):
            continue
        deduped.append(candidate)
    return deduped


def scan_file(path: Path, *, source_scope: str = "repo") -> list[SkillCandidate]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(text, source_path=str(path), source_scope=source_scope)
