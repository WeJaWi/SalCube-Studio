"""Dmiqwi — local video studio web UI."""
from __future__ import annotations

import atexit
import hmac
import json
import secrets
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, g, jsonify, make_response, redirect,
                   render_template, request, send_file, abort)

from pipeline import (animation, cancel, config, fleet, jobs, orchestrator,
                      scheduler, stats, storage, thumbnail)

app = Flask(__name__)

# In-memory job registry. Fine for a single-user local tool.
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()

# Video-creation queue: up to MAX_CONCURRENT_JOBS jobs run at once. Extra
# submissions sit as "queued" and start automatically as a worker frees up.
# _ACTIVE groups "running" + "awaiting_approval" for UI display only (both
# show up front in /api/jobs) — it is NOT the concurrency gate. A job
# parked at awaiting_approval is just idling on a human decision, not using
# CPU/API resources, so it must not hold a worker slot: _SLOT_STATUSES is
# the actual gate, checked by _active_slot_count()/_kick_queue().
_ACTIVE = frozenset({"running", "awaiting_approval", "dispatched", "uploading",
                     "retry_wait"})
_SLOT_STATUSES = frozenset({"running"})

# Persisted so server restarts (code deploys, crashes) do not wipe the queue.
# Running/awaiting jobs are re-queued on restore; assets on disk are reused.
QUEUE_STATE_FILE = config.OUTPUT_DIR / "queue_state.json"
# Guards the queue_state.json write — with several jobs progressing at once,
# concurrent writers must not share the same .tmp path (interleaved writes
# would corrupt it, or one replace() could pick up the other's half-written
# file). File I/O here is a few KB and fast, so serializing it is cheap.
_PERSIST_LOCK = threading.Lock()

# OAuth state is intentionally short-lived and in memory: its only purpose is
# to bind the browser callback to an authorization request made by this app.
DRIVE = storage.GoogleDriveStorage()
_DRIVE_AUTH_STATES: dict[str, tuple[str, float]] = {}
_DRIVE_AUTH_LOCK = threading.Lock()
FLEET_WORKERS: dict[str, dict] = {}
_FLEET_LOCK = threading.Lock()
FLEET_WORKER: fleet.FleetWorker | None = None

# Phase-weighted progress (0-100), keyed by the real `step` names emitted
# from pipeline/orchestrator.py's emit() calls — each value is that phase's
# START percentage. "render" is by far the longest phase on a long video
# (hundreds of beats), so it gets its own fine-grained per-beat progress on
# top of this coarse table (see _render_progress_pct below).
_PHASE_START_PCT = {
    "start": 0, "outline": 0, "awaiting": 3, "script": 3, "plan": 8,
    "music": 10, "voiceover": 10, "host": 25, "broll": 30, "anim": 30,
    "render": 50, "done": 100,
}
_RENDER_START_PCT = 50
_RENDER_BEAT_BUDGET_PCT = 43  # remaining ~5% of the render phase is stitch


def _render_progress_pct(completed: int, total: int) -> int:
    if total <= 0:
        return _RENDER_START_PCT
    frac = min(1.0, completed / total)
    return _RENDER_START_PCT + int(_RENDER_BEAT_BUDGET_PCT * frac)

# ── Access control (only active when ACCESS_KEYS is set in .env) ──
# Each key belongs to a named user with an allowed-profiles set; None = all.
ACCESS = config.access_keys()

_LOGIN_PAGE = """<!doctype html><html><head><title>Dmiqwi</title>
<style>
body{background:#1a1c1f;color:#d4d7dc;font-family:Inter,-apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
background-image:radial-gradient(800px 400px at 50% -10%,rgba(120,128,140,.14),transparent 55%),
linear-gradient(180deg,#1c1e22,#17191c)}
form{background:#25282e;padding:40px 36px;border-radius:16px;border:1px solid #3a3f48;
text-align:center;box-shadow:0 0 0 1px rgba(255,255,255,.03) inset,0 12px 32px rgba(0,0,0,.25)}
.mark{width:40px;height:40px;border-radius:12px;margin:0 auto 14px;
background:linear-gradient(145deg,#6a707a,#4a505a);color:#e8eaed;font-weight:800;
font-size:18px;display:grid;place-items:center;box-shadow:0 0 0 1px rgba(255,255,255,.06) inset}
input{background:#1e2126;color:#d4d7dc;border:1px solid #3a3f48;
border-radius:12px;padding:12px 14px;font-size:15px;width:260px;margin:14px 0;outline:none}
input:focus{border-color:#5a6a72;box-shadow:0 0 0 3px rgba(148,156,168,.12)}
button{background:linear-gradient(180deg,#6a727c,#4e5560 45%,#3a404a);color:#e8eaed;border:0;
border-radius:999px;padding:11px 28px;font-size:14px;font-weight:650;cursor:pointer;
box-shadow:0 0 0 1px rgba(255,255,255,.08) inset}
h2{margin:0;font-size:22px;letter-spacing:-.03em;color:#c8ccd2}h2 span{color:#5ec8d8}
</style></head><body>
<form method="post" action="/login"><div class="mark">D</div>
<h2>Dmi<span>qwi</span></h2>
<input type="password" name="key" placeholder="Access key" autofocus />
<br/><button>Enter</button>{ERR}</form></body></html>"""


def _user_for(key):
    return next((u for u in ACCESS if key and hmac.compare_digest(u["key"], key)), None)


def _allowed_profile(name: str) -> bool:
    u = getattr(g, "user", None)
    return not ACCESS or (u and (u["profiles"] is None or name in u["profiles"]))


