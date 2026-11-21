from __future__ import annotations

import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import duckdb

from .core import _representative_window
from .models import Finding

SETUP_RE = re.compile(
    r"(?i)\b(pip3?|uv|poetry|conda|npm|yarn|pnpm|cargo|go|bundle|composer|dotnet|mix)"
    r"\s+(install|add|sync|ci|update|lock|get)\b"
)
ENV_RE = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)=(\S*)")
GENERIC_ERRORS = {"", "text_error", "timeout", "err:unknown"}
WORKTREE_RE = re.compile(r"(?:/workspace|/testbed|/repo|/app)/[^/\s]+")
ENV_PREFIX_RE = re.compile(r"^(?:[A-Z_][A-Z0-9_]*=\S*\s+)+")
ACTIONABLE_ENV = {
    "PYTHONPATH",
    "PATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "LD_LIBRARY_PATH",
    "PYTHONHOME",
}


@dataclass(frozen=True)
class CommandEntity:
    runner: str
    target: str
    selector: str = ""
    cwd: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return self.runner, self.target, self.selector

    @property
    def label(self) -> str:
        parts = [self.runner]
        if self.target:
            parts.append(self.target)
        if self.selector:
            parts.append(self.selector)
        return " ".join(parts)


@dataclass
class Delta:
    kind: str
    command: str


@lru_cache(maxsize=65536)
def _normalize_path(value: str) -> str:
    value = value.strip().strip("'\"")
    value = WORKTREE_RE.sub("<root>", value)
    value = re.sub(r"/home/[^/\s]+/[^/\s]+", "<path>", value)
    value = re.sub(r"/tmp/[^\s]+", "<tmp>", value)
    return value


def _split_command(raw: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*(?:&&|;|\|\|)\s*", raw)
        if part.strip()
    ]


def _strip_env(command: str) -> str:
    while True:
        match = ENV_RE.match(command)
        if not match:
            return command
        command = command[match.end() :].lstrip()
        if command.startswith("&&"):
            command = command[2:].strip()


@lru_cache(maxsize=65536)
def parse_entity(raw: str, target: str = "") -> CommandEntity:
    command = (raw or "").strip()
    cwd = ""
    if "cd " in command:
        cd_match = re.search(r"(?:^|&&|;)\s*cd\s+([^&;]+)", command)
        if cd_match:
            cwd = _normalize_path(cd_match.group(1))
        command = re.sub(r"^(?:cd\s+[^&;]+?\s*&&\s+)+", "", command)
    if "=" in command or command.startswith("export "):
        command = _strip_env(command)
    if not any(char in command for char in ("'", '"', "\\")):
        tokens = command.split()
    else:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
    if not tokens:
        return CommandEntity(runner="", target=_normalize_path(target))
    runner = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    if runner in {"python", "python3", "python3.10", "python3.11", "python3.12"}:
        if args[:1] == ["-m"] and len(args) > 1:
            runner = args[1]
            args = args[2:]
        elif args[:1] == ["-c"]:
            return CommandEntity(runner="python", target="-c", cwd=cwd)
    if runner in {"pip", "pip3"} and args[:1] == ["install"]:
        runner = "pip install"
        args = args[1:]
    positional = [arg for arg in args if not arg.startswith("-")]
    selector = ""
    for index, arg in enumerate(args):
        if arg in {"-k", "--keyword"} and index + 1 < len(args):
            selector = args[index + 1]
        elif "::" in arg:
            selector = arg.split("::", 1)[1][:80]
    target_value = positional[0] if positional else target
    return CommandEntity(
        runner=runner,
        target=_normalize_path(target_value) if target_value else "",
        selector=selector,
        cwd=cwd,
    )


@lru_cache(maxsize=65536)
def _delta(raw: str) -> Delta | None:
    command = _strip_env((raw or "").strip())
    for part in _split_command(command):
        match = SETUP_RE.search(part)
        if match:
            return Delta(kind="dependency", command=_compact_delta(part))
        env_match = ENV_RE.match(part) or re.match(
            r"(?i)^export\s+([A-Z_][A-Z0-9_]*)=(.+)$", part
        )
        if env_match:
            names = {
                name.upper()
                for name in re.findall(
                    r"(?:^|\s)(?:export\s+)?([A-Z_][A-Z0-9_]*)=", part
                )
            }
            if not names or not names <= ACTIONABLE_ENV:
                continue
            return Delta(
                kind="environment",
                command=_compact_delta(part, keep_env=True),
            )
    return None


def _compact_delta(command: str, *, keep_env: bool = False) -> str:
    if not keep_env:
        command = ENV_PREFIX_RE.sub("", command)
    command = re.sub(r"(?i)^export\s+", "", command)
    command = re.sub(r"\s+", " ", command).strip()
    return command[:180]


def _is_specific_error(signature: str) -> bool:
    return bool(signature) and signature not in GENERIC_ERRORS and signature != "err:unknown"


