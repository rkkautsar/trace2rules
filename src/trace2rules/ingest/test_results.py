from __future__ import annotations

import re

TEST_COMMAND_RE = re.compile(
    r"(?i)(pytest|py\.test|unittest|tox|nox|npm (run )?test|yarn test|pnpm test|"
    r"vitest|jest|cargo test|go test|dotnet test|rspec|phpunit|mix test|"
    r"swift test|mvn test|gradle test)"
)
COUNT_RE = re.compile(r"(?i)(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)")
CARGO_RE = re.compile(
    r"(?i)test result:\s*(?:ok|FAILED)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed;"
    r"\s*(\d+)\s+ignored"
)
JEST_RE = re.compile(
    r"(?i)Tests:\s*(?:(\d+)\s+failed,\s*)?(?:(\d+)\s+skipped,\s*)?"
    r"(?:(\d+)\s+passed,\s*)?(\d+)\s+total"
)


def parse_test_result(command: str, observation: str) -> tuple[int, int, int, int, int] | None:
    """Return (total, passed, failed, errors, skipped) from a test run."""
    if not TEST_COMMAND_RE.search(command) or not observation:
        return None
    text = observation[-12000:]
    if re.search(r"(?i)no tests ran|collected 0 items", text):
        return (0, 0, 0, 0, 0)
    jest = JEST_RE.search(text)
    if jest:
        failed = int(jest.group(1) or 0)
        skipped = int(jest.group(2) or 0)
        passed = int(jest.group(3) or 0)
        total = int(jest.group(4) or 0)
        return (total, passed, failed, 0, skipped)
    cargo = CARGO_RE.search(text)
    if cargo:
        passed, failed, ignored = (int(cargo.group(i)) for i in (1, 2, 3))
        return (passed + failed + ignored, passed, failed, 0, ignored)
    counts: dict[str, int] = {}
    for value, label in COUNT_RE.findall(text):
        label = label.lower()
        if label == "error":
            label = "errors"
        counts[label] = max(counts.get(label, 0), int(value))
    if not counts:
        return None
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("errors", 0)
    skipped = counts.get("skipped", 0)
    total = passed + failed + errors + skipped
    return (total, passed, failed, errors, skipped)