def _job_profile(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if job:
        return job.get("profile")
    f = config.OUTPUT_DIR / job_id / "inputs.json"
    if f.exists():
        try:
            return json.loads(f.read_text()).get("_profile")
        except Exception:  # noqa: BLE001
            return None
    return None


@app.before_request
def _auth():
    if request.path == "/api/fleet/heartbeat":
        bearer = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if (config.FLEET_ROLE == "main" and config.FLEET_PAIRING_TOKEN
                and bearer and hmac.compare_digest(config.FLEET_PAIRING_TOKEN, bearer)):
            g.fleet_worker = True
            return None
        return jsonify({"error": "fleet authentication required"}), 401
    if not ACCESS:
        return None  # single-user localhost mode — unchanged behavior
    if request.path == "/login" or request.path.startswith("/static"):
        return None
    g.user = _user_for(request.cookies.get("studio_key"))
    if g.user:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "not authorized"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return _LOGIN_PAGE.replace("{ERR}", "")
    u = _user_for((request.form.get("key") or "").strip())
    if not u:
        return _LOGIN_PAGE.replace(
            "{ERR}", "<p style='color:#f85149'>Wrong key</p>"), 401
    resp = make_response(redirect("/"))
    resp.set_cookie("studio_key", u["key"], max_age=90 * 24 * 3600,
                    httponly=True, samesite="Lax")
    return resp


def _list_profiles() -> list[str]:
    """Only runnable channel profiles — reference/source docs living in the
    same folder (no "video" section) must never appear in the dropdown."""
    import json as _json
    out = []
    for p in config.PROFILES_DIR.glob("*.json"):
        try:
            if "video" in _json.loads(p.read_text()):
                out.append(p.stem)
        except Exception:  # noqa: BLE001
            pass
    return sorted(out)


def _profile_theme_defaults() -> dict:
    """profile name → its default color schemes (for the UI checkboxes)."""
    import json as _json
    out = {}
    for p in config.PROFILES_DIR.glob("*.json"):
        try:
            v = _json.loads(p.read_text()).get("video", {})
            out[p.stem] = v.get("anim_themes_default") or ["classic"]
        except Exception:  # noqa: BLE001
            out[p.stem] = ["classic"]
    return out


def _active_slot_count() -> int:
    return sum(1 for j in JOBS.values() if j.get("status") in _SLOT_STATUSES)


def _next_queued_ids(n: int) -> list[str]:
    """Up to `n` still-queued job ids, in the order they should start.

    FIFO by created_at, except that anything carrying `priority_at` sorts
    ahead of everything else — that's how a manual retry starts next
    instead of joining the back of the line, and how a job preempted to
    make room for it gets the following slot rather than starving.
    """
    if n <= 0:
        return []
    queued = [(jid, j) for jid, j in JOBS.items() if j.get("status") == "queued"]
    queued.sort(key=lambda x: (0, x[1]["priority_at"]) if x[1].get("priority_at")
                else (1, x[1].get("created_at") or ""))
    return [jid for jid, _ in queued[:n]]


def _persist_queue() -> None:
    """Write queue snapshot to disk (no private thread Event objects)."""
    with LOCK:
        rows = []
        for jid, j in JOBS.items():
            st = j.get("status") or "queued"
            # Don't keep cancelled noise forever
            if st == "cancelled":
                continue
            rows.append({
                "job_id": jid,
                "status": st,
                "step": j.get("step", ""),
                "title": j.get("title", ""),
                "profile": j.get("profile", ""),
                "inputs": j.get("inputs") or {},
                "created_at": j.get("created_at") or "",
                "error": j.get("error"),
                "result": j.get("result"),
                "progress_pct": j.get("progress_pct", 0),
                "est_chars": j.get("est_chars"),
                "chars": j.get("chars"),
                "est_beats": j.get("est_beats"),
                "size": j.get("size"),
                "drive": j.get("drive"),
                "dispatched_to": j.get("dispatched_to"),
                "dispatched_at": j.get("dispatched_at"),
                "retries": j.get("retries", 0),
                "priority_at": j.get("priority_at"),
                # Keep a short log so the UI isn't empty after restart
                "log": list(j.get("log") or [])[-40:],
            })
    # Concurrent job threads all call this — serialize the disk write so two
    # writers never share the same .tmp path mid-write (see _PERSIST_LOCK).
    with _PERSIST_LOCK:
        try:
            QUEUE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = QUEUE_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "jobs": rows}, indent=1))
            tmp.replace(QUEUE_STATE_FILE)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _restore_queue() -> None:
    """Reload queue from disk after a restart. Incomplete work is re-queued."""
    if not QUEUE_STATE_FILE.exists():
        return
    try:
        data = json.loads(QUEUE_STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    rows = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return
    restored_q = 0
    with LOCK:
        for row in rows:
            if not isinstance(row, dict):
                continue
            jid = (row.get("job_id") or "").strip()
            if not jid or jid in JOBS:
                continue
            profile = row.get("profile") or "grandma-hannah"
            inputs = row.get("inputs") or {}
            # Prefer on-disk inputs.json (source of truth after partial runs)
            inputs_file = config.OUTPUT_DIR / jid / "inputs.json"
            if inputs_file.exists():
                try:
                    saved = json.loads(inputs_file.read_text())
                    profile = saved.pop("_profile", None) or profile
                    for k, v in saved.items():
                        if k not in inputs or inputs.get(k) in (None, "", []):
                            inputs[k] = v
                except Exception:  # noqa: BLE001
                    pass
            title = (row.get("title") or inputs.get("title") or jid).strip()
            st = row.get("status") or "queued"
            final = config.OUTPUT_DIR / jid / "final.mp4"
            part1 = config.OUTPUT_DIR / jid / "final_part1.mp4"
            drive = row.get("drive") if isinstance(row.get("drive"), dict) else {}
            has_local_deliverable = final.exists() or part1.exists()
            drive_complete = drive.get("state") == "uploaded"
            # Threads die with the process — any in-flight job must re-queue
            # unless the final video is already on disk.
            if has_local_deliverable or drive_complete:
                st = "done"
                step = "done"
                progress_pct = 100
                log = list(row.get("log") or [])
                log.append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "step": "queue",
                    "message": ("Restored after restart — Drive deliverables already uploaded"
                                if drive_complete else
                                "Restored after restart — final deliverable already present"),
                })
            elif st in ("running", "awaiting_approval", "queued", "retry_wait"):
                # retry_wait's sleeping timer thread died with the process,
                # so it would otherwise sit in that state forever — fold it
                # back into the queue like any other interrupted job.
                # In-flight work died with the process — re-queue it.
                # Errored jobs are NOT auto-retried (a permanently broken job
                # would re-burn credits on every restart); the queue UI offers
                # a Retry button that resumes them deliberately.
                st = "queued"
                step = "queued"
                progress_pct = 0  # a fresh run re-progresses from the top
                log = list(row.get("log") or [])
                log.append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "step": "queue",
                    "message": "Restored after restart — re-queued "
                               "(assets on disk will be reused)",
                })
                restored_q += 1
            else:
                step = row.get("step") or st
                progress_pct = row.get("progress_pct", 0)
                log = list(row.get("log") or [])
            JOBS[jid] = {
                "status": st,
                "step": step,
                "progress_pct": progress_pct,
                "log": log,
                "title": title,
                "profile": profile,
                "inputs": inputs,
                "created_at": row.get("created_at") or datetime.now().isoformat(
                    timespec="seconds"),
                "error": row.get("error") if st == "error" else None,
                "result": str(final) if final.exists() else row.get("result"),
                "drive": drive,
                "dispatched_to": row.get("dispatched_to"),
                "dispatched_at": row.get("dispatched_at"),
                "est_chars": row.get("est_chars"),
                "chars": row.get("chars"),
                "est_beats": row.get("est_beats"),
                "size": row.get("size"),
                "retries": row.get("retries", 0),
                "priority_at": row.get("priority_at"),
            }
        # queue_state.json is deliberately small and can be replaced during a
        # crash/deploy. Finished videos remain the source of truth on disk, so
        # recover their library rows when an empty snapshot is encountered.
        if not JOBS and not rows:
            for job_dir in config.OUTPUT_DIR.iterdir():
                if not job_dir.is_dir() or not (job_dir / "inputs.json").exists():
                    continue
                final = job_dir / "final.mp4"
                part1 = job_dir / "final_part1.mp4"
                if not final.exists() and not part1.exists():
                    continue
                try:
                    saved = json.loads((job_dir / "inputs.json").read_text())
                except Exception:  # noqa: BLE001
                    continue
                profile = saved.pop("_profile", "grandma-hannah")
                JOBS[job_dir.name] = {
                    "status": "done", "step": "done", "progress_pct": 100,
                    "log": [{"t": datetime.now().strftime("%H:%M:%S"),
                             "step": "queue", "message": "Recovered finished video from disk"}],
                    "title": saved.get("title") or job_dir.name, "profile": profile,
                    "inputs": saved,
                    "created_at": datetime.fromtimestamp(job_dir.stat().st_mtime).isoformat(timespec="seconds"),
                    "result": str(final if final.exists() else part1),
                    "drive": {},
                }
    if restored_q:
        print(f"[queue] restored {restored_q} incomplete job(s) from "
              f"{QUEUE_STATE_FILE.name}")
    _persist_queue()
    _kick_queue()


