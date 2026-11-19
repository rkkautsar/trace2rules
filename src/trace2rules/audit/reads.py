from __future__ import annotations

import re
from pathlib import Path

from .models import AgentRule
from .parsing import COMMAND_RE, PATH_RE

_READ_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
    ".kt", ".cs", ".rb", ".php", ".swift",
}
_LARGE_SOURCE_BYTES = 12_000


def _read_candidate(path: str) -> str:
    return re.sub(r":\d+(?:-\d+)?$", "", path.strip().strip("`"))


def _large_source(path: str, root: Path | None, names: set[str]) -> bool:
    candidate = _read_candidate(path)
    if not candidate or Path(candidate).suffix not in _READ_SUFFIXES:
        return False
    if Path(candidate).name in names:
        return True
    if root is None:
        return False
    file_path = Path(candidate)
    if file_path.is_absolute():
        try:
            file_path = file_path.relative_to(root)
        except ValueError:
            return False
    file_path = root / file_path
    try:
        return file_path.is_file() and file_path.stat().st_size >= _LARGE_SOURCE_BYTES
    except OSError:
        return False


def _scoped_test_edits(rule: AgentRule, edit_events: list[tuple[str, str]]) -> list[tuple[str, str]]:
    lower = rule.text.lower()
    if "<crate>" in lower or ("cargo test" in lower and "crate" in lower):
        return [
            (step, target)
            for step, target in edit_events
            if target.endswith(".rs") and re.search(r"(^|/)crates/", target)
        ]
    if "snapshot" in lower:
        return [
            (step, target)
            for step, target in edit_events
            if re.search(r"(?i)(^|/)(?:tests?|fixtures?|snapshots?)/|\.snap$", target)
        ]
    if "fixture" in lower:
        return [
            (step, target)
            for step, target in edit_events
            if re.search(r"(?i)(^|/)(?:tests?|fixtures?)/", target)
        ]
    return edit_events


def _is_source_read(action: str, raw: str, target: str, edited: str) -> bool:
    candidates = [target, *re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)", raw)]
    edited_path = Path(_read_candidate(edited))
    has_source = any(
        Path(_read_candidate(path)).suffix in _READ_SUFFIXES
        and (
            Path(_read_candidate(path)) == edited_path
            or Path(_read_candidate(path)).name == edited_path.name
        )
        for path in candidates
    )
    if not has_source:
        return False
    if action == "read":
        return True
    return action == "exec" and bool(re.search(r"\b(?:cat|sed|nl|head|tail)\b", raw))


def _read_hygiene(events: list[tuple], root: Path | None, rule: AgentRule) -> tuple[list[str], bool]:
    names = {
        Path(name).name
        for name in re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)\b", rule.text)
        if not re.search(r"[*<>]", name)
    }
    reads: list[str] = []
    whole = False
    for _sid, _step, action, raw, target, _object, _outcome in events:
        if action not in {"read", "exec"}:
            continue
        if action == "exec" and re.search(r"\b(?:rg|grep)\b", raw) and not re.search(
            r"\b(?:cat|nl|sed|head|tail)\b", raw
        ):
            continue
        candidates = [target, *re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)", raw)]
        if not any(_large_source(path, root, names) for path in candidates):
            continue
        reads.append(raw)
        bounded = bool(
            re.search(r"\b(?:sed\s+-n|head|tail)\b|:\d+(?:-\d+)?\b", f"{raw} {target}")
        )
        shell_read = bool(re.search(r"\b(?:cat|nl\s+-ba)\b", raw))
        if not bounded and (action == "read" or shell_read):
            whole = True
    return reads, whole


def _stale_paths(rule: AgentRule, root: Path | None) -> tuple[str, ...]:
    if root is None or not root.is_dir():
        return ()
    stale: list[str] = []
    for value in COMMAND_RE.findall(rule.text):
        for candidate in PATH_RE.findall(value):
            candidate = candidate.strip().strip("`")
            if (
                not candidate
                or candidate.startswith(("/", "~"))
                or candidate.startswith(("http://", "https://"))
                or re.search(r"[*<>]", candidate)
                or (
                    len(candidate.split("/")) == 2
                    and not Path(candidate).suffix
                )
            ):
                continue
            if not (root / candidate).exists():
                stale.append(candidate)
    return tuple(dict.fromkeys(stale))