def _iter_sessions(con: duckdb.DuckDBPyConnection) -> Iterable[tuple[str, str, list[tuple]]]:
    columns = {row[1] for row in con.execute("PRAGMA table_info('sessions')").fetchall()}
    instance_expr = "s.instance_id" if "instance_id" in columns else "s.session_id"
    cursor = con.execute(
        f"""
        SELECT e.session_id, s.repo, {instance_expr}, e.step_idx, e.segment_idx,
               e.action_type, e.verb, e.target, e.raw_target, e.outcome,
               e.is_error, e.error_signature, e.approx_out_tokens
        FROM tool_events e
        JOIN sessions s USING (session_id)
        WHERE e.action_type = 'exec' AND coalesce(e.raw_target, '') <> ''
        ORDER BY e.session_id, e.step_idx
        """
    )
    current_session = None
    current_repo = ""
    current_instance = ""
    events: list[tuple] = []
    while True:
        batch = cursor.fetchmany(2048)
        if not batch:
            if current_session is not None:
                yield current_session, current_repo, current_instance, events
            return
        for row in batch:
            session_id, repo, instance_id = row[0], row[1], row[2]
            if current_session is None:
                current_session, current_repo, current_instance = (
                    session_id,
                    repo,
                    instance_id,
                )
            elif session_id != current_session:
                yield current_session, current_repo, current_instance, events
                current_session, current_repo, current_instance = (
                    session_id,
                    repo,
                    instance_id,
                )
                events = []
            events.append(row)


def prevention_deltas(
    con: duckdb.DuckDBPyConnection, limit: int | None = None
) -> list[Finding]:
    """Find setup/environment changes that precede a successful retry.

    A finding requires the same command entity to fail, then succeed after a
    setup or environment delta in the same compaction segment. The delta is the
    rule; the error sequence is only evidence.
    """
    aggregates: dict[tuple[str, str, str], dict] = {}
    controls: dict[tuple[str, tuple[str, str, str], str], set[str]] = defaultdict(set)

    for session_id, repo, instance_id, events in _iter_sessions(con):
        for index, source in enumerate(events):
            if not source[10] or not _is_specific_error(source[11]):
                continue
            if source[5] != "exec":
                continue
            source_entity = parse_entity(source[8] or "", source[7] or "")
            if not source_entity.runner:
                continue
            recovery = None
            delta = None
            for target_index in range(index + 1, min(len(events), index + 7)):
                target = events[target_index]
                if target[5] != "exec" or target[10]:
                    continue
                if target[4] != source[4]:
                    continue
                target_entity = parse_entity(target[8] or "", target[7] or "")
                if target_entity.key != source_entity.key:
                    continue
                between = events[index + 1 : target_index]
                delta = next(
                    (
                        candidate
                        for candidate in (
                            _delta(event[8] or "") for event in reversed(between)
                        )
                        if candidate is not None
                    ),
                    None,
                )
                recovery = target
                break
            if recovery is None:
                continue
            if delta is None:
                controls[(repo, source_entity.key, source[11])].add(session_id)
                continue
            key = (repo, delta.kind, delta.command, source[11])
            aggregate = aggregates.setdefault(
                key,
                {
                    "sessions": set(),
                    "instances": set(),
                    "entities": set(),
                    "runners": set(),
                    "entity_keys": set(),
                    "tokens": 0,
                    "occurrences": 0,
                    "example_session": session_id,
                    "example_step": int(source[3]),
                    "example_recovery_step": int(recovery[3]),
                    "example_source": source[8] or "",
                    "example_target": recovery[8] or "",
                },
            )
            aggregate["sessions"].add(session_id)
            aggregate["instances"].add(instance_id)
            aggregate["entities"].add(source_entity.label)
            aggregate["runners"].add(source_entity.runner)
            aggregate["entity_keys"].add(source_entity.key)
            aggregate["occurrences"] += 1
            aggregate["tokens"] += sum(
                int(event[12] or 0)
                for event in [source, *events[index + 1 : target_index], recovery]
            )

    findings: list[Finding] = []
    for (repo, kind, delta, error), value in aggregates.items():
        sessions = len(value["sessions"])
        instances = len(value["instances"])
        if sessions < 3 or instances < 2:
            continue
        control_sessions: set[str] = set()
        for entity_key in value["entity_keys"]:
            control_sessions |= controls.get((repo, entity_key, error), set())
        control = len(control_sessions)
        confidence = sessions / (sessions + control) if sessions + control else 0.0
        if confidence < 0.5:
            continue
        entities = sorted(value["entities"])
        runners = sorted(value["runners"])
        entity_label = " / ".join(runners[:4]) if runners else "commands"
        findings.append(
            Finding(
                kind="prevention_delta",
                evidence_kind="preventive",
                repo=repo,
                treatment_sessions=sessions,
                control_sessions=control,
                title=f"{delta} before {entity_label}",
                sessions=sessions,
                tokens=int(value["tokens"]),
                paired_instances=instances,
                detail=(
                    f"{kind} delta; recovered after {error}; "
                    f"{sessions} sessions / {instances} instances; "
                    f"entities {len(value['entities'])}; "
                    f"no-delta recoveries {control}; confidence {confidence:.0%}"
                ),
                failure_family=error,
                cause=(
                    f"{value['example_source'][:200]} -> {delta} -> "
                    f"{value['example_target'][:200]}"
                ),
                recommendation=(
                    f"Before {entity_label}, run `{delta}`. "
                    f"It preceded a successful retry in {sessions} sessions "
                    f"({instances} instances); do not rediscover it per session."
                ),
                side="repo" if kind in {"dependency", "environment"} else "agent",
                window=_representative_window(
                    con,
                    value["example_session"],
                    value["example_step"],
                    value["example_recovery_step"],
                    include_scratch=True,
                ),
            )
        )
    findings.sort(key=lambda finding: (finding.sessions, finding.tokens), reverse=True)
    return findings[:limit] if limit is not None else findings
