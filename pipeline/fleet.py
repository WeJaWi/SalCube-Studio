"""Worker-side polling client for the private Dmiqwi render fleet."""
from __future__ import annotations

import socket
import threading
import time
import traceback
import uuid
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

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="dmiqwi-fleet-heartbeat")
            self._thread.start()

    def _payload(self) -> dict:
        with self._lock:
            jobs = [r.snapshot() for r in self._jobs.values()]
        active = sum(1 for j in jobs if j["status"] in ("running", "awaiting_approval"))
        return {
            "worker_id": self.worker_id, "name": self.name,
            "hardware": {"cpu_cores": config._detect_cpu_count(),
                         "ram_gb": round(config._detect_ram_gb(), 1),
                         "hw_encoder": config.gpu_encoder()},
            "capacity": {"max_jobs": config.FLEET_MAX_JOBS,
                         "free": max(0, config.FLEET_MAX_JOBS - active)},
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