def _schedule_retry(job_id: str) -> None:
    """Put a failed job back in the queue after a short delay.

    Delayed rather than immediate because the failures this actually
    catches are provider outages (kie.ai 5xx), where retrying instantly
    just spends an attempt on a service that is still down. The job sits in
    "retry_wait" meanwhile, which holds no slot, so other work proceeds.
    """
    delay = float(config.queue_setting("retry_delay_seconds") or 60)

    def _later() -> None:
        time.sleep(delay)
        with LOCK:
            job = JOBS.get(job_id)
            if job is None or job.get("status") != "retry_wait":
                return  # user intervened (stopped/retried/cancelled) — leave it
            job["status"] = "queued"
            job["step"] = "queued"
            job["priority_at"] = datetime.now().isoformat(timespec="seconds")
            job["log"].append({
                "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                "message": "Auto-retry: back in the queue"})
        _persist_queue()
        _kick_queue()

    threading.Thread(target=_later, daemon=True).start()


def _preempt_for(job_id: str) -> bool:
    """Free a slot for `job_id` by stopping the running job that has the
    least to lose — the one with the lowest progress. The stopped job is
    marked to requeue and gets priority, so it takes the next free slot
    rather than going to the back of the line.

    Caller must NOT hold LOCK (token.cancel() kills subprocesses, and
    _persist_queue re-acquires LOCK).
    """
    with LOCK:
        running = [(jid, j) for jid, j in JOBS.items()
                   if j.get("status") == "running" and jid != job_id
                   and j.get("_cancel") is not None]
        if not running:
            return False
        running.sort(key=lambda x: x[1].get("progress_pct", 0))
        victim_id, victim = running[0]
        token = victim["_cancel"]
        victim["_requeue_on_stop"] = True
        # Preempted work was already in flight, so it earns the next slot
        # rather than starving behind everything else.
        victim["priority_at"] = "0000"
        victim["log"].append({
            "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
            "message": (f"Paused at {victim.get('progress_pct', 0)}% so a "
                        f"manual retry could start — resumes next")})
    token.cancel()
    return True


def _kick_queue() -> None:
    """Start queued jobs until MAX_CONCURRENT_JOBS workers are busy.

    Safe to call often / concurrently — jobs are claimed (status flipped to
    "running") while still holding LOCK, so two overlapping kicks can never
    double-start the same job.
    """
    if config.queue_setting("queue_paused"):
        return  # paused: let running jobs finish, just don't pick up more
    to_start: list[tuple[str, dict, str]] = []
    with LOCK:
        free = config.max_concurrent_jobs() - _active_slot_count()
        if free <= 0:
            return
        for jid in _next_queued_ids(free):
            job = JOBS[jid]
            job["status"] = "running"
            job["step"] = "starting"
            to_start.append((jid, job["inputs"], job["profile"]))
    if not to_start:
        return
    _persist_queue()
    for jid, inputs, profile in to_start:
        threading.Thread(
            target=_run_job_wrapper, args=(jid, inputs, profile), daemon=True,
        ).start()


def _run_job_wrapper(job_id: str, inputs: dict, profile: str) -> None:
    try:
        _run_job(job_id, inputs, profile)
    finally:
        _persist_queue()
        # Always try to drain the queue when this worker finishes or dies.
        try:
            _kick_queue()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _run_fleet_worker_job(spec: dict, reporter: fleet.FleetReporter) -> None:
    """Run a main-dispatched job locally on a worker, never via shell input."""
    job_id = str(spec["job_id"])
    inputs = dict(spec.get("inputs") or {})
    profile = str(spec.get("profile") or "grandma-hannah")
    profile_data = spec.get("profile_data") if isinstance(spec.get("profile_data"), dict) else None
    job_dir = config.OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "inputs.json").write_text(json.dumps({**inputs, "_profile": profile}, indent=1))
    with LOCK:
        JOBS[job_id] = {
            "status": "running", "step": "start", "progress_pct": 0,
            "log": [], "title": inputs.get("title") or job_id, "profile": profile,
            "inputs": inputs, "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    _run_job(job_id, inputs, profile, profile_data=profile_data,
             fleet_reporter=reporter)


def _fleet_dispatch_for(worker_id: str, free: int) -> list[dict]:
    """Claim queued jobs only after this main has filled its own render slots."""
    if config.FLEET_ROLE != "main" or free <= 0:
        return []
    with LOCK:
        if _active_slot_count() < config.max_concurrent_jobs():
            return []  # overflow policy: main keeps its own compute busy first
        job_ids = _next_queued_ids(free)
        dispatch = []
        for job_id in job_ids:
            job = JOBS[job_id]
            try:
                profile_data = orchestrator.load_profile(job["profile"])
            except Exception as e:  # noqa: BLE001
                job.update(status="error", step="error", error=f"fleet profile snapshot failed: {e}")
                continue
            job.update(status="dispatched", step="fleet", dispatched_to=worker_id,
                       dispatched_at=datetime.now().isoformat(timespec="seconds"))
            dispatch.append({"job_id": job_id, "inputs": job["inputs"],
                             "profile": job["profile"], "profile_data": profile_data})
    if dispatch:
        _persist_queue()
    return dispatch


def _append_job_log(job_id: str, step: str, message: str) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.setdefault("log", []).append({
            "t": datetime.now().strftime("%H:%M:%S"),
            "step": step, "message": message,
        })


def _drive_deliverables(job_dir: Path) -> list[tuple[str, Path]]:
    """Return the real deliverables, never final.mp4's Patreon duplicate."""
    part1 = job_dir / "final_part1.mp4"
    part2 = job_dir / "final_part2.mp4"
    if part1.exists() or part2.exists():
        return [(name, path) for name, path in (("part1", part1), ("part2", part2))
                if path.exists()]
    final = job_dir / "final.mp4"
    return [("final", final)] if final.exists() else []


def _upload_job_deliverables(job_id: str, job_dir: Path) -> None:
    """Best-effort cloud handoff; a Drive issue must never erase the local file."""
    if not DRIVE.configured:
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "not_configured", "files": {}}
        _append_job_log(job_id, "drive", "Drive is not configured — keeping local deliverable")
        _persist_queue()
        return
    if not DRIVE.authorized:
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "pending_authorization", "files": {}}
        _append_job_log(job_id, "drive", "Drive needs one-time browser authorization — keeping local deliverable")
        _persist_queue()
        return

    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        drive = dict(job.get("drive") or {})
        files = dict(drive.get("files") or {})
        job["drive"] = {"state": "uploading", "files": files}
    _append_job_log(job_id, "drive", "Uploading verified deliverable(s) to Drive…")
    _persist_queue()

    try:
        for role, path in _drive_deliverables(job_dir):
            if role in files and not path.exists():
                continue
            result = DRIVE.upload(path, state_file=job_dir / f".gdrive-{role}.json")
            files[role] = result
            with LOCK:
                job = JOBS.get(job_id)
                if job:
                    job["drive"] = {"state": "uploading", "files": files}
            _persist_queue()
            # A Patreon final.mp4 is a hardlink/copy of part 1. Once part 1
            # has been verified in Drive, both local names are safe to remove.
            path.unlink(missing_ok=True)
            if role == "part1":
                (job_dir / "final.mp4").unlink(missing_ok=True)
            _append_job_log(job_id, "drive", f"Uploaded and verified {result['name']}")
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "uploaded", "files": files}
        _append_job_log(job_id, "drive", "Drive handoff complete; local deliverable removed")
    except Exception as e:  # noqa: BLE001
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "error", "files": files, "error": str(e)}
        _append_job_log(job_id, "drive", f"Drive upload failed; local file kept ({e})")
    finally:
        _persist_queue()


