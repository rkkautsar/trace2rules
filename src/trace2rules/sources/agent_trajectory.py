from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import Session, ToolCall
from .common import classify_tool_call, parse_arguments, target_from_args

SOURCE_DIRS = {
    "claude-code": ("claude_code", ".claude/projects"),
    "codex": ("codex", ".codex/sessions"),
    "pi": ("pi", ".pi/agent/sessions"),
    "omp": ("omp", ".omp/agent/sessions"),
    "opencode": ("opencode", ""),
}


def _detect_source(path: Path) -> str | None:
    if path.is_file() and path.suffix == ".json":
        return "opencode"
    if path.is_dir() and any(path.glob("*.json")):
        return "opencode"
    parts = path.parts
    for source, markers in SOURCE_DIRS.items():
        for marker in markers:
            if not marker:
                continue
            if marker in parts or marker in path.as_posix():
                return source
    if any(part == "sessions" for part in parts):
        for source, (marker, _) in SOURCE_DIRS.items():
            if marker in parts:
                return source
    return None


def _source_roots(root: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for source, (marker, suffix) in SOURCE_DIRS.items():
        if not marker:
            continue
        candidates = [root / suffix] if suffix else []
        candidates.extend(
            [
                root / marker,
                root / "sessions" / marker,
            ]
        )
        for candidate in candidates:
            if candidate.exists() and candidate not in seen:
                seen.add(candidate)
                roots.append((source, candidate))
    return roots


def _iter_files(root: Path, source: str, limit: int | None) -> list[Path]:
    if root.is_file():
        return [root]
    suffix = ".json" if source == "opencode" else ".jsonl"
    files = sorted(root.rglob(f"*{suffix}"))
    if limit:
        files = files[:limit]
    return [path for path in files if path.is_file()]


def _repo_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


def _session_id(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    return f"{path.stem}-{digest}"


def _records_to_session(
    path: Path, source: str, records: list[dict], repo_override: str | None
) -> Session:
    meta = next((record for record in records if record.get("role") == "meta"), {})
    repo = repo_override or _repo_from_cwd(meta.get("cwd")) or "unknown"
    session_id = _session_id(path)
    session = Session(
        session_id=session_id,
        repo=repo,
        instance_id=session_id,
        source="agent-trajectory",
        agent=source,
    )
    pending: dict[str, ToolCall] = {}
    for record in records:
        role = record.get("role")
        if role == "assistant" and record.get("tool_calls"):
            for tool_call in record["tool_calls"]:
                name = tool_call.get("name") or ""
                args = parse_arguments(tool_call.get("args"))
                raw_target = target_from_args(name, args, tool_call.get("args") or name)
                action, target, verb, object_id = classify_tool_call(name, args, raw_target)
                call = ToolCall(
                    tool_name=name,
                    raw_target=raw_target,
                    action_type=action,
                    target=target,
                    verb=verb,
                    object_id=object_id,
                )
                session.calls.append(call)
                if tool_call.get("id"):
                    pending[tool_call["id"]] = call
        elif role == "tool":
            call = pending.get(record.get("tool_call_id") or "")
            if call is not None:
                call.observation = record.get("content") or ""
                if "ok" in record:
                    call.is_error = not record["ok"]
    return session


def ingest_agent_sessions(
    source: str | Path,
    db_path: str | Path,
    *,
    limit: int | None = None,
    repo: str | None = None,
):
    """Normalize local agent sessions with @letta-ai/trajectory."""
    try:
        from trajectory import NodeUnavailableError, NormalizationError, normalize_many
    except ImportError as error:
        raise RuntimeError(
            "agent-trajectory is not installed; install the local extra: "
            "pip install 'trace2rules[local]'."
        ) from error

    from ..ingest import IngestStats, write_sessions

    root = Path(source).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"Session source does not exist: {root}")
    if root.is_file() and root.suffix == ".db":
        raise ValueError(
            "agent-trajectory does not read OpenCode SQLite directly; export the "
            "session with `opencode export <session-id>` first."
        )

    if root.is_file():
        detected = _detect_source(root)
        if detected is None:
            raise ValueError(f"Cannot detect agent format from {root}")
        roots = [(detected, root)]
    else:
        roots = _source_roots(root)
        if not roots:
            detected = _detect_source(root)
            if detected is None:
                raise ValueError(
                    f"No known agent session stores under {root}; pass --source with "
                    "a Claude Code, Codex, Pi, or OMP session directory."
                )
            roots = [(detected, root)]

    stats: list[IngestStats] = []
    all_sessions: list[Session] = []
    try:
        for source_name, source_root in roots:
            files = _iter_files(source_root, source_name, limit)
            if not files:
                if source_name == "opencode":
                    raise ValueError(
                        "agent-trajectory does not read OpenCode SQLite directly; "
                        "export sessions with `opencode export <session-id>` first."
                    )
                continue
            transcripts: list[tuple[Path, str]] = [
                (path, path.read_text(encoding="utf-8", errors="replace")) for path in files
            ]
            results: list[dict] = []
            BATCH = 32
            for start in range(0, len(transcripts), BATCH):
                batch = transcripts[start : start + BATCH]
                try:
                    results.extend(
                        normalize_many(
                            [
                                {"source": source_name, "transcript": text}
                                for _, text in batch
                            ]
                        )
                    )
                except NormalizationError:
                    for path, text in batch:
                        try:
                            results.extend(
                                [normalize_many([{"source": source_name, "transcript": text}])[0]]
                            )
                        except NormalizationError as error:
                            print(f"Skipping {path}: {error}")
                            results.append({"records": [], "diagnostics": []})
            for (path, _), result in zip(transcripts, results):
                if not result.get("records"):
                    continue
                all_sessions.append(_records_to_session(path, source_name, result["records"], repo))
            stats.append(
                IngestStats(
                    f"agent-trajectory:{source_name}",
                    len(files),
                    len([s for s in all_sessions if s.agent == source_name]),
                    sum(len(s.calls) for s in all_sessions if s.agent == source_name),
                )
            )
    except NodeUnavailableError as error:
        raise RuntimeError(
            "agent-trajectory requires Node.js 20 or newer on PATH."
        ) from error

    write_sessions(db_path, all_sessions)
    return stats
