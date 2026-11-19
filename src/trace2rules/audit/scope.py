from __future__ import annotations

from pathlib import Path

from .models import AgentRule

_SOURCE_CACHE: dict[Path, list[str]] = {}
_SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
    ".kt", ".cs", ".rb", ".php", ".swift",
}


def _source_index(root: Path) -> list[str]:
    resolved = root.resolve()
    if resolved in _SOURCE_CACHE:
        return _SOURCE_CACHE[resolved]
    paths: list[str] = []
    for path in resolved.rglob("*"):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(resolved)
        if "tests" in relative.parts or "test" in relative.parts:
            continue
        paths.append(relative.as_posix())
    _SOURCE_CACHE[resolved] = paths
    return paths


def _is_test_path(path: str) -> bool:
    normalized = Path(path).as_posix()
    parts = Path(normalized).parts
    name = Path(normalized).name
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _infer_scope(rule: AgentRule, root: Path | None) -> tuple[str, ...]:
    normalized_paths = tuple(Path(path).as_posix() for path in rule.paths)
    explicit = tuple(path for path in normalized_paths if not _is_test_path(path))
    if explicit:
        return explicit
    if root is None or not root.is_dir():
        return ()
    source_paths = _source_index(root)
    inferred: list[str] = []
    for path in normalized_paths:
        if not _is_test_path(path):
            continue
        stem = Path(path).stem
        if stem.startswith("test_"):
            stem = stem[5:]
        elif stem.endswith("_test"):
            stem = stem[:-5]
        test_parts = set(Path(path).parent.parts) - {"tests", "test", "."}
        matches = [source for source in source_paths if Path(source).stem == stem]
        if matches:
            inferred.append(
                max(
                    matches,
                    key=lambda source: (
                        len(test_parts & set(Path(source).parent.parts)),
                        -len(Path(source).parts),
                    ),
                )
            )
    return tuple(dict.fromkeys(inferred))