def _run_job(job_id: str, inputs: dict, profile: str, *,
             profile_data: dict | None = None,
             fleet_reporter: fleet.FleetReporter | None = None) -> None:
    job_dir = config.OUTPUT_DIR / job_id

    # Ensure status is running (kick_queue usually set this already), and
    # publish a fresh cancel token so Stop/preempt can reach this job's
    # ffmpeg children. Underscore-prefixed so _persist_queue never tries to
    # serialize it.
    token = cancel.CancelToken(job_id)
    cancel.set_current(token)
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["_cancel"] = token

    # JobManager JSONL logging (Wexilo port) is OPTIONAL: it observes the
    # render, it must never be able to kill it. If setup fails the job runs
    # without structured logs, and the failure is recorded in the job log.
    jm, manual_job_id = None, None
    try:
        jm = jobs.JobManager.instance()
        manual_job_id = jm.start_manual_job(
            meta={"tool": "health_studio_render", "profile": profile,
                  "inputs": inputs},
            tag="render",
        )
        with LOCK:
            JOBS[job_id]["manual_job_id"] = manual_job_id
        print(f"[job {job_id}] worker started (profile={profile}, "
              f"jsonl={jm.get_log_path(manual_job_id)})")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"[job {job_id}] ⚠ JobManager setup failed ({e}) — "
              f"rendering continues WITHOUT JSONL logging")
        with LOCK:
            JOBS[job_id]["log"].append({
                "t": datetime.now().strftime("%H:%M:%S"), "step": "start",
                "message": f"⚠ job-manager logging unavailable ({e}) — "
                           f"continuing without it"})

    def _jm_call(fn_name: str, *args) -> None:
        """Best-effort JobManager call — a broken JSONL file (disk full,
        deleted logs dir, …) must not take the render down with it."""
        if jm is None or manual_job_id is None:
            return
        try:
            getattr(jm, fn_name)(manual_job_id, *args)
        except Exception as e:  # noqa: BLE001
            print(f"[job {job_id}] jobmanager.{fn_name} failed: {e}")

    def progress(step: str, message: str) -> None:
        pct = _PHASE_START_PCT.get(step)
        with LOCK:
            job = JOBS[job_id]
            job["step"] = step
            if pct is not None:
                # Monotonic: a stray out-of-order emit() must never regress
                # the bar (matters most once render's beat-progress has
                # already pushed past 50%).
                job["progress_pct"] = max(job.get("progress_pct", 0), pct)
            job["log"].append({"t": datetime.now().strftime("%H:%M:%S"),
                               "step": step, "message": message})
        # JSONL writes stay OUTSIDE the lock — status polls contend on it
        _jm_call("manual_log", f"[{step}] {message}")
        if pct is not None:
            _jm_call("manual_progress", pct)
        # Persist occasionally on major phase changes (not every log line)
        if step in _PHASE_START_PCT:
            _persist_queue()
        if fleet_reporter:
            with LOCK:
                pct_now = JOBS.get(job_id, {}).get("progress_pct", 0)
            fleet_reporter.progress(step, message, pct_now)

    def beat_progress(completed: int, total: int) -> None:
        """Fine-grained progress inside the render phase — by far the
        longest phase on a long video. Only ever raises progress_pct,
        never regresses it; persists roughly every 10% to avoid contending
        _PERSIST_LOCK on every single beat completion."""
        pct = _render_progress_pct(completed, total)
        should_persist = False
        with LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            old = job.get("progress_pct", 0)
            if pct > old:
                job["progress_pct"] = pct
                should_persist = old // 10 != pct // 10
        if should_persist:
            _persist_queue()

    def gate(hooks: dict, outline: str):
        """Publish hooks+outline, block until /api/approve supplies the picks."""
        if fleet_reporter:
            return fleet_reporter.await_approval(hooks, outline)
        event = threading.Event()
        with LOCK:
            JOBS[job_id]["status"] = "awaiting_approval"
            JOBS[job_id]["awaiting"] = {"hooks": hooks, "outline": outline}
            JOBS[job_id]["_gate_event"] = event
        _persist_queue()
        event.wait()
        with LOCK:
            approval = JOBS[job_id].pop("approval")
            JOBS[job_id].pop("awaiting", None)
            JOBS[job_id].pop("_gate_event", None)
            JOBS[job_id]["status"] = "running"
        _persist_queue()
        hook_text = approval.get("hook_text") or hooks.get(
            approval.get("hook_type", "bold_statement"), "")
        return hook_text, (approval.get("outline") or outline)

    def on_planned(meta: dict) -> None:
        """The real narration length is now known (see orchestrator.py's
        on_planned call, right after the script is finalized). Purely
        informational in this increment — updates the queue's size
        badge/ETA display, never blocks or gates anything."""
        settings = config.load_settings()
        small_th = config.queue_setting("small_threshold_chars", profile, settings)
        big_th = config.queue_setting("big_threshold_chars", profile, settings)
        with LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            job["chars"] = meta["chars"]
            job["est_beats"] = meta["est_beats"]
            job["size"] = scheduler.classify_size(meta["chars"], small_th, big_th)
        _persist_queue()

    def voice_gate(info: dict) -> None:
        """Log TTS totals / Patreon cut, then continue without waiting.

        Visual generation starts automatically (no manual Approve click).
        """
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id]["voice_info"] = info
                JOBS[job_id]["status"] = "running"
                JOBS[job_id]["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "step": "voice",
                    "message": (
                        f"Voice auto-approved — total ≈ {info.get('total_time')}, "
                        f"{info.get('beats')} beats"
                        + (f", Patreon cut ≈ {info.get('cut_time')} "
                           f"(beat {info.get('cut_beat')})"
                           if info.get("cut_time") else "")
                        + " → generating visuals"
                    ),
                })
        _persist_queue()

    try:
        out = orchestrator.run_pipeline(inputs, profile, job_dir, progress,
                                        gate, voice_gate, beat_progress,
                                        on_planned, profile_data)
        with LOCK:
            JOBS[job_id]["status"] = "uploading"
            JOBS[job_id]["step"] = "drive"
            JOBS[job_id]["result"] = str(out)
            JOBS[job_id]["drive"] = {"state": "uploading", "files": {}}
        _persist_queue()
        _upload_job_deliverables(job_id, job_dir)
        with LOCK:
            if job_id in JOBS:
                # A Drive failure is an operational retry, not a failed
                # render. The verified local render remains playable.
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["step"] = "done"
                JOBS[job_id]["progress_pct"] = 100
        _persist_queue()
        _jm_call("manual_progress", 100)
        _jm_call("manual_finish", 0)
        if fleet_reporter:
            with LOCK:
                fleet_reporter.complete(dict(JOBS.get(job_id, {}).get("drive") or {}))
    except cancel.JobCancelled:
        # Deliberately stopped (Stop button, or preempted by a manual
        # retry) — NOT a failure. Everything generated so far stays on disk,
        # so resuming re-uses it instead of re-billing the provider APIs.
        with LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                requeue = bool(job.pop("_requeue_on_stop", False))
                job["status"] = "queued" if requeue else "cancelled"
                job["step"] = "queued" if requeue else "cancelled"
                job.pop("_cancel", None)
                job["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                    "message": ("Stopped — back in the queue, will resume "
                                "from what's already on disk"
                                if requeue else "Stopped and cancelled")})
        _persist_queue()
        _jm_call("manual_log", "STOPPED by user")
        _jm_call("manual_finish", 1)
        if fleet_reporter:
            fleet_reporter.fail("stopped by user")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"[job {job_id}] ✗ pipeline failed: {e}")
        # Auto-retry a genuine failure a couple of times before giving up —
        # most failures here are provider outages, and the on-disk caches
        # mean a retry resumes rather than restarting from scratch. The
        # delay matters: an instant retry into a provider that is down just
        # burns an attempt.
        limit = int(config.queue_setting("job_retry_limit", profile) or 0)
        with LOCK:
            job = JOBS.get(job_id)
            attempts = int((job or {}).get("retries", 0))
            will_retry = job is not None and attempts < limit
            if job is not None:
                job.pop("_cancel", None)
                if will_retry:
                    job["retries"] = attempts + 1
                    job["status"] = "retry_wait"
                    job["step"] = "retry_wait"
                    job["error"] = str(e)
                    job["log"].append({
                        "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                        "message": (f"Failed ({str(e)[:120]}) — auto-retry "
                                    f"{attempts + 1}/{limit} shortly")})
                else:
                    job["status"] = "error"
                    job["error"] = str(e)
                    job["log"].append({
                        "t": datetime.now().strftime("%H:%M:%S"),
                        "step": "error", "message": str(e)})
        _persist_queue()
        _jm_call("manual_log", f"ERROR: {e}")
        _jm_call("manual_finish", 1)
        if fleet_reporter:
            fleet_reporter.fail(str(e))
        if will_retry:
            _schedule_retry(job_id)


