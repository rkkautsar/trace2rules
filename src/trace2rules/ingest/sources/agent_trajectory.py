from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from rich.progress import Progress

from ...feedback import constraint_keys
from ...models import Feedback, Session, ToolCall
from .common import classify_tool_call, parse_arguments, target_from_args

SOURCE_DIRS = {
    "claude-code": ("claude_code", ".claude/projects"),
    "codex": ("codex", ".codex/sessions"),
    "pi": ("pi", ".pi/agent/sessions"),
    "omp": ("omp", ".omp/agent/sessions"),
    "opencode": ("opencode", ""),
}

TOOL_CALL_TYPES = {
    "tool_use",
    "toolCall",
    "tool_call",
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "web_search_call",
    "mcp_tool_call",
}


@dataclass(slots=True)
class TranscriptMeta:
    """Source-neutral session/episode metadata extracted from raw JSONL."""

    session_id: str = ""
    parent_session_id: str = ""
    instance_id: str = ""
    title: str = ""
    is_continuation: bool = False
    segment_of_call: dict[str, int] = field(default_factory=dict)
    segments: int = 1


def _child_meta(row: dict, *keys: str) -> str:
    value: object = row
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) else ""


def _is_compaction(row: dict, source: str) -> bool:
    if source == "codex":
        return row.get("type") == "response_item" and _child_meta(
            row, "payload", "type"
        ) == "compaction"
    if source in {"omp", "pi"}:
        return row.get("type") == "compaction"
    if source == "claude-code":
        message = row.get("message") or {}
        return (
            row.get("type") in {"summary", "compaction"}
            or row.get("isCompactSummary") is True
            or message.get("isCompactSummary") is True
        )
    return str(row.get("type", "")).lower().startswith("compact")


def _walk_tool_ids(value: object):
    if isinstance(value, dict):
        if value.get("type") in TOOL_CALL_TYPES:
            call_id = value.get("id") or value.get("call_id")
            if isinstance(call_id, str) and call_id:
                yield call_id
        for child in value.values():
            yield from _walk_tool_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_tool_ids(child)


def _tool_ids(row: dict, source: str) -> tuple[str, ...]:
    if source in {"omp", "pi"}:
        message = row.get("message") or {}
        if message.get("role") != "assistant":
            return ()
        content = message.get("content")
        if not isinstance(content, list):
            return ()
        return tuple(
            str(part["id"])
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"toolCall", "tool_call"}
            and part.get("id")
        )
    if source == "codex":
        payload = row.get("payload") or {}
        if payload.get("type") not in TOOL_CALL_TYPES:
            return ()
        call_id = payload.get("id") or payload.get("call_id")
        return (str(call_id),) if call_id else ()
    if source == "claude-code":
        message = row.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return ()
        return tuple(
            str(part["id"])
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"tool_use", "toolCall", "tool_call"}
            and part.get("id")
        )
    return tuple(_walk_tool_ids(row))


def _relevant_line(line: str, source: str) -> bool:
    if source in {"omp", "pi"}:
        return any(
            marker in line
            for marker in (
                '"type":"session"',
                '"type": "session"',
                '"type":"compaction"',
                '"type": "compaction"',
                '"toolCall"',
                '"tool_call"',
            )
        )
    if source == "codex":
        return any(
            marker in line
            for marker in (
                '"type":"session_meta"',
                '"type": "session_meta"',
                '"type":"compaction"',
                '"type": "compaction"',
                '"function_call"',
                '"custom_tool_call"',
                '"local_shell_call"',
                '"web_search_call"',
                '"mcp_tool_call"',
            )
        )
    if source == "claude-code":
        return any(
            marker in line
            for marker in (
                '"sessionId"',
                '"tool_use"',
                '"type":"summary"',
                '"type": "summary"',
                '"isCompactSummary"',
                '"compaction"',
            )
        )
    return True


