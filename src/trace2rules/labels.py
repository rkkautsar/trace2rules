from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

from .db import DB_PATH, connect
from .redact import redact_text

DEFAULT_MODEL = "clef-flash"
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

MODELS: dict[str, dict[str, str]] = {
    "clef-flash": {
        "name": "clef-flash",
        "provider": "cloudflare",
        "model_id": "@cf/cloudflare/clef-flash",
        "wire_model": "clef-flash",
        "style": "clef",
    },
    "clef": {
        "name": "clef",
        "provider": "cloudflare",
        "model_id": "@cf/cloudflare/clef",
        "wire_model": "clef",
        "style": "clef",
    },
    "jev": {
        "name": "jev",
        "provider": "cloudflare",
        "model_id": "typesafe/jev",
        "wire_model": "typesafe/jev",
        "style": "jev",
    },
    "gemini-decision-1": {
        "name": "gemini-decision-1",
        "provider": "gemini",
        "model_id": "gemini-decision-1",
    },
    "gpt-6-luna": {
        "name": "gpt-6-luna",
        "provider": "openai",
        "model_id": "gpt-6-luna",
    },
}

MODEL_ALIASES = {
    "luna": "gpt-6-luna",
    "gemini": "gemini-decision-1",
    "gemini-decision": "gemini-decision-1",
    "jev-1.13": "jev",
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


def _wrangler_ready() -> bool:
    if not shutil.which("wrangler"):
        return False
    try:
        result = subprocess.run(
            ["wrangler", "whoami", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and '"loggedIn": true' in result.stdout


def detect_model() -> str | None:
    """Pick the cheapest available decision backend without requiring config."""
    configured = os.getenv("TRACE2RULES_DECISION_MODEL")
    if configured:
        return configured
    if _wrangler_ready():
        return "clef-flash"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini-decision-1"
    if os.getenv("OPENAI_API_KEY"):
        return "gpt-6-luna"
    return None


def _resolve_model(name: str | None) -> dict[str, str]:
    selected = (name or os.getenv("TRACE2RULES_DECISION_MODEL") or detect_model() or "").strip()
    if not selected:
        raise RuntimeError(
            "No decision backend found; set GEMINI_API_KEY, OPENAI_API_KEY, "
            "TRACE2RULES_DECISION_MODEL, or install wrangler."
        )
    selected = MODEL_ALIASES.get(selected, selected)
    if selected not in MODELS:
        raise RuntimeError(
            "Unknown decision model "
            f"{selected!r}; choose one of: {', '.join(sorted(MODELS))}."
        )
    return MODELS[selected]


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


def _auth_for(spec: dict[str, str]):
    provider = spec["provider"]
    if provider == "cloudflare":
        return _cloudflare_auth()
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "gemini-decision-1 requires GEMINI_API_KEY or GOOGLE_API_KEY."
            )
        return key
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("gpt-6-luna requires OPENAI_API_KEY.")
        return key
    raise RuntimeError(f"Unsupported decision provider: {provider}")


def _openai_questions() -> list[dict]:
    return [
        {
            "type": "choice",
            "name": name,
            "instructions": question["instructions"],
            "choices": [
                {"value": value, "description": description}
                for value, description in question["criteria"].items()
            ],
        }
        for name, question in QUESTIONS.items()
    ]


def _openai_answers(payload: dict) -> dict:
    answers = payload.get("answers") or []
    return {
        answer.get("name"): {
            "choice": answer.get("choice"),
            "confidence": answer.get("confidence") or 0.0,
        }
        for answer in answers
        if answer.get("name")
    }


def _post(auth, state: dict, spec: dict[str, str]) -> dict:
    provider = spec["provider"]
    if provider == "cloudflare":
        account, token = auth
        if spec["style"] == "jev":
            url = "https://api.cloudflare.com/client/v4/accounts/{}/ai/run".format(account)
            body = {
                "model": spec["wire_model"],
                "input": {"state": state, "questions": QUESTIONS},
            }
        else:
            url = (
                "https://api.cloudflare.com/client/v4/accounts/{}/ai/run/{}".format(
                    account, spec["model_id"]
                )
            )
            body = {
                "model": spec["wire_model"],
                "state": state,
                "questions": QUESTIONS,
            }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        if not payload.get("success"):
            raise RuntimeError(f"{spec['name']} error: {payload.get('errors')}")
        return payload["result"]["answers"]

    if provider == "gemini":
        body = {
            "model": spec["model_id"],
            "input": json.dumps(state, ensure_ascii=False),
            "questions": QUESTIONS,
        }
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/decisions",
            data=json.dumps(body).encode(),
            headers={
                "x-goog-api-key": auth,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        return payload["answers"]

    if provider == "openai":
        body = {
            "model": spec["model_id"],
            "input": json.dumps(state, ensure_ascii=False),
            "questions": _openai_questions(),
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/decisions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {auth}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        return _openai_answers(payload)

    raise RuntimeError(f"Unsupported decision provider: {provider}")


def _retry(auth, state: dict, limiter: RateLimiter, spec: dict[str, str]) -> dict:
    last_error: Exception | None = None
    for attempt in range(6):
        limiter.wait()
        try:
            return _post(auth, state, spec)
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
    raise RuntimeError(
        f"{spec['name']} failed after retries: {last_error}"
    )


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
        "normalized_target": redact_text(target[:500]),
        "raw_example": redact_text((raw_example or "").replace("\x00", " ")[:500]),
    }


def label_targets(
    db_path: str | Path = DB_PATH,
    limit: int | None = None,
    rpm: int = DEFAULT_RPM,
    workers: int = DEFAULT_WORKERS,
    model: str | None = None,
) -> tuple[int, int]:
    con = connect(db_path)
    rows = _pending(con, limit)
    if not rows:
        con.close()
        return 0, 0

    spec = _resolve_model(model)
    auth = _auth_for(spec)
    limiter = RateLimiter(rpm)
    done = 0
    failed = 0
    batch: list[tuple] = []
    started = time.monotonic()

    def work(row: tuple[str, str, str, str]) -> tuple:
        result = _retry(auth, _state(row), limiter, spec)
        intent = result["intent"]
        object_type = result["object_type"]
        return (
            row[0],
            row[1],
            row[2],
            intent["choice"],
            float(intent.get("confidence") or 0.0),
            object_type["choice"],
            float(object_type.get("confidence") or 0.0),
            spec["name"],
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