@app.route("/")
def index():
    return render_template(
        "index.html",
        profiles=[p for p in _list_profiles() if _allowed_profile(p)],
        missing_keys=config.missing_keys(),
        heygen_ready=bool(config.HEYGEN_API_KEY and config.HEYGEN_AVATAR_ID),
        anim_templates=list(animation.TEMPLATES),
        anim_themes=list(animation.THEMES),
        profile_theme_defaults=_profile_theme_defaults(),
    )


def _build_inputs(data: dict, title: str) -> dict:
    """Shared input packing for /api/render (one title → one job)."""
    broll = [k for k in ("stock", "stock_image", "ai_video", "animation")
             if data.get(f"broll_{k}")]
    anim_templates = data.get("anim_templates")
    return {
        "title": title,
        "language": (data.get("language") or "").strip(),
        "length_chars": data.get("length_chars"),
        "comment_cta": bool(data.get("comment_cta", True)),
        "patreon_split": bool(data.get("patreon_split", False)),
        "patreon_cut_from": data.get("patreon_cut_from"),
        "patreon_cut_to": data.get("patreon_cut_to"),
        "promote_ebook": bool(data.get("promote_ebook", False)),
        "ebook_cta_at": data.get("ebook_cta_at"),
        "ebook_text": (data.get("ebook_text") or "").strip(),
        "promote_community": bool(data.get("promote_community", False)),
        "cited_philosophers": (data.get("cited_philosophers") or "").strip(),
        "custom_script": (data.get("custom_script") or "").strip(),
        "auto_hook": (data.get("auto_hook") or "").strip(),
        "script_model": (data.get("script_model") or "").strip(),
        "broll": broll or ["stock"],
        "mindmap": bool(data.get("mindmap", False)),
        "anim_templates": anim_templates if isinstance(anim_templates, list) else None,
        # None when absent — the orchestrator falls back to the profile's
        # anim_themes_default (e.g. Matthias: amber+plum)
        "anim_themes": data.get("anim_themes") or None,
        # None when absent — profile default, then "vision"
        "smart_stock": (data.get("smart_stock") or "").strip() or None,
        "qr_badge": bool(data.get("qr_badge", False)),
        "spotify_popup": bool(data.get("spotify_popup", False)),
        "spotify_url": (data.get("spotify_url") or "").strip(),
    }


def _enqueue_job(inputs: dict, profile: str, *, resume: str = "") -> str:
    """Create a job dir + registry entry with status=queued. Does not start it."""
    job_id = resume or uuid.uuid4().hex[:12]
    job_dir = config.OUTPUT_DIR / job_id
    inputs_file = job_dir / "inputs.json"
    title = (inputs.get("title") or "").strip()
    if resume and inputs_file.exists():
        saved = json.loads(inputs_file.read_text())
        for k, v in saved.items():
            if k == "_profile":
                continue
            if k not in inputs or inputs.get(k) in (None, "", []):
                inputs[k] = v
        title = inputs.get("title") or title
        profile = saved.get("_profile") or profile
    job_dir.mkdir(parents=True, exist_ok=True)
    inputs_file.write_text(json.dumps({**inputs, "_profile": profile}, indent=1))

    # Known at enqueue time, before any LLM call: the user's requested
    # target length (or that profile's own default if left blank) — same
    # fallback run_pipeline() itself applies. Lets the queue show a size
    # badge/ETA immediately; replaced with the real narration length once
    # the script stage actually finishes (see on_planned in _run_job).
    try:
        defaults = orchestrator.load_profile(profile).get("defaults", {})
    except Exception:  # noqa: BLE001
        defaults = {}
    est_chars = int(inputs.get("length_chars") or defaults.get("length_chars", 15000))

    with LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "step": "queued",
            "log": [{"t": datetime.now().strftime("%H:%M:%S"),
                     "step": "queue", "message": "Added to queue"}],
            "title": title,
            "profile": profile,
            "inputs": inputs,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "est_chars": est_chars,
            "chars": None,
            "est_beats": None,
        }
    _persist_queue()
    return job_id


@app.route("/api/render", methods=["POST"])
def start_render():
    """Enqueue one or more video jobs. Runs serially via the queue worker.

    Multi-title: put one title per line in `title`, or pass `titles: [...]`.
    Each title becomes its own job (same settings). Resume is single-job only.
    """
    data = request.get_json(force=True)
    profile = (data.get("profile") or "grandma-hannah").strip()
    if not _allowed_profile(profile):
        return jsonify({"error": "this profile is not available to you"}), 403
    resume = (data.get("resume_job") or "").strip()
    if resume and not _allowed_profile(_job_profile(resume) or ""):
        return jsonify({"error": "this job is not available to you"}), 403

    # Resolve title list
    if resume:
        titles = [(data.get("title") or "").strip() or "resume"]
    elif isinstance(data.get("titles"), list) and data["titles"]:
        titles = [str(t).strip() for t in data["titles"] if str(t).strip()]
    else:
        raw = (data.get("title") or "").strip()
        if not raw:
            return jsonify({"error": "title is required"}), 400
        # One title per line → batch enqueue
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        titles = lines if lines else [raw]

    if not titles and not resume:
        return jsonify({"error": "title is required"}), 400

    job_ids: list[str] = []
    for i, title in enumerate(titles):
        # resume only applies to a single job
        r = resume if (resume and i == 0) else ""
        inputs = _build_inputs(data, title)
        # Resume: merge saved inputs.json, request fields still win
        if r:
            inputs_file = config.OUTPUT_DIR / r / "inputs.json"
            if inputs_file.exists():
                saved = json.loads(inputs_file.read_text())
                broll_in_request = any(
                    f"broll_{x}" in data
                    for x in ("stock", "stock_image", "ai_video", "animation"))
                for k, v in saved.items():
                    if k == "_profile" or (k == "broll" and broll_in_request):
                        continue
                    if k not in data:
                        inputs[k] = v
                profile = saved.get("_profile") or profile
                if not (data.get("title") or "").strip():
                    inputs["title"] = saved.get("title") or inputs.get("title") or ""
        job_ids.append(_enqueue_job(inputs, profile, resume=r))

    # A manual retry is an explicit "do this now": give it priority so it
    # takes the next slot, and if every slot is busy, free one by pausing
    # the running job with the least progress (that job keeps its work and
    # resumes right after). Only for resumes — a batch of brand-new titles
    # should not shove running work aside.
    if resume and job_ids:
        with LOCK:
            job = JOBS.get(job_ids[0])
            if job is not None and job.get("status") == "queued":
                job["priority_at"] = datetime.now().isoformat(timespec="seconds")
                job["retries"] = 0  # manual attempt resets the auto-retry budget
            free_now = config.max_concurrent_jobs() - _active_slot_count()
        if free_now <= 0 and not config.queue_setting("queue_paused"):
            _preempt_for(job_ids[0])

    _kick_queue()
    return jsonify({
        "job_id": job_ids[0],
        "job_ids": job_ids,
        "queued": len(job_ids),
    })