def extract_transcript_meta(path: Path, source: str, text: str) -> TranscriptMeta:
    """Extract header id, parent id, and compaction episode boundaries.

    The trajectory normalizer drops source-specific boundary records, so this
    pre-pass runs over the same raw transcript before normalization.
    """
    meta = TranscriptMeta()
    segment = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if not _relevant_line(line, source):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue

        if source == "codex" and row.get("type") == "session_meta":
            payload = row.get("payload") or {}
            meta.session_id = str(payload.get("id") or payload.get("session_id") or "")
            meta.parent_session_id = str(
                payload.get("parent_thread_id")
                or _child_meta(payload, "source", "subagent", "thread_spawn", "parent_thread_id")
                or ""
            )
            meta.title = str(payload.get("title") or "")
        elif source in {"omp", "pi"} and row.get("type") == "session":
            meta.session_id = str(row.get("id") or "")
            meta.parent_session_id = str(
                row.get("parentSession") or row.get("continuationOf") or ""
            )
            meta.is_continuation = bool(row.get("continuationOf"))
            meta.title = str(row.get("title") or "")
        elif source == "claude-code":
            meta.session_id = meta.session_id or str(
                row.get("sessionId") or row.get("session_id") or ""
            )
            meta.parent_session_id = meta.parent_session_id or str(
                row.get("parentSessionId") or row.get("parent_session_id") or ""
            )
            meta.title = meta.title or str(row.get("title") or "")

        if _is_compaction(row, source):
            segment += 1
            continue
        for call_id in _tool_ids(row, source):
            meta.segment_of_call[call_id] = segment

    meta.segments = segment + 1
    return meta


