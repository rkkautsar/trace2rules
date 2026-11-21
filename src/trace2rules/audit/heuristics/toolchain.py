from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import AuditOutcome

_COMMANDS = re.compile(
    r"(?i)(?:\b(?:cargo|rustc)\s+\+nightly|RUSTC_BOOTSTRAP\s*=|"
    r"\brustup\s+override\b|\bnvm\s+(?:use|install)\b|"
    r"\buv\s+python\s+pin\b|\bpyenv\s+local\b|"
    r"\bgo\s+env\s+-w\s+GOTOOLCHAIN\b|\basdf\s+|\bmise\s+use\b)"
)
_RUST_USE = re.compile(r"(?i)\b(?:cargo|rustc|rustup)\b")
_MSRV = re.compile(r"(?i)\bmsrv\b|\brust-version\b")
_TOOLCHAIN_FILES = {
    ".nvmrc",
    ".python-version",
    "rust-toolchain.toml",
    "rust-toolchain",
    ".tool-versions",
    "mise.toml",
    ".mise.toml",
}


def evaluate(
    rule: Any,
    events: list[tuple],
    edit_events: list[tuple[str, str]],
) -> AuditOutcome | None:
    if _MSRV.search(rule.text):
        # A toolchain file or extra rustc flags do not prove the MSRV API rule.
        return None
    if not any(
        _RUST_USE.search(raw or "")
        for _sid, _step, action, raw, _target, _object, _outcome in events
        if action == "exec"
    ):
        return None
    commands = [
        raw
        for _sid, _step, action, raw, target, _object, _outcome in events
        if action == "exec" and _COMMANDS.search(raw)
    ]
    files = [
        target
        for _step, target in edit_events
        if Path(target).name in _TOOLCHAIN_FILES
    ]
    hits = [*commands, *files]
    negative = bool(re.search(r"(?i)\b(?:never|do not|don't|avoid)\b", rule.text))
    violation = bool(hits) and negative
    return AuditOutcome(
        applicable=1,
        observed=1 if hits else 0,
        compliant=0 if violation else 1,
        violations=1 if violation else 0,
        examples=hits[:2],
    )