@app.route("/api/approve/<job_id>", methods=["POST"])
def approve(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    data = request.get_json(force=True)
    remote_approved = False
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        event = job.get("_gate_event")
        approval = {
            "hook_type": (data.get("hook_type") or "bold_statement").strip(),
            "hook_text": (data.get("hook_text") or "").strip(),
            "outline": (data.get("outline") or "").strip(),
        }
        if job.get("status") != "awaiting_approval":
            return jsonify({"error": "job is not awaiting approval"}), 409
        remote_worker = job.get("dispatched_to")
        if remote_worker:
            with _FLEET_LOCK:
                worker = FLEET_WORKERS.get(remote_worker)
                if not worker:
                    return jsonify({"error": "remote worker is offline"}), 409
                worker.setdefault("approvals", []).append({"job_id": job_id, **approval})
            job["status"] = "running"
            job.pop("awaiting", None)
            remote_approved = True
        else:
            if not event:
                return jsonify({"error": "job approval signal is unavailable"}), 409
            job["approval"] = approval
    if remote_approved:
        # Never call _persist_queue while holding LOCK: it snapshots under
        # that same non-reentrant lock.
        _persist_queue()
        return jsonify({"ok": True, "remote": True})
    event.set()
    return jsonify({"ok": True})


@app.route("/api/fleet/heartbeat", methods=["POST"])
def fleet_heartbeat():
    """Fixed-schema worker heartbeat; bearer auth is enforced in _auth()."""
    data = request.get_json(force=True) or {}
    worker_id = str(data.get("worker_id") or "").strip()
    if not worker_id or len(worker_id) > 120:
        return jsonify({"error": "valid worker_id is required"}), 400
    capacity = data.get("capacity") if isinstance(data.get("capacity"), dict) else {}
    try:
        free = max(0, min(8, int(capacity.get("free") or 0)))
    except (TypeError, ValueError):
        free = 0
    reported = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    now = datetime.now()
    with _FLEET_LOCK:
        worker = FLEET_WORKERS.setdefault(worker_id, {"approvals": [], "dispatch": {}})
        worker.update({"worker_id": worker_id, "name": str(data.get("name") or worker_id),
                       "hardware": data.get("hardware") if isinstance(data.get("hardware"), dict) else {},
                       "capacity": capacity, "last_seen": now.isoformat(timespec="seconds")})
        approvals = list(worker.get("approvals") or [])
        worker["approvals"] = []
        pending_dispatch = dict(worker.get("dispatch") or {})
    with LOCK:
        for report in reported:
            if not isinstance(report, dict):
                continue
            job_id = str(report.get("job_id") or "")
            job = JOBS.get(job_id)
            if not job or job.get("dispatched_to") != worker_id:
                continue
            state = str(report.get("status") or "running")
            if state not in ("running", "awaiting_approval", "done", "error"):
                continue
            job["status"] = state
            job["step"] = str(report.get("step") or state)
            job["progress_pct"] = max(job.get("progress_pct", 0), int(report.get("progress_pct") or 0))
            if state == "awaiting_approval" and isinstance(report.get("awaiting"), dict):
                job["awaiting"] = report["awaiting"]
            if isinstance(report.get("drive"), dict):
                job["drive"] = report["drive"]
            if state == "error":
                job["error"] = str(report.get("error") or "remote worker failed")
    # A job appearing in this heartbeat acknowledges receipt of any previous
    # dispatch. Until then we resend it; the worker ignores duplicate IDs.
    with _FLEET_LOCK:
        worker = FLEET_WORKERS[worker_id]
        for report in reported:
            if isinstance(report, dict):
                worker.get("dispatch", {}).pop(str(report.get("job_id") or ""), None)
    # Do not hold _FLEET_LOCK while claiming queue work: approval requests
    # acquire LOCK then _FLEET_LOCK, so reversing that order would deadlock.
    new_dispatch = _fleet_dispatch_for(worker_id, free)
    with _FLEET_LOCK:
        worker = FLEET_WORKERS[worker_id]
        for spec in new_dispatch:
            worker.setdefault("dispatch", {})[spec["job_id"]] = spec
        dispatch = list(worker.get("dispatch", {}).values())
    _persist_queue()
    return jsonify({"dispatch": dispatch, "approvals": approvals})


@app.route("/api/workers")
def fleet_workers():
    now = datetime.now().timestamp()
    with _FLEET_LOCK:
        rows = []
        for worker in FLEET_WORKERS.values():
            row = {k: v for k, v in worker.items() if k not in ("approvals", "dispatch")}
            try:
                age = max(0, now - datetime.fromisoformat(row["last_seen"]).timestamp())
            except Exception:  # noqa: BLE001
                age = config.FLEET_STALE_SECONDS + 1
            row["stale"] = age > config.FLEET_STALE_SECONDS
            row["age_seconds"] = round(age)
            rows.append(row)
    return jsonify(rows)


@app.route("/api/dispatch/<job_id>", methods=["POST"])
def dispatch_to_worker(job_id: str):
    """Manually send one queued job to an available worker immediately."""
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    if config.FLEET_ROLE != "main":
        return jsonify({"error": "this machine is not configured as fleet main"}), 409
    requested = (request.get_json(silent=True) or {}).get("worker_id")
    now = datetime.now().timestamp()
    with _FLEET_LOCK:
        choices = []
        for worker in FLEET_WORKERS.values():
            try:
                age = now - datetime.fromisoformat(worker.get("last_seen", "")).timestamp()
            except Exception:  # noqa: BLE001
                continue
            free = int((worker.get("capacity") or {}).get("free") or 0)
            if age <= config.FLEET_STALE_SECONDS and free > 0:
                if not requested or requested == worker.get("worker_id"):
                    choices.append(worker)
        if not choices:
            return jsonify({"error": "no connected worker has a free render slot"}), 409
        worker = choices[0]
        worker_id = worker["worker_id"]
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "queued":
            return jsonify({"error": "only queued jobs can be sent to a worker"}), 409
        try:
            profile_data = orchestrator.load_profile(job["profile"])
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"profile snapshot failed: {e}"}), 500
        job.update(status="dispatched", step="fleet", dispatched_to=worker_id,
                   dispatched_at=datetime.now().isoformat(timespec="seconds"))
        spec = {"job_id": job_id, "inputs": job["inputs"],
                "profile": job["profile"], "profile_data": profile_data}
    with _FLEET_LOCK:
        FLEET_WORKERS[worker_id].setdefault("dispatch", {})[job_id] = spec
    _persist_queue()
    return jsonify({"ok": True, "worker_id": worker_id})


@app.route("/api/jobs")
def api_jobs():
    """Active/recent jobs — lets a reloaded page re-attach + show the queue.
    Restricted users only see jobs from their allowed profiles.
    Queued jobs are ordered FIFO (oldest first); others newest first after."""
    with LOCK:
        items = list(JOBS.items())
    # Queue position among still-queued jobs (1-based)
    queued_ordered = sorted(
        [(jid, j) for jid, j in items if j.get("status") == "queued"],
        key=lambda x: x[1].get("created_at") or "",
    )
    qpos = {jid: i + 1 for i, (jid, _) in enumerate(queued_ordered)}
    settings = config.load_settings()
    _host_cache: dict[str, bool] = {}

    def _host_enabled(profile: str) -> bool:
        # A handful of tiny profile files, re-read at most once per profile
        # per request (this endpoint is polled every 3s) — not worth a
        # process-lifetime cache given profiles can be hand-edited live.
        if profile not in _host_cache:
            try:
                _host_cache[profile] = bool(
                    orchestrator.load_profile(profile).get("host", {}).get("enabled"))
            except Exception:  # noqa: BLE001
                _host_cache[profile] = False
        return _host_cache[profile]

    def _item(jid: str, j: dict) -> dict:
        profile = j.get("profile", "")
        chars = j.get("chars") or j.get("est_chars") or 0
        beats = j.get("est_beats") or 0
        size = j.get("size") or (
            scheduler.classify_size(
                chars,
                config.queue_setting("small_threshold_chars", profile, settings),
                config.queue_setting("big_threshold_chars", profile, settings),
            ) if chars else None)
        return {
            "job_id": jid,
            "status": j["status"],
            "title": j.get("title", ""),
            "profile": profile,
            "step": j.get("step", ""),
            "created_at": j.get("created_at", ""),
            "queue_pos": qpos.get(jid),
            "error": j.get("error"),
            "progress_pct": j.get("progress_pct", 0),
            "est_chars": j.get("est_chars"),
            "chars": j.get("chars"),
            "size": size,
            "eta_seconds": stats.eta_seconds(profile, chars) if chars else None,
            "cost_estimate_usd": (
                stats.estimate_cost_usd(chars, beats, _host_enabled(profile))
                if chars else None),
            "retries": j.get("retries", 0),
            # Drives the Stop button: only a job with a live cancel token
            # can actually be stopped, and retry_wait can be pulled out of
            # its wait.
            "stoppable": bool(j.get("_cancel")) or j.get("status") == "retry_wait",
            "prioritized": bool(j.get("priority_at")),
        }

    # Show: active first, then queued (FIFO), then recent finished
    active, queued, done = [], [], []
    for jid, j in items:
        if not _allowed_profile(j.get("profile", "")):
            continue
        st = j.get("status")
        row = _item(jid, j)
        if st in _ACTIVE:
            active.append(row)
        elif st == "queued":
            queued.append(row)
        else:
            done.append(row)
    queued.sort(key=lambda r: r.get("created_at") or "")
    done.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return jsonify(active + queued + done[:30])