def _scan_transcript(path: Path, source: str):
    """Return the decoded transcript so normalization does not read it again."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return path, None, None, error
    return path, extract_transcript_meta(path, source, text), text, None


# Cap on decoded transcripts kept for the normalize pass. Scanning has to finish
# before normalization (session ids are assigned across the whole source root),
# so retaining every fresh transcript would hold the entire corpus in memory.
# Texts are cached in scan order up to this budget (counted in characters, a
# close enough proxy for bytes on these transcripts) and reused by the batches
# that consume the same prefix; anything beyond it is read a second time.
TEXT_CACHE_BYTES = 128 * 1024 * 1024


def _normalize_bisect(
    normalize_many,
    source: str,
    bounds: dict,
    items: list[tuple[int, Path, str]],
) -> dict[int, dict]:
    """Normalize ``items``, bisecting failing requests down to single files.

    A malformed or oversized transcript can crash the Node runtime. Splitting
    the batch in half isolates it in O(log n) requests instead of retrying every
    file individually. Results stay keyed by the caller's index.
    """
    results: dict[int, dict] = {}

    def attempt(chunk: list[tuple[int, Path, str]]) -> None:
        try:
            normalized = list(
                normalize_many(
                    [
                        {"source": source, "transcript": text, "bounds": bounds}
                        for _index, _path, text in chunk
                    ]
                )
            )
        except Exception as error:
            if len(chunk) == 1:
                index, path, _text = chunk[0]
                print(f"Skipping {path}: {error}")
                results[index] = {"records": [], "diagnostics": []}
                return
            middle = len(chunk) // 2
            attempt(chunk[:middle])
            attempt(chunk[middle:])
            return
        for (index, _path, _text), result in zip(chunk, normalized):
            results[index] = result

    if items:
        attempt(items)
    return results


def _normalize_batch(
    source: str,
    bounds: dict,
    batch: list[tuple[Path, TranscriptMeta]],
    cache: dict[Path, str],
    normalize_many,
) -> list[dict]:
    """Normalize one batch, reusing texts decoded during the scan pass.

    Returns one entry per batch item so callers can zip it against the batch.
    """
    results = [{"records": [], "diagnostics": []} for _ in batch]
    items: list[tuple[int, Path, str]] = []
    for index, (path, _meta) in enumerate(batch):
        text = cache.pop(path, None)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                print(f"Skipping {path}: {error}")
                continue
        items.append((index, path, text))
    for index, result in _normalize_bisect(normalize_many, source, bounds, items).items():
        results[index] = result
    return results


def _assign_unique_session_ids(
    transcript_metas: list[tuple[Path, TranscriptMeta]],
) -> None:
    """Keep duplicate source session IDs from collapsing separate transcripts."""
    used: set[str] = set()
    for path, meta in transcript_metas:
        base = meta.session_id
        if base not in used:
            used.add(base)
            continue
        digest = hashlib.sha1(str(path).encode()).hexdigest()[:8]
        candidate = f"{base}#{digest}"
        suffix = 2
        while candidate in used:
            candidate = f"{base}#{digest}-{suffix}"
            suffix += 1
        meta.session_id = candidate
        used.add(candidate)


def _normalize_parent(value: str) -> str:
    if not value:
        return ""
    if value.endswith(".jsonl"):
        return Path(value).stem.split("_")[-1].removesuffix(".jsonl")
    return value


def _resolve_instance(
    session_id: str,
    metas: dict[str, TranscriptMeta],
    seen: tuple[str, ...] = (),
) -> str:
    meta = metas.get(session_id)
    if meta is None:
        return session_id
    parent = _normalize_parent(meta.parent_session_id)
    if not parent or parent not in metas or parent in seen:
        return session_id
    if meta.is_continuation and metas[parent].title != meta.title:
        return session_id
    return _resolve_instance(parent, metas, seen + (session_id,))


def _detect_source(path: Path) -> str | None:
    if path.is_file() and path.suffix == ".db":
        return "opencode"
    if path.is_dir() and any(path.glob("opencode*.db")):
        return "opencode"
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


WINDOWS_AGENT_STORES = (
    (".codex/sessions", "codex"),
    (".claude/projects", "claude-code"),
    (".pi/agent/sessions", "pi"),
    (".local/share/opencode", "opencode"),
    ("AppData/Roaming/opencode", "opencode"),
    ("AppData/Local/opencode", "opencode"),
)


def _append_root(
    roots: list[tuple[str, Path]],
    seen: set[Path],
    source: str,
    candidate: Path,
) -> None:
    try:
        exists = candidate.exists()
    except OSError:
        return
    if exists and candidate not in seen:
        seen.add(candidate)
        roots.append((source, candidate))


def _windows_stores_enabled(root: Path) -> bool:
    setting = os.environ.get("TRACE2RULES_WINDOWS_STORES", "").lower()
    if setting in {"0", "false", "no"}:
        return False
    if setting in {"1", "true", "yes"}:
        return True
    if root.resolve() != Path.home().resolve() or not Path("/mnt/c/Users").is_dir():
        return False
    try:
        version = Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "microsoft" in version or "wsl" in version


def _source_roots(root: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    opencode = root / ".local" / "share" / "opencode"
    _append_root(roots, seen, "opencode", opencode)
    for relative, source in (
        ("AppData/Roaming/opencode", "opencode"),
        ("AppData/Local/opencode", "opencode"),
    ):
        _append_root(roots, seen, source, root / relative)
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
            _append_root(roots, seen, source, candidate)
    if _windows_stores_enabled(root):
        for user in sorted(Path("/mnt/c/Users").glob("*")):
            for relative, source in WINDOWS_AGENT_STORES:
                _append_root(roots, seen, source, user / relative)
    return roots


def _iter_files(root: Path, source: str, limit: int | None) -> list[Path]:
    if root.is_file():
        return [root]
    suffix = ".json" if source == "opencode" else ".jsonl"
    skip = {"changes.jsonl"}
    files = [
        path
        for path in root.rglob(f"*{suffix}")
        if path.is_file() and path.name not in skip
    ]
    if limit:
        files.sort(
            key=lambda path: (path.stat().st_mtime, path.as_posix()),
            reverse=True,
        )
        return files[:limit]
    return files


@lru_cache(maxsize=512)
def _repo_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    path = Path(cwd)
    root = path
    try:
        root = Path(
            subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        remote = subprocess.check_output(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        remote = ""
    if remote:
        value = remote.removesuffix(".git")
        if "://" in value:
            value = urlparse(value).path
        elif "@" in value and ":" in value:
            value = value.split(":", 1)[1]
        parts = [part for part in re.split(r"[/:]", value) if part]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
    return root.name or path.name


def _resolve_repos(cwds: Iterable[str | None]) -> dict[str, str]:
    """Resolve each distinct cwd to a repo label once, in parallel (git is fork-bound)."""
    unique = sorted({cwd for cwd in cwds if cwd})
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        resolved = pool.map(_repo_from_cwd, unique)
        return dict(zip(unique, resolved))


def _session_id(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    return f"{path.stem}-{digest}"


def _records_to_session(
    path: Path,
    source: str,
    records: list[dict],
    repo_override: str | None,
    *,
    meta: TranscriptMeta | None = None,
    repos: dict[str, str] | None = None,
) -> Session:
    meta_record = next((record for record in records if record.get("role") == "meta"), {})
    cwd = meta_record.get("cwd")
    repo = repo_override
    if not repo:
        repo = (repos or {}).get(cwd or "") or _repo_from_cwd(cwd) or "unknown"
    session_id = (meta.session_id if meta and meta.session_id else _session_id(path))
    started_at = None
    for record in records:
        for key in ("timestamp", "created_at", "createdAt", "time"):
            value = record.get(key)
            if value:
                try:
                    started_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if started_at.tzinfo is not None:
                        started_at = started_at.astimezone(timezone.utc).replace(tzinfo=None)
                    break
                except ValueError:
                    continue
        if started_at:
            break
    session = Session(
        session_id=session_id,
        repo=repo,
        instance_id=(meta.instance_id if meta and meta.instance_id else session_id),
        source="agent-trajectory",
        source_ref=str(path),
        agent=source,
        started_at=started_at,
        cwd=str(meta_record.get("cwd") or ""),
    )
    pending: dict[str, ToolCall] = {}
    final_assistant_text = ""
    for record in records:
        role = record.get("role")
        assistant_text = ""
        if role == "assistant":
            content = record.get("content")
            if isinstance(content, list):
                assistant_text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                assistant_text = str(content or "")
            assistant_text = assistant_text.strip()[:800]
            if assistant_text:
                final_assistant_text = assistant_text
        if role == "user":
            content = record.get("content")
            if isinstance(content, list):
                text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = str(content or "")
            text = text.strip()
            if text and not text.startswith(("<system-reminder>", "<user>")):
                keys = constraint_keys(text)
                session.feedback.append(
                    Feedback(
                        text=text,
                        step_idx=len(session.calls),
                        kind="constraint" if keys else "feedback",
                        keys=keys,
                    )
                )
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
                    segment_idx=(
                        meta.segment_of_call.get(tool_call.get("id") or "", 0)
                        if meta
                        else 0
                    ),
                    assistant_context=assistant_text,
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
    if session.calls and final_assistant_text:
        session.calls[-1].assistant_context = final_assistant_text
    return session


def ingest_agent_sessions(
    source: str | Path,
    db_path: str | Path,
    *,
    limit: int | None = None,
    repo: str | None = None,
    progress: Progress | None = None,
    split_compactions: bool = True,
):
    """Normalize local agent sessions with @letta-ai/trajectory."""
    try:
        from trajectory import NodeUnavailableError, NormalizationError, normalize_many
    except ImportError as error:
        raise RuntimeError(
            "agent-trajectory is not installed; install the local extra: "
            "pip install 'trace2rules[local]'."
        ) from error

    from ..normalize import IngestStats
    from ..writer import SessionWriter

    root = Path(source).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"Session source does not exist: {root}")
    if (root.is_file() and root.suffix == ".db") or (
        root.is_dir() and any(root.glob("opencode*.db"))
    ):
        from .opencode import ingest_opencode

        return ingest_opencode(
            root, db_path, limit=limit, repo=repo, split_compactions=split_compactions
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
    seen_session_ids: set[str] = set()
    bounds = {"toolResults": {"maxCharacters": 20000, "strategy": "head-tail"}}
    try:
        for source_name, source_root in roots:
            if source_name == "opencode" and (
                (source_root.is_file() and source_root.suffix == ".db")
                or (source_root.is_dir() and any(source_root.glob("opencode*.db")))
            ):
                from .opencode import ingest_opencode

                stats.extend(
                    ingest_opencode(
                        source_root,
                        db_path,
                        limit=limit,
                        repo=repo,
                        split_compactions=split_compactions,
                    )
                )
                continue
            files = _iter_files(source_root, source_name, limit)
            if not files:
                continue
            metas_by_id: dict[str, TranscriptMeta] = {}
            transcript_metas: list[tuple[Path, TranscriptMeta]] = []
            from ...db import connect as _connect

            manifest_con = _connect(db_path)
            try:
                manifest = {
                    row[0]: (row[1], row[2], row[3])
                    for row in manifest_con.execute(
                        "SELECT source_ref, mtime_ns, size, split_compactions FROM ingest_files"
                    ).fetchall()
                }
                ingested_refs = {
                    row[0]
                    for row in manifest_con.execute(
                        "SELECT DISTINCT source_ref FROM sessions WHERE source_ref <> ''"
                    ).fetchall()
                }
            finally:
                manifest_con.close()
            selected_paths: list[Path] = []
            fresh_paths: list[Path] = []
            scan_task = (
                progress.add_task(f"Scanning {source_name}", total=len(files))
                if progress
                else None
            )
            # Decide from stat + manifest before reading the transcript. The
            # metadata scan used to run for every cached file and was discarded.
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                selected_paths.append(path)
                cached = manifest.get(str(path))
                if (
                    cached
                    and cached[0] == stat.st_mtime_ns
                    and cached[1] == stat.st_size
                    and cached[2] == split_compactions
                    and str(path) in ingested_refs
                ):
                    if progress and scan_task is not None:
                        progress.update(scan_task, advance=1)
                    continue
                fresh_paths.append(path)
            text_cache: dict[Path, str] = {}
            cached_bytes = 0
            if fresh_paths:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    # Bound in-flight submissions: map() would let a fast worker
                    # decode every later file while the first result is pending.
                    pending = [
                        pool.submit(_scan_transcript, path, source_name)
                        for path in fresh_paths[:2]
                    ]
                    next_index = len(pending)
                    while pending:
                        path, meta, text, error = pending.pop(0).result()
                        if next_index < len(fresh_paths):
                            pending.append(
                                pool.submit(
                                    _scan_transcript,
                                    fresh_paths[next_index],
                                    source_name,
                                )
                            )
                            next_index += 1
                        if error is not None:
                            print(f"Skipping {path}: {error}")
                            if progress and scan_task is not None:
                                progress.update(scan_task, advance=1)
                            continue
                        meta.session_id = meta.session_id or _session_id(path)
                        if meta.session_id not in metas_by_id:
                            metas_by_id[meta.session_id] = meta
                        transcript_metas.append((path, meta))
                        if (
                            text is not None
                            and cached_bytes + len(text) <= TEXT_CACHE_BYTES
                        ):
                            text_cache[path] = text
                            cached_bytes += len(text)
                        if progress and scan_task is not None:
                            progress.update(scan_task, advance=1)
            for meta in metas_by_id.values():
                meta.instance_id = _resolve_instance(meta.session_id, metas_by_id)
            _assign_unique_session_ids(transcript_metas)

            task_id = (
                progress.add_task(
                    f"Ingesting {source_name}", total=len(transcript_metas)
                )
                if progress
                else None
            )
            source_sessions = 0
            source_calls = 0

            def normalize_batch(batch):
                return _normalize_batch(
                    source_name, bounds, batch, text_cache, normalize_many
                )

            BATCH_BYTES = 8 * 1024 * 1024
            batches: list[list[tuple[Path, TranscriptMeta]]] = []
            current: list[tuple[Path, TranscriptMeta]] = []
            current_bytes = 0
            for item in transcript_metas:
                size = item[0].stat().st_size
                if current and current_bytes + size > BATCH_BYTES:
                    batches.append(current)
                    current = []
                    current_bytes = 0
                current.append(item)
                current_bytes += size
            if current:
                batches.append(current)
            workers = min(2, len(batches)) if batches else 1
            chunk: list[Session] = []

            def consume(batch, results) -> None:
                nonlocal chunk, source_sessions, source_calls
                usable = [
                    (path, meta, result["records"])
                    for (path, meta), result in zip(batch, results)
                    if result.get("records")
                ]
                repos: dict[str, str] = {}
                if repo is None and usable:
                    cwds = [
                        cwd
                        for _path, _meta, records in usable
                        if (
                            cwd := next(
                                (
                                    record.get("cwd")
                                    for record in records
                                    if record.get("role") == "meta"
                                ),
                                None,
                            )
                        )
                    ]
                    repos = _resolve_repos(cwds)
                for path, meta, records in usable:
                    session = _records_to_session(
                        path, source_name, records, repo, meta=meta, repos=repos
                    )
                    if session.session_id in seen_session_ids:
                        continue
                    seen_session_ids.add(session.session_id)
                    chunk.append(session)
                if len(chunk) >= 64:
                    written = writer.write(chunk)
                    source_sessions += written.sessions
                    source_calls += written.events
                    chunk = []
                if progress and task_id is not None:
                    progress.update(task_id, advance=len(batch))

            writer = SessionWriter(
                db_path, split_compactions=split_compactions
            )
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    pending: list[tuple] = []
                    for batch in batches:
                        pending.append((batch, pool.submit(normalize_batch, batch)))
                        if len(pending) >= max(2, workers * 2):
                            ready_batch, ready_future = pending.pop(0)
                            consume(ready_batch, ready_future.result())
                    for ready_batch, ready_future in pending:
                        consume(ready_batch, ready_future.result())
                if chunk:
                        written = writer.write(chunk)
                        source_sessions += written.sessions
                        source_calls += written.events
            finally:
                writer.close()
            stats.append(
                IngestStats(
                    f"agent-trajectory:{source_name}",
                    len(files),
                    source_sessions,
                    source_calls,
                )
            )
            manifest_con = _connect(db_path)
            try:
                rows = []
                for path in selected_paths:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    rows.append(
                        [str(path), stat.st_mtime_ns, stat.st_size, split_compactions]
                    )
                if rows:
                    manifest_con.execute("BEGIN TRANSACTION")
                    try:
                        manifest_con.executemany(
                            "INSERT OR REPLACE INTO ingest_files VALUES (?, ?, ?, ?)",
                            rows,
                        )
                        manifest_con.execute("COMMIT")
                    except Exception:
                        manifest_con.execute("ROLLBACK")
                        raise
            finally:
                manifest_con.close()
    except NodeUnavailableError as error:
        raise RuntimeError(
            "agent-trajectory requires Node.js 20 or newer on PATH."
        ) from error

    return stats
