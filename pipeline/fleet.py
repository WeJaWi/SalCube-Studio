"""Worker-side polling client for the private Dmiqwi render fleet."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import threading
import time
import traceback
from typing import Any, Callable, Dict

import requests

from . import config


class FleetReporter:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._lock = threading.Lock()
        self._approval = threading.Event()
        self._approval_data: dict = {}
        self._state: dict = {"job_id": job_id, "status": "running",
                             "step": "start", "progress_pct": 0}

    def progress(self, step: str, message: str, pct: int) -> None:
        with self._lock:
            self._state.update(status="running", step=step,
                               message=message, progress_pct=pct)

    def await_approval(self, hooks: dict, outline: str) -> tuple[str, str]:
        with self._lock:
            self._state.update(status="awaiting_approval", step="awaiting",
                               awaiting={"hooks": hooks, "outline": outline})
            self._approval.clear()
        self._approval.wait()
        with self._lock:
            approval = dict(self._approval_data)
            self._state.pop("awaiting", None)
            self._state.update(status="running", step="outline")
        hook = approval.get("hook_text") or hooks.get(
            approval.get("hook_type", "bold_statement"), next(iter(hooks.values())))
        return hook, approval.get("outline") or outline

    def approve(self, approval: dict) -> None:
        with self._lock:
            self._approval_data = dict(approval)
            self._approval.set()

    def complete(self, drive: dict) -> None:
        with self._lock:
            self._state.update(status="done", step="done", progress_pct=100,
                               drive=drive)

    def fail(self, error: str) -> None:
        with self._lock:
            self._state.update(status="error", step="error", error=error)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)


class FleetWorker:
    """Poll the main over Tailscale and run complete dispatched jobs."""
    def __init__(self, runner: Callable[[dict, FleetReporter], None],
                 aborter: Callable[[str], None] | None = None):
        self.runner = runner
        # Called when main asks for a dispatched job to be stopped. Without
        # it, a Stop on main can't reach work running on this machine —
        # main has no ffmpeg of its own to kill for a remote job.
        self.aborter = aborter
        self.worker_id = config.FLEET_WORKER_ID or socket.gethostname()
        self.name = config.FLEET_WORKER_NAME or socket.gethostname()
        self._jobs: Dict[str, FleetReporter] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.monotonic()
        # Controller policy is mirrored locally as a second safety rail. An
        # older controller can omit it and the worker retains legacy active
        # behavior; a current controller can drain/pause or lower capacity
        # without restarting this process.
        self._policy: dict = {"mode": "active", "max_jobs": None}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="dmiqwi-fleet-heartbeat")
            self._thread.start()

    def _payload(self) -> dict:
        with self._lock:
            jobs = [r.snapshot() for r in self._jobs.values()]
            policy = dict(self._policy)
        active = sum(
            1 for j in jobs
            if j["status"] in ("running", "awaiting_approval")
        )
        physical_max = max(1, min(8, int(config.FLEET_MAX_JOBS)))
        try:
            override = (int(policy.get("max_jobs"))
                        if policy.get("max_jobs") is not None else None)
        except (TypeError, ValueError):
            override = None
        effective_max = (min(physical_max, max(1, override))
                         if override is not None else physical_max)
        accepting = policy.get("mode") == "active"
        try:
            disk = shutil.disk_usage(config.OUTPUT_DIR)
            disk_metrics = {
                "free_gb": round(disk.free / 1_000_000_000, 1),
                "free_percent": round(100 * disk.free / disk.total, 1),
            }
        except OSError:
            disk_metrics = {}
        try:
            load_1m = round(os.getloadavg()[0], 2)
        except (AttributeError, OSError):
            load_1m = None
        return {
            "worker_id": self.worker_id, "name": self.name,
            "protocol_version": 2,
            "hardware": {"cpu_cores": config._detect_cpu_count(),
                         "ram_gb": round(config._detect_ram_gb(), 1),
                         "hw_encoder": config.gpu_encoder(),
                         "platform": platform.system(),
                         "platform_release": platform.release(),
                         "python": platform.python_version()},
            "capacity": {"max_jobs": physical_max,
                         "effective_max_jobs": effective_max,
                         "active": active,
                         "free": (max(0, effective_max - active)
                                  if accepting else 0)},
            "metrics": {"disk": disk_metrics, "load_1m": load_1m,
                        "uptime_seconds": round(time.monotonic() - self._started_at)},
            "policy": policy,
            "jobs": jobs,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = requests.post(f"{config.FLEET_MAIN_URL}/api/fleet/heartbeat",
                                  json=self._payload(), headers={
                                      "Authorization": f"Bearer {config.FLEET_PAIRING_TOKEN}"},
                                  timeout=max(15, config.FLEET_HEARTBEAT_SECONDS * 3))
                if r.status_code == 200:
                    self._handle(r.json())
                else:
                    print(f"[fleet] heartbeat rejected: {r.status_code} {r.text[:160]}")
            except Exception as e:  # noqa: BLE001
                print(f"[fleet] heartbeat failed: {e}")
            self._stop.wait(config.FLEET_HEARTBEAT_SECONDS)

    def _handle(self, reply: dict) -> None:
        # Aborts first: if main wants a job stopped, don't spend this cycle
        # starting more work for it.
        for jid in reply.get("aborts") or []:
            jid = str(jid or "")
            if not jid or self.aborter is None:
                continue
            try:
                self.aborter(jid)
            except Exception as e:  # noqa: BLE001
                print(f"[fleet] abort of {jid} failed: {e}")
        policy = reply.get("policy")
        if isinstance(policy, dict):
            mode = str(policy.get("mode") or "active")
            if mode not in ("active", "draining", "paused"):
                mode = "active"
            max_jobs = policy.get("max_jobs")
            try:
                max_jobs = max(1, min(8, int(max_jobs))) if max_jobs is not None else None
            except (TypeError, ValueError):
                max_jobs = None
            with self._lock:
                self._policy = {"mode": mode, "max_jobs": max_jobs}
        # The main only acknowledges a terminal snapshot after applying and
        # persisting it. Pruning here keeps days of completed reporters from
        # bloating every future heartbeat while preserving retry-by-same-ID.
        for jid in reply.get("acknowledged_jobs") or []:
            jid = str(jid or "")
            with self._lock:
                reporter = self._jobs.get(jid)
                if (reporter and reporter.snapshot().get("status")
                        in ("done", "error")):
                    self._jobs.pop(jid, None)
        for approval in reply.get("approvals") or []:
            with self._lock:
                reporter = self._jobs.get(str(approval.get("job_id") or ""))
            if reporter:
                reporter.approve(approval)
        for spec in reply.get("dispatch") or []:
            jid = str(spec.get("job_id") or "")
            if not jid:
                continue
            with self._lock:
                mode = self._policy.get("mode") or "active"
                physical_max = max(1, min(8, int(config.FLEET_MAX_JOBS)))
                override = self._policy.get("max_jobs")
                effective_max = min(physical_max, int(override)) if override else physical_max
                active = sum(
                    1 for item in self._jobs.values()
                    if item.snapshot().get("status") in ("running", "awaiting_approval")
                )
                if mode != "active" or active >= effective_max:
                    continue
                existing = self._jobs.get(jid)
                if existing:
                    # Repeated delivery while the job is still active is the
                    # normal heartbeat acknowledgement path. A deliberate
                    # same-ID retry after a terminal result is different: it
                    # must get a fresh reporter/thread or the worker would
                    # replay the old done/error snapshot forever.
                    if existing.snapshot().get("status") in (
                            "running", "awaiting_approval"):
                        continue
                reporter = FleetReporter(jid)
                self._jobs[jid] = reporter
            threading.Thread(target=self._run, args=(spec, reporter), daemon=True,
                             name=f"dmiqwi-fleet-{jid}").start()

    def _run(self, spec: dict, reporter: FleetReporter) -> None:
        try:
            self.runner(spec, reporter)
        except Exception as e:  # noqa: BLE001
            # This is the only exception boundary visible to the fleet main.
            # Preserve the traceback (bounded so heartbeat payloads stay
            # small) — a bare Windows OSError message is not actionable when
            # the worker's console is on another machine.
            detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            reporter.fail(detail[-6000:])
