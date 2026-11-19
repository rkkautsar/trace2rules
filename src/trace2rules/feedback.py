from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

TOPICS = (
    "cpu", "memory", "ram", "cache", "caching", "schema", "index", "test",
    "benchmark", "load", "wsl", "timeout", "sqlite", "postgres", "build",
    "compile", "server", "docker", "gpu", "port", "config",
)
NEGATION = re.compile(
    r"(?i)\b(no|not|never|don't|do not|without|stop|avoid|limit|cap|reduce|lower|only)\b"
)
ACCEPTANCE = re.compile(r"(?i)\b(yes|good|great|perfect|thanks|thank you|lgtm|nice|works)\b")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
EXPLICIT_RE = re.compile(
    r"(?i)\b(?:use|run|prefer|avoid|stop using|instead of|rather than|do not use|don't use)\s+"
    r"(?:`([^`]+)`|([A-Za-z0-9_./-]{2,}))"
)
CODE_RE = re.compile(r"`([^`]+)`")
KEY_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "only", "just",
    "your", "you", "please", "still", "again", "also", "more", "less", "code",
    "file", "files", "work", "working", "thing", "things", "really", "actually",
    "it", "is", "to", "of", "in", "on", "at", "as", "be", "or", "if", "so",
    "we", "i", "me", "my", "our", "but", "not", "no", "do", "does", "did",
}


@dataclass(slots=True)
class SteeringFinding:
    repo: str
    session: str
    key: str
    occurrences: int
    examples: tuple[str, ...]


def user_turns(path: Path) -> list[str]:
    turns: list[str] = []
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return turns
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "message":
                message = record.get("message") or {}
                role = message.get("role")
                content = message.get("content")
            else:
                role = record.get("role")
                content = record.get("content")
            if role != "user":
                continue
            if isinstance(content, list):
                text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = str(content or "")
            text = text.strip()
            if not text or text.startswith(("<system-reminder>", "<user>")):
                continue
            turns.append(text)
    return turns


def _clean_key(value: str) -> str:
    key = value.strip().strip("`").lower()
    key = key.split()[0] if " " in key else key
    key = key.strip(".,:;!?()[]{}<>\"'")
    return key if len(key) >= 2 and key not in KEY_STOPWORDS else ""


def _explicit_keys(sentence: str) -> set[str]:
    keys: set[str] = set()
    for match in EXPLICIT_RE.finditer(sentence):
        key = _clean_key(match.group(1) or match.group(2) or "")
        if key:
            keys.add(key)
    for raw in CODE_RE.findall(sentence):
        key = _clean_key(raw)
        if key and (key.startswith(("src/", "tests/", "./")) or "." in key or "/" in key):
            keys.add(key)
    return keys


def constraint_keys(text: str) -> tuple[str, ...]:
    keys: set[str] = set()
    for sentence in SENTENCE_SPLIT.split(text):
        if not sentence or ACCEPTANCE.search(sentence) and not NEGATION.search(sentence):
            continue
        if not NEGATION.search(sentence) and not EXPLICIT_RE.search(sentence):
            continue
        lower = sentence.lower()
        for topic in TOPICS:
            if re.search(rf"\b{re.escape(topic)}\b", lower):
                keys.add(topic)
        keys.update(_explicit_keys(sentence))
    return tuple(sorted(keys))


def mine_repeated_steering(source: str | Path) -> list[SteeringFinding]:
    root = Path(source).expanduser()
    findings: list[SteeringFinding] = []
    if root.is_file():
        files = [root]
    elif root.name == "sessions":
        # OMP stores the main transcript one level below each project directory.
        files = sorted(root.glob("*/*.jsonl"))
    else:
        files = sorted(root.rglob("*.jsonl"))
    for path in files:
        turns = user_turns(path)
        by_key: dict[str, list[str]] = defaultdict(list)
        for text in turns:
            for key in constraint_keys(text):
                by_key[key].append(text)
        for key, messages in by_key.items():
            if len(messages) < 2:
                continue
            findings.append(
                SteeringFinding(
                    repo=path.parent.name.lstrip("-") or "unknown",
                    session=str(path),
                    key=key,
                    occurrences=len(messages),
                    examples=tuple(message[:240] for message in messages[:3]),
                )
            )
    return findings