@app.route("/api/queue/<job_id>", methods=["DELETE"])
def cancel_queued(job_id: str):
    """Remove a job that is still waiting in the queue (not started)."""
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "queued":
            return jsonify({
                "error": f"can only cancel queued jobs (status={job.get('status')})"
            }), 409
        job["status"] = "cancelled"
        job["step"] = "cancelled"
        job["log"].append({
            "t": datetime.now().strftime("%H:%M:%S"),
            "step": "queue",
            "message": "Removed from queue",
        })
    _persist_queue()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/queue/<job_id>/stop", methods=["POST"])
def stop_running(job_id: str):
    """Stop a RUNNING job. Default is stop-and-requeue: everything already
    generated stays on disk, so resuming continues from there instead of
    re-rendering (and re-paying for) the whole video. Pass
    {"cancel": true} to stop it for good instead.
    """
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    data = request.get_json(silent=True) or {}
    requeue = not bool(data.get("cancel"))
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") == "retry_wait":
            # Waiting to auto-retry — no thread to stop, just take it out.
            job["status"] = "queued" if requeue else "cancelled"
            job["step"] = job["status"]
            token = None
        else:
            token = job.get("_cancel")
            if token is None:
                return jsonify({
                    "error": f"job is not running (status={job.get('status')})"
                }), 409
            job["_requeue_on_stop"] = requeue
    # Outside LOCK: cancel() kills child processes, and the job's own thread
    # needs the lock to record its stopped state.
    killed = token.cancel() if token is not None else 0
    _persist_queue()
    return jsonify({"ok": True, "job_id": job_id, "requeued": requeue,
                    "killed_processes": killed})


@app.route("/api/queue/pause", methods=["GET", "POST"])
def queue_pause():
    """Pause/resume picking up NEW jobs. Running jobs are left alone —
    pausing is for "don't start anything else", not "stop everything"."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        paused = bool(data.get("paused"))
        config.save_settings({"queue": {"queue_paused": paused}})
        if not paused:
            _kick_queue()  # resume immediately rather than waiting for an event
    return jsonify({"paused": bool(config.queue_setting("queue_paused"))})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        payload = {k: v for k, v in job.items() if not k.startswith("_")}
    # Surface which final files exist (Patreon split → two halves)
    job_dir = config.OUTPUT_DIR / job_id
    payload["videos"] = {
        "final": (job_dir / "final.mp4").exists(),
        "part1": (job_dir / "final_part1.mp4").exists(),
        "part2": (job_dir / "final_part2.mp4").exists(),
    }
    cut_meta = job_dir / "patreon_cut.json"
    if cut_meta.exists():
        try:
            payload["patreon_cut"] = json.loads(cut_meta.read_text())
        except Exception:  # noqa: BLE001
            pass
    return jsonify(payload)


@app.route("/api/video/<job_id>")
def video(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    # part=1 → free half, part=2 → paid half, default → final.mp4 (free half
    # when Patreon-split, otherwise the single full render)
    part = (request.args.get("part") or "").strip()
    job_dir = config.OUTPUT_DIR / job_id
    if part in ("1", "part1", "free"):
        path = job_dir / "final_part1.mp4"
        suffix = " part1-free"
    elif part in ("2", "part2", "paid", "patreon"):
        path = job_dir / "final_part2.mp4"
        suffix = " part2-paid"
    else:
        path = job_dir / "final.mp4"
        # Prefer free half if final is missing but parts exist
        if not path.exists() and (job_dir / "final_part1.mp4").exists():
            path = job_dir / "final_part1.mp4"
        suffix = ""
    with LOCK:
        job = JOBS.get(job_id) or {}
        drive_files = ((job.get("drive") or {}).get("files") or {})
    drive_role = "part1" if part in ("1", "part1", "free") else (
        "part2" if part in ("2", "part2", "paid", "patreon") else "final")
    # Default view is the free half for a Patreon split, just like the local
    # fallback above. Files stay private; this route proxies Drive only after
    # the app's normal profile/auth checks have passed.
    if not path.exists() and drive_role == "final" and "part1" in drive_files:
        drive_role = "part1"
    drive_file = drive_files.get(drive_role) or {}
    if not path.exists() and drive_file.get("id"):
        try:
            upstream = DRIVE.download_response(drive_file["id"], request.headers.get("Range", ""))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Drive video unavailable: {e}"}), 502
        headers = {k: upstream.headers[k] for k in (
            "Accept-Ranges", "Content-Length", "Content-Range", "ETag",
        ) if k in upstream.headers}
        download = request.args.get("download") == "1"
        disposition = "attachment" if download else "inline"
        filename = drive_file.get("name") or f"{job_id}{suffix}.mp4"
        headers["Content-Disposition"] = f'{disposition}; filename="{filename.replace(chr(34), "")}"'
        return Response(upstream.iter_content(chunk_size=1024 * 1024),
                        status=upstream.status_code,
                        headers=headers,
                        content_type=upstream.headers.get("Content-Type", "video/mp4"))
    if not path.exists():
        abort(404)
    # ?download=1 → save-as instead of playing in the browser tab
    download = request.args.get("download") == "1"
    with LOCK:
        title = (JOBS.get(job_id) or {}).get("title")
    if not title:  # server restarted since — the job dir remembers
        f = job_dir / "inputs.json"
        if f.exists():
            title = (json.loads(f.read_text()).get("title") or "").strip()
    title = title or job_id
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[:80]
    return send_file(path, mimetype="video/mp4", as_attachment=download,
                     download_name=f"{safe or job_id}{suffix}.mp4")


def _drive_redirect_uri() -> str:
    """Use a Desktop OAuth loopback callback, never a public/Tailscale URL."""
    if config.GDRIVE_REDIRECT_URI:
        return config.GDRIVE_REDIRECT_URI
    port = request.environ.get("SERVER_PORT") or "80"
    suffix = "" if port == "80" else f":{port}"
    return f"http://localhost{suffix}/api/drive/callback"


@app.route("/api/drive/status")
def drive_status():
    return jsonify(DRIVE.status())


@app.route("/api/drive/authorize")
def drive_authorize():
    if not DRIVE.configured:
        return jsonify({"error": "Set GDRIVE_CLIENT_SECRET_FILE in .env and restart"}), 409
    state = secrets.token_urlsafe(32)
    redirect_uri = _drive_redirect_uri()
    with _DRIVE_AUTH_LOCK:
        now = datetime.now().timestamp()
        _DRIVE_AUTH_STATES.clear()
        _DRIVE_AUTH_STATES[state] = (redirect_uri, now + 600)
    return redirect(DRIVE.authorization_url(redirect_uri, state))


@app.route("/api/drive/callback")
def drive_callback():
    state = request.args.get("state") or ""
    code = request.args.get("code") or ""
    with _DRIVE_AUTH_LOCK:
        saved = _DRIVE_AUTH_STATES.pop(state, None)
    if not saved or saved[1] < datetime.now().timestamp():
        return "Drive authorization expired or did not match this app. Start again.", 400
    if not code:
        return "Google Drive authorization was declined.", 400
    try:
        DRIVE.complete_authorization(code, saved[0])
        return ("Google Drive connected. Existing completed local videos can be "
                "uploaded with POST /api/drive/retry/&lt;job-id&gt;.")
    except Exception as e:  # noqa: BLE001
        return f"Google Drive authorization failed: {e}", 502


@app.route("/api/drive/retry/<job_id>", methods=["POST"])
def drive_retry(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        drive_state = (job.get("drive") or {}).get("state")
        if drive_state == "uploading":
            return jsonify({"error": "Drive upload already running"}), 409
        if not _drive_deliverables(config.OUTPUT_DIR / job_id):
            return jsonify({"error": "no local deliverable available to upload"}), 409
        job["drive"] = {"state": "uploading", "files": (job.get("drive") or {}).get("files") or {}}
    _persist_queue()
    threading.Thread(target=_upload_job_deliverables,
                     args=(job_id, config.OUTPUT_DIR / job_id), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/host/<profile>")
def host_image(profile: str):
    if not _allowed_profile(profile):
        abort(403)
    """Preview of the host fallback still for a profile (404 if none)."""
    path = config.HOSTS_DIR / f"{Path(profile).stem}.png"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Live-togglable settings (gpu_encode today). Read once per job at
    job-start (see orchestrator.py) — changing this never affects a job
    already running, only ones started after the change."""
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        patch = {}
        if "gpu_encode" in data:
            patch["gpu_encode"] = bool(data["gpu_encode"])
        settings = config.save_settings(patch)
    else:
        settings = config.load_settings()
    return jsonify({
        "gpu_encode": bool(settings.get("gpu_encode", False)),
        "hardware": {
            "cpu_cores": config._detect_cpu_count(),
            "ram_gb": round(config._detect_ram_gb(), 1),
            "gpu_encode_available": config.gpu_encode_available(),
        },
    })


