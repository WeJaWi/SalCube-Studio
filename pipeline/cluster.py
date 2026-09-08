"""Persistent, bounded event and job history for the SouthCube controller."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts
from .node_monitor import sanitize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClusterHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        data = artifacts.read(self.path)
        self._nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
        self._jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        self._events = data.get("events") if isinstance(data.get("events"), list) else []
        self._change_ids = set(str(x) for x in (data.get("change_ids") or [])[-1000:])
        self._last_write = 0.0

    def _event(self, worker_id: str, kind: str, severity: str, message: str,
               job_id: str | None = None) -> None:
        stamp = _now()
        raw = f"{stamp}:{worker_id}:{kind}:{job_id or ''}:{message}:{len(self._events)}"
        event = {"id": hashlib.sha256(raw.encode()).hexdigest()[:18], "at": stamp,
                 "worker_id": worker_id, "kind": kind, "severity": severity,
                 "message": str(message)[:500]}
        if job_id:
            event["job_id"] = job_id
        self._events.append(event)
        self._events = self._events[-500:]

    @staticmethod
    def _node_record(node: dict) -> dict:
        keep = {key: node.get(key) for key in (
            "worker_id", "name", "role", "online", "stale", "health",
            "last_seen", "age_seconds", "hardware", "metrics", "capacity",
            "policy", "node", "agent")}
        return sanitize(keep)

    @staticmethod
    def _job_record(job: dict) -> dict:
        return sanitize({key: job.get(key) for key in (
            "job_id", "title", "profile", "worker_id", "status", "step",
            "progress_pct", "updated_at", "created_at", "error", "recent_events")})

    def known_workers(self) -> list[dict]:
        with self._lock:
            return [json.loads(json.dumps(row)) for wid, row in self._nodes.items()
                    if wid != "main"]

    def observe(self, nodes: list[dict], jobs: list[dict]) -> dict:
        """Record meaningful transitions and return newest-first history."""
        changed = False
        with self._lock:
            initial_job_baseline = not self._jobs
            seen = set()
            for raw in nodes:
                if not isinstance(raw, dict):
                    continue
                node = self._node_record(raw)
                wid = str(node.get("worker_id") or "")[:120]
                if not wid:
                    continue
                seen.add(wid)
                before = self._nodes.get(wid)
                if before is None:
                    self._event(wid, "node_connected", "info",
                                f"{node.get('name') or wid} joined the cluster")
                    changed = True
                elif bool(before.get("online")) != bool(node.get("online")):
                    if node.get("online"):
                        self._event(wid, "node_online", "info",
                                    f"{node.get('name') or wid} is online")
                    else:
                        self._event(wid, "heartbeat_lost", "warning",
                                    f"Heartbeat lost from {node.get('name') or wid}; the node may be stopped or unreachable")
                    changed = True
                old_instance = ((before or {}).get("node") or {}).get("instance_id")
                new_instance = (node.get("node") or {}).get("instance_id")
                if before and old_instance and new_instance and old_instance != new_instance:
                    self._event(wid, "node_restarted", "info",
                                f"{node.get('name') or wid} started a new app instance")
                    changed = True
                for item in ((node.get("node") or {}).get("changes") or []):
                    if not isinstance(item, dict):
                        continue
                    change_id = str(item.get("id") or "")
                    if not change_id or change_id in self._change_ids:
                        continue
                    self._change_ids.add(change_id)
                    self._event(wid, "file_change", "info",
                                str(item.get("message") or "A monitored file changed")[:500])
                    changed = True
                if before != node:
                    self._nodes[wid] = node

            # A previously known worker omitted from a current controller view
            # is retained as history, but never presented as dispatch capacity.
            for wid, before in list(self._nodes.items()):
                if wid == "main" or wid in seen or not before.get("online"):
                    continue
                offline = {**before, "online": False, "stale": True,
                           "health": "offline", "age_seconds": None,
                           "capacity": {**(before.get("capacity") or {}),
                                        "free": 0, "effective_free": 0}}
                self._nodes[wid] = offline
                self._event(wid, "heartbeat_lost", "warning",
                            f"Heartbeat lost from {before.get('name') or wid}; the node may be stopped or unreachable")
                changed = True

            for raw in jobs:
                if not isinstance(raw, dict):
                    continue
                job = self._job_record(raw)
                jid = str(job.get("job_id") or "")[:120]
                if not jid:
                    continue
                before = self._jobs.get(jid)
                if before is None and not initial_job_baseline:
                    self._event(str(job.get("worker_id") or "main"), "job_seen", "info",
                                f"Job added: {job.get('title') or jid}", jid)
                    changed = True
                elif before and (before.get("status"), before.get("worker_id")) != (
                        job.get("status"), job.get("worker_id")):
                    status = str(job.get("status") or "unknown")
                    severity = "warning" if status in ("error", "retry_wait") else "info"
                    self._event(str(job.get("worker_id") or "main"), "job_status",
                                severity, f"Job {jid} is {status.replace('_', ' ')}", jid)
                    changed = True
                self._jobs[jid] = job
            if len(self._jobs) > 500:
                ordered = sorted(self._jobs.values(), key=lambda row: str(
                    row.get("updated_at") or row.get("created_at") or ""), reverse=True)[:500]
                self._jobs = {str(row["job_id"]): row for row in ordered}
                changed = True
            if len(self._change_ids) > 1000:
                self._change_ids = set(list(self._change_ids)[-1000:])

            now_mono = time.monotonic()
            if changed or now_mono - self._last_write >= 30:
                artifacts.write(self.path, {"version": 1, "updated_at": _now(),
                    "nodes": self._nodes, "jobs": self._jobs,
                    "events": self._events[-500:],
                    "change_ids": list(self._change_ids)[-1000:]})
                self._last_write = now_mono
            return {
                "events": list(reversed(self._events[-100:])),
                "job_history": sorted(self._jobs.values(), key=lambda row: str(
                    row.get("updated_at") or row.get("created_at") or ""), reverse=True)[:200],
            }
