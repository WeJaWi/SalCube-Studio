"""Durable submission intents, account aliases, and character accounting."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, cancel, config

_lock = threading.RLock()


class SubmissionUncertain(RuntimeError):
    pass


class UsageLimitReached(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event(event, **fields):
    path = config.OUTPUT_DIR / "ai33_usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": _now(), "event": event, **fields}) + "\n")


def usage():
    # One row per logical intent, so polling and restarts never double-count.
    latest = {}
    try:
        with (config.OUTPUT_DIR / "ai33_usage.jsonl").open() as source:
            for line in source:
                try:
                    row = json.loads(line)
                    previous = latest.get(row["intent"], {})
                    latest[row["intent"]] = {**previous, **row, "at": previous.get("at", row.get("at"))}
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return list(latest.values())


def submit(path: Path, payload: dict, submitter, *, kind: str):
    contract = artifacts.digest(payload)
    with _lock:
        previous = artifacts.read(path)
        if previous.get("input") == contract:
            if previous.get("task_id"):
                return previous["task_id"]
            if previous.get("state") == "submitting":
                raise SubmissionUncertain("AI33 submission needs reconciliation; no duplicate task was submitted")
        cancel.check()
        token = cancel.current()
        job_id = token.job_id if token else path.parent.name
        profile = getattr(token, "profile", "")
        chars = len(str(payload.get("text") or ""))
        rows = usage()
        for field, selected in (
            ("AI33_DAILY_CHARACTER_LIMIT", [r for r in rows if str(r.get("at", ""))[:10] == _now()[:10]]),
            ("AI33_JOB_CHARACTER_LIMIT", [r for r in rows if r.get("job_id") == job_id])):
            cap = max(0, int(config.get(field, "0")))
            spent = sum(int(r.get("characters") or 0) for r in selected if r.get("event") != "rejected")
            if cap and spent + chars > cap:
                raise UsageLimitReached(f"{field} reached; adjust the configured limit before resuming")
        row = {"intent": uuid.uuid4().hex, "input": contract, "state": "submitting",
               "kind": kind, "job_id": job_id, "profile": profile,
               "characters": chars, "at": _now()}
        artifacts.write(path, row)
        _event("reserved", **{k: v for k, v in row.items() if k not in ("input", "state", "at")})
    try:
        task_id = submitter()
    except SubmissionUncertain:
        raise
    except cancel.JobCancelled:
        # A cancellation may have arrived after acceptance; retain intent.
        raise
    except Exception:
        with _lock:
            row["state"] = "rejected"
            artifacts.write(path, row)
            _event("rejected", intent=row["intent"])
        raise
    with _lock:
        row.update(state="submitted", task_id=task_id)
        artifacts.write(path, row)
        meta = artifacts.read(config.OUTPUT_DIR / "ai33_task_accounts.json").get(task_id, {})
        _event("submitted", intent=row["intent"], task_id=task_id,
               account=meta.get("account", "unknown"))
    return task_id


def remember(task_id: str, api_key: str):
    with _lock:
        path = config.OUTPUT_DIR / "ai33_task_accounts.json"
        rows = artifacts.read(path)
        rows[task_id] = {"account": "primary" if api_key == config.AI33_API_KEY else "backup",
                         "fingerprint": artifacts.digest(api_key), "at": _now()}
        artifacts.write(path, rows)


def key_for(task_id: str):
    row = artifacts.read(config.OUTPUT_DIR / "ai33_task_accounts.json").get(task_id)
    if not row:
        return None
    for key in (config.AI33_API_KEY, config.AI33_API_KEY_BACKUP):
        if key and artifacts.digest(key) == row.get("fingerprint"):
            return key
    raise SubmissionUncertain("The account key that created this AI33 task is unavailable; restore it to resume polling")


def complete(task_id: str, data: dict):
    with _lock:
        for row in usage():
            if row.get("task_id") == task_id and row.get("event") != "succeeded":
                # Record only fields explicitly returned by the provider.
                credits = data.get("credits_consumed")
                _event("succeeded", intent=row["intent"], credits_consumed=credits)
                break


def invalidate_definitively_rejected_task(path: Path, task_id: str,
                                          reason: str) -> bool:
    """Allow one new intent after a provider confirmed a task never ran.

    This must only be used for a documented deterministic rejection (such as
    a custom ElevenLabs voice unavailable to that account).  Timeouts and
    generic failures remain submitted so they can never be replayed blindly.
    """
    with _lock:
        row = artifacts.read(path)
        if not row or row.get("task_id") != task_id:
            return False
        row.pop("task_id", None)
        row["state"] = "rejected"
        artifacts.write(path, row)
        _event("rejected", intent=row.get("intent", ""), task_id=task_id,
               reason=str(reason)[:160])
        return True