# ── Thumbnail tool — standalone, no relation to the video pipeline/queue ──
THUMB_DIR = config.ROOT / "thumbnails"
THUMB_TEMPLATES_DIR = THUMB_DIR / "templates"
THUMB_OUT_DIR = THUMB_DIR / "generated"
THUMB_UPLOADS_DIR = THUMB_DIR / "uploads"
for _d in (THUMB_TEMPLATES_DIR, THUMB_OUT_DIR, THUMB_UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/webp": ".webp"}


def _save_upload(file_storage, dest_dir: Path, stem: str) -> Path:
    ext = _IMG_EXT.get((file_storage.mimetype or "").lower())
    if not ext:
        ext = Path(file_storage.filename or "").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
    dest = dest_dir / f"{stem}{ext}"
    file_storage.save(dest)
    return dest


@app.route("/thumbnails")
def thumbnails_page():
    default_tpl = next(iter(THUMB_TEMPLATES_DIR.glob("default.*")), None)
    return render_template(
        "thumbnails.html",
        has_default_template=bool(default_tpl),
        missing_key=not bool(config.KIE_THUMBNAIL_API_KEY),
        default_model=config.KIE_THUMBNAIL_MODEL)


@app.route("/api/thumbnail", methods=["POST"])
def api_thumbnail():
    """One-shot synchronous thumbnail edit (no queue — a single kie.ai call
    takes well under the render pipeline's timescale)."""
    if not config.KIE_THUMBNAIL_API_KEY:
        return jsonify({"error": "KIE_THUMBNAIL_API_KEY is not set in .env"}), 400

    main_text = (request.form.get("main_text") or "").strip()
    red_word = (request.form.get("red_word") or "").strip()
    yellow_word = (request.form.get("yellow_word") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not (main_text and red_word and yellow_word and name):
        return jsonify({
            "error": "main_text, red_word, yellow_word, and name are all "
                     "required"}), 400

    character = request.files.get("character")
    if not character or not character.filename:
        return jsonify({
            "error": "a character reference image is required"}), 400

    job_id = uuid.uuid4().hex[:12]
    template_file = request.files.get("template")
    save_default = request.form.get("save_as_default") == "1"

    if template_file and template_file.filename:
        if save_default:
            for old in THUMB_TEMPLATES_DIR.glob("default.*"):
                old.unlink(missing_ok=True)
            template_path = _save_upload(template_file, THUMB_TEMPLATES_DIR,
                                         "default")
        else:
            template_path = _save_upload(template_file, THUMB_UPLOADS_DIR,
                                         f"{job_id}_tpl")
    else:
        template_path = next(iter(THUMB_TEMPLATES_DIR.glob("default.*")), None)
        if not template_path:
            return jsonify({
                "error": "no template image provided and no default "
                         "template saved yet — upload one and tick "
                         "“save as default”"}), 400

    character_path = _save_upload(character, THUMB_UPLOADS_DIR,
                                  f"{job_id}_char")

    model = (request.form.get("model") or "").strip() or None

    dest = THUMB_OUT_DIR / f"{job_id}.png"
    try:
        thumbnail.generate(template_path, character_path, dest,
                           main_text=main_text, red_word=red_word,
                           yellow_word=yellow_word, name=name, model=model)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    return jsonify({"job_id": job_id, "url": f"/api/thumbnail/file/{job_id}"})


@app.route("/api/thumbnail/file/<job_id>")
def api_thumbnail_file(job_id: str):
    path = THUMB_OUT_DIR / f"{job_id}.png"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/png")


if __name__ == "__main__":
    import os
    if config.FLEET_ROLE not in ("off", "main", "worker"):
        raise SystemExit("FLEET_ROLE must be off, main, or worker")
    if config.FLEET_ROLE == "main" and (not ACCESS or not config.FLEET_PAIRING_TOKEN):
        raise SystemExit("fleet main requires ACCESS_KEYS and FLEET_PAIRING_TOKEN")
    if config.FLEET_ROLE == "worker" and (not config.FLEET_MAIN_URL or not config.FLEET_PAIRING_TOKEN):
        raise SystemExit("fleet worker requires FLEET_MAIN_URL and FLEET_PAIRING_TOKEN")
    port = int(os.environ.get("PORT", 5062))
    host = (os.environ.get("HOST") or "").strip() or (
        "0.0.0.0" if ACCESS else "127.0.0.1")
    _vt = "available" if config.gpu_encode_available() else "unavailable"
    print(f"Dmiqwi → http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port} "
         f"(MAX_CONCURRENT_JOBS={config.MAX_CONCURRENT_JOBS}, "
         f"beat_workers: {config.resolve_beat_workers(False)} CPU / "
         f"{config.resolve_beat_workers(True)} GPU, "
         f"VideoToolbox: {_vt})")
    # Restore any queue that survived the previous process, then drain it.
    _restore_queue()
    atexit.register(_persist_queue)
    if config.FLEET_ROLE == "worker":
        FLEET_WORKER = fleet.FleetWorker(_run_fleet_worker_job)
        FLEET_WORKER.start()
        print(f"[fleet] worker {FLEET_WORKER.worker_id} polling {config.FLEET_MAIN_URL}")
    app.run(host=host, port=port, debug=False, threaded=True)
