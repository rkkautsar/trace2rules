from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

from .db import DB_PATH, connect

MODEL = "@cf/cloudflare/clef-flash"
DEFAULT_RPM = 270
DEFAULT_WORKERS = 6

INTENT_CRITERIA = {
    "explore": "List or inspect the workspace, repo root, or directory structure",
    "read": "Read a specific file",
    "search": "Search for a symbol, pattern, or filename",
    "edit": "Create or modify a file",
    "run_tests": "Run pytest or a specific test file/selector",
    "run_script": "Execute a reproduction, verification, or ad-hoc script",
    "install": "Install the project, dependencies, or a package",
    "env_check": "Probe imports, versions, paths, or environment variables",
    "git": "Inspect or modify git state",
    "other": "None of the above",
}

OBJECT_CRITERIA = {
    "file": "A source, test, or config file",
    "directory": "A directory, package root, or workspace",
    "test_scope": "A test file, test selector, or suite",
    "package": "An installed or local package",
    "module": "An importable Python module",
    "config": "Project configuration",
    "unknown": "Cannot determine",
}

QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "What is the agent trying to accomplish?",
        "criteria": INTENT_CRITERIA,
    },
    "object_type": {
        "type": "choice",
        "instructions": "What kind of object is acted on?",
        "criteria": OBJECT_CRITERIA,
    },
}


class RateLimiter:
    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_slot = time.monotonic()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_slot > now:
                time.sleep(self.next_slot - now)
                now = time.monotonic()
            self.next_slot = max(now, self.next_slot) + self.interval


def _wrangler_json(*args: str) -> dict:
    try:
        output = subprocess.check_output(
            ["wrangler", *args], text=True, stderr=subprocess.PIPE
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "wrangler not found; install it with `npm install -g wrangler`."
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[:300]
        raise RuntimeError(
            "wrangler is not authenticated; run `wrangler login`."
            + (f" ({detail})" if detail else "")
        ) from error
    return json.loads(output)


def _cloudflare_auth() -> tuple[str, str]:
    token = _wrangler_json("auth", "token", "--json")["token"]
    whoami = _wrangler_json("whoami", "--json")
    accounts = whoami.get("accounts") or []
    if not accounts:
        raise RuntimeError("wrangler is logged in but exposes no account")
    return accounts[0]["id"], token


def _post(account: str, token: str, state: dict) -> dict:
    body = json.dumps(
        {"model": "clef-flash", "state": state, "questions": QUESTIONS}
    ).encode()
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    if not payload.get("success"):
        raise RuntimeError(f"clef-flash error: {payload.get('errors')}")
    return payload["result"]


def _retry(account: str, token: str, state: dict, limiter: RateLimiter) -> dict:
    last_error: Exception | None = None
    for attempt in range(6):
        limiter.wait()
        try:
            return _post(account, token, state)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            if error.code == 429 or error.code >= 500:
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2**attempt
                time.sleep(min(delay, 60))
                last_error = RuntimeError(f"HTTP {error.code}: {detail}")
                continue
            raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            time.sleep(2**attempt)
    raise RuntimeError(f"clef-flash failed after retries: {last_error}")


def _pending(
    con: duckdb.DuckDBPyConnection, limit: int | None
) -> list[tuple[str, str, str, str]]:
    query = """
        SELECT e.tool_name, e.action_type, e.target, min(e.raw_target) AS raw_example
        FROM tool_events e
        LEFT JOIN target_labels l
          ON e.tool_name = l.tool_name
         AND e.action_type = l.action_type
         AND e.target = l.target
        WHERE l.tool_name IS NULL
        GROUP BY 1, 2, 3
        ORDER BY count(*) DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return con.execute(query).fetchall()


def _state(row: tuple[str, str, str, str]) -> dict:
    tool_name, action_type, target, raw_example = row
    return {
        "tool": tool_name,
        "action_type": action_type,
        "normalized_target": target[:500],
        "raw_example": (raw_example or "").replace("\x00", " ")[:500],
    }


def label_targets(
    db_path: str | Path = DB_PATH,
    limit: int | None = None,
    rpm: int = DEFAULT_RPM,
    workers: int = DEFAULT_WORKERS,
) -> tuple[int, int]:
    con = connect(db_path)
    rows = _pending(con, limit)
    if not rows:
        con.close()
        return 0, 0

    account, token = _cloudflare_auth()
    limiter = RateLimiter(rpm)
    done = 0
    failed = 0
    batch: list[tuple] = []
    started = time.monotonic()

    def work(row: tuple[str, str, str, str]) -> tuple:
        result = _retry(account, token, _state(row), limiter)
        answers = result["answers"]
        intent = answers["intent"]
        object_type = answers["object_type"]
        return (
            row[0],
            row[1],
            row[2],
            intent["choice"],
            float(intent.get("confidence") or 0.0),
            object_type["choice"],
            float(object_type.get("confidence") or 0.0),
            "clef-flash",
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, row): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    batch.append(future.result())
                    done += 1
                except Exception as error:  # noqa: BLE001 - report per-row failures
                    failed += 1
                    print(f"FAILED {row[0]} {row[2][:80]}: {error}")
                if len(batch) >= 100:
                    con.executemany(
                        "INSERT OR REPLACE INTO target_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
                        batch,
                    )
                    batch.clear()
                if (done + failed) % 100 == 0:
                    elapsed = time.monotonic() - started
                    rate = (done + failed) / elapsed * 60 if elapsed else 0
                    print(
                        f"{done + failed}/{len(rows)}  ok={done} failed={failed}  "
                        f"{rate:.0f}/min",
                        flush=True,
                    )
        if batch:
            con.executemany(
                "INSERT OR REPLACE INTO target_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
                batch,
            )
    finally:
        con.close()
    return done, failed
