"""Dmiqwi — local video studio web UI."""
from __future__ import annotations

import atexit
import hmac
import json
import platform
import re
import secrets
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, g, jsonify, make_response, redirect,
                   render_template, request, send_file, abort)

from pipeline import (ai33_tasks, analytics, animation, artifacts, cancel,
                      clients, cluster, config, delivery_doc, fleet, identity,
                      hyperframes, jobs, llm, node_monitor, operations, orchestrator,
                      render as video_render, research, resources, scheduler,
                      stats, storage, telemetry, thumbnail)

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
_ACTIVE = frozenset({"researching", "awaiting_title", "running",
                     "awaiting_approval", "dispatched", "uploading",
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
_DRIVE_UPLOAD_LOCK = threading.Lock()
FLEET_WORKERS: dict[str, dict] = {}
_FLEET_LOCK = threading.Lock()
FLEET_WORKER: fleet.FleetWorker | None = None
OPS: operations.OperationsCenter | None = None
_FLEET_MODES = frozenset({"active", "draining", "paused"})
_CLUSTER_HISTORY = None
_NODE_MONITOR = None
_CLUSTER_INIT_LOCK = threading.Lock()
_CLUSTER_STOP = threading.Event()
UPDATE_LOCK_FILE = config.OUTPUT_DIR / ".southcube-update.lock"
UPDATE_STATUS_FILE = config.OUTPUT_DIR / "update_status.json"


def _cluster_history():
    global _CLUSTER_HISTORY
    path = config.OUTPUT_DIR / "cluster_history.json"
    with _CLUSTER_INIT_LOCK:
        if _CLUSTER_HISTORY is None or _CLUSTER_HISTORY.path != path:
            _CLUSTER_HISTORY = cluster.ClusterHistory(path)
        return _CLUSTER_HISTORY


def _node_monitor():
    global _NODE_MONITOR
    with _CLUSTER_INIT_LOCK:
        if _NODE_MONITOR is None or _NODE_MONITOR.output_dir != config.OUTPUT_DIR:
            _NODE_MONITOR = node_monitor.NodeMonitor(config.ROOT, config.OUTPUT_DIR)
        return _NODE_MONITOR


def _safe_node_report(value) -> dict:
    """Accept only the documented, secret-free worker telemetry fields."""
    if not isinstance(value, dict):
        return {}
    clean = node_monitor.sanitize(value)
    if not isinstance(clean, dict):
        return {}
    return {key: clean.get(key) for key in (
        "sampled_at", "instance_id", "state", "cpu", "memory", "gpu",
        "processes", "changes") if key in clean}


def _update_status() -> dict:
    status = artifacts.read(UPDATE_STATUS_FILE)
    if not status:
        status = {"state": "manual", "message": "Automatic update supervisor is not running",
                  "supervised": False, "auto_update": False,
                  "local_revision": fleet.application_revision()}
    return node_monitor.sanitize(status)


def _fleet_policy(worker_id: str, settings: dict | None = None) -> dict:
    """Return one worker's persisted, sanitized controller policy."""
    settings = settings if settings is not None else config.load_settings()
    fleet_settings = settings.get("fleet_workers", {}) or {}
    if not isinstance(fleet_settings, dict):
        fleet_settings = {}
    raw = fleet_settings.get(worker_id, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or "active")
    if mode not in _FLEET_MODES:
        mode = "active"
    max_jobs = raw.get("max_jobs")
    try:
        max_jobs = max(1, min(8, int(max_jobs))) if max_jobs is not None else None
    except (TypeError, ValueError):
        max_jobs = None
    return {"mode": mode, "max_jobs": max_jobs}


def _fleet_capacity(worker: dict, policy: dict | None = None) -> dict:
    """Capacity the controller may actually assign, including in-flight work.

    A heartbeat's reported free count does not include dispatches that have
    been sent but not acknowledged yet. Subtracting those pending deliveries
    prevents the controller from overfilling a worker on consecutive polls.
    """
    policy = policy or _fleet_policy(str(worker.get("worker_id") or ""))
    reported = worker.get("capacity") if isinstance(worker.get("capacity"), dict) else {}
    try:
        physical_max = max(0, min(8, int(reported.get("max_jobs") or 0)))
    except (TypeError, ValueError):
        physical_max = 0
    try:
        reported_free = max(0, min(physical_max, int(reported.get("free") or 0)))
    except (TypeError, ValueError):
        reported_free = 0
    reported_jobs = worker.get("jobs") if isinstance(worker.get("jobs"), list) else []
    active = sum(
        1 for row in reported_jobs
        if isinstance(row, dict)
        and row.get("status") in fleet.SLOT_STATUSES
    )
    # v2 workers report this directly; keep the job-list count as a fallback
    # for old workers and use the larger value when a partial list arrives.
    try:
        active = max(active, max(0, int(reported.get("active") or 0)))
    except (TypeError, ValueError):
        pass
    pending = len(worker.get("dispatch") or {})
    override = policy.get("max_jobs")
    effective_max = min(physical_max, override) if override is not None else physical_max
    effective_free = min(reported_free, max(0, effective_max - active))
    effective_free = max(0, effective_free - pending)
    if policy.get("mode") != "active":
        effective_free = 0
    metrics = worker.get("metrics") or {}
    free_gb = (metrics.get("disk") or {}).get("free_gb")
    memory_gb = (metrics.get("render") or {}).get("available_memory_gb")
    if isinstance(free_gb, (int, float)) and free_gb < 3:
        effective_free = 0
    if isinstance(memory_gb, (int, float)) and memory_gb < 1:
        effective_free = 0
    return {
        "max_jobs": physical_max,
        "reported_free": reported_free,
        "effective_max_jobs": effective_max,
        "active": active,
        "pending": pending,
        "free": effective_free,
        "effective_free": effective_free,
    }


def _fleet_worker_rows() -> list[dict]:
    """Secret-free, UI-ready fleet view with job assignments and health."""
    now = datetime.now().timestamp()
    with LOCK:
        assignments: dict[str, list[dict]] = {}
        for jid, job in JOBS.items():
            wid = job.get("dispatched_to")
            if not wid or job.get("status") == "cancelled":
                continue
            assignments.setdefault(str(wid), []).append({
                "job_id": jid,
                "title": job.get("publication_title") or job.get("title") or jid,
                "profile": job.get("profile") or "",
                "status": job.get("status") or "unknown",
                "step": job.get("step") or "",
                "progress_pct": job.get("progress_pct") or 0,
            })
    settings = config.load_settings()
    rows: list[dict] = []
    with _FLEET_LOCK:
        for wid, worker in FLEET_WORKERS.items():
            policy = _fleet_policy(wid, settings)
            capacity = _fleet_capacity(worker, policy)
            try:
                age = max(0, now - datetime.fromisoformat(worker["last_seen"]).timestamp())
            except Exception:  # noqa: BLE001
                age = config.FLEET_STALE_SECONDS + 1
            stale = age > config.FLEET_STALE_SECONDS
            online = not stale and capacity["max_jobs"] > 0
            available = online and capacity["effective_free"] > 0
            if stale:
                health = "offline"
            elif policy["mode"] == "draining":
                health = "draining"
            elif policy["mode"] == "paused":
                health = "paused"
            elif available:
                health = "available"
            else:
                health = "busy"
            pending_ids = set((worker.get("dispatch") or {}).keys())
            worker_jobs = [
                {**job, "dispatch_pending": job["job_id"] in pending_ids}
                for job in assignments.get(wid, [])
            ]
            rows.append({
                "worker_id": wid,
                "name": str(worker.get("name") or wid),
                "protocol_version": int(worker.get("protocol_version") or 1),
                "hardware": worker.get("hardware") if isinstance(worker.get("hardware"), dict) else {},
                "metrics": worker.get("metrics") if isinstance(worker.get("metrics"), dict) else {},
                "node": worker.get("node") if isinstance(worker.get("node"), dict) else {},
                "capacity": capacity,
                "policy": policy,
                "last_seen": worker.get("last_seen"),
                "age_seconds": round(age),
                "stale": stale,
                "online": online,
                "available": available,
                "health": health,
                "jobs": worker_jobs,
            })
    rows.sort(key=lambda row: (row["stale"], row["name"].lower()))
    return node_monitor.sanitize(rows)

# Phase-weighted progress (0-100), keyed by the real `step` names emitted
# from pipeline/orchestrator.py's emit() calls — each value is that phase's
# START percentage. "render" is by far the longest phase on a long video
# (hundreds of beats), so it gets its own fine-grained per-beat progress on
# top of this coarse table (see _render_progress_pct below).
_PHASE_START_PCT = {
    "start": 0, "outline": 0, "awaiting": 3, "script": 3, "plan": 8,
    "music": 10, "voiceover": 10, "host": 25, "broll": 30, "anim": 30,
    "hyperframes": 30,
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

_LOGIN_PAGE = """<!doctype html><html><head><title>SalCube Studio</title>
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
<form method="post" action="/login"><div class="mark">S</div>
<h2>SalCube <span>Studio</span></h2>
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
    central_path = (request.path in ("/cluster", "/api/cluster", "/api/update/status")
                    or request.path.startswith("/api/ops/"))
    if central_path and config.FLEET_ROLE == "worker":
        return jsonify({"error": "SalCube runs on the main computer; open its SouthCube dashboard"}), 409
    if not ACCESS:
        return None  # single-user localhost mode — unchanged behavior
    if request.path == "/login" or request.path.startswith("/static"):
        return None
    g.user = _user_for(request.cookies.get("studio_key"))
    if g.user:
        owner_path = (central_path or request.path == "/api/workers"
                      or request.path.startswith("/api/workers/"))
        if owner_path and g.user.get("profiles") is not None:
            return jsonify({"error": "SalCube's fleet-wide reports require owner access"}), 403
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


def _profile_broll_defaults() -> dict:
    """Profile name → checked B-roll sources when selected in the UI."""
    import json as _json
    standard = ["stock", "stock_image", "ai_video", "animation"]
    out = {}
    for p in config.PROFILES_DIR.glob("*.json"):
        try:
            v = _json.loads(p.read_text()).get("video", {})
            chosen = v.get("broll_defaults")
            out[p.stem] = chosen if isinstance(chosen, list) else standard
        except Exception:  # noqa: BLE001
            out[p.stem] = standard
    return out


def _active_slot_count() -> int:
    # A render running on a fleet worker consumes that worker's capacity,
    # not one of this controller Mac's local slots. Counting remote work here
    # left the Mac idle whenever Windows had MAX_CONCURRENT_JOBS jobs active.
    return sum(1 for j in JOBS.values()
               if (j.get("status") in _SLOT_STATUSES
                   or (j.get("_fleet_reporter") and j.get("status") in fleet.SLOT_STATUSES))
               and not j.get("dispatched_to"))


def _next_queued_ids(n: int) -> list[str]:
    """Up to `n` still-queued job ids, in the order they should start.

    FIFO by created_at, except that anything carrying `priority_at` sorts
    ahead of everything else — that's how a manual retry starts next
    instead of joining the back of the line, and how a job preempted to
    make room for it gets the following slot rather than starving.
    """
    if n <= 0:
        return []
    queued = [(jid, j) for jid, j in JOBS.items() if j.get("status") == "queued"
              and not j.get("_fleet_reporter") and not j.get("dispatched_to")]
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
                "publication_title": j.get("publication_title"),
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
                "attempt_id": j.get("attempt_id"),
                "export_validation_required": j.get("export_validation_required", False),
                "performance": j.get("performance"),
                "retries": j.get("retries", 0),
                "retry_at": j.get("retry_at"),
                "retry_limit": j.get("retry_limit"),
                "priority_at": j.get("priority_at"),
                "updated_at": j.get("updated_at"),
                "video_group": j.get("video_group"),
                "local_only": bool(j.get("local_only")),
                "research_attempt": j.get("research_attempt", 0),
                "title_options": j.get("title_options"),
                "package": j.get("package"),
                "thumbnail": j.get("thumbnail"),
                "description": j.get("description"),
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
    thumbnail_resume: list[tuple[str, str]] = []
    research_resume: list[str] = []
    package_resume: list[str] = []
    delivery_resume: list[str] = []
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
            has_local_deliverable = (video_render.is_valid_video(final)
                                     or video_render.is_valid_video(part1))
            if row.get("export_validation_required"):
                has_local_deliverable = bool(_verified_delivery_roles(
                    final.parent, drive.get("files") or {}))
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
            elif st == "paused":
                # A deliberate Stop must survive a restart, or restarting
                # the server would silently undo it.
                step = "paused"
                progress_pct = row.get("progress_pct", 0)
                log = list(row.get("log") or [])
            elif st == "researching":
                step = "research"
                progress_pct = 0
                log = list(row.get("log") or [])
                research_resume.append(jid)
            elif st == "awaiting_title":
                step = "title"
                progress_pct = 0
                log = list(row.get("log") or [])
            elif (row.get("dispatched_to")
                  and st in ("running", "awaiting_approval", "dispatched", "retry_wait")):
                # The process is still alive on another machine when only
                # the controller restarts. Keep ownership remote and wait
                # for its next heartbeat instead of accidentally starting a
                # duplicate render on this Mac.
                st = "retry_wait" if st == "retry_wait" else "dispatched"
                step = "retry_wait" if st == "retry_wait" else "fleet"
                progress_pct = row.get("progress_pct", 0)
                log = list(row.get("log") or [])
                log.append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "step": "fleet",
                    "message": "Controller restarted — waiting for the assigned worker",
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
            restored_thumbnail = (row.get("thumbnail")
                                  if isinstance(row.get("thumbnail"), dict) else {})
            if not _profile_uses_thumbnail(profile):
                # Profile configuration is authoritative for old persisted
                # jobs too; do not resurrect a pre-update thumbnail thread.
                restored_thumbnail = {"state": "disabled"}
            # A background thumbnail thread dies on restart just like a
            # render thread. Remember which independent approval task to
            # resume after releasing LOCK.
            if restored_thumbnail.get("state") in ("generating_words", "generating_image"):
                thumbnail_resume.append((jid, restored_thumbnail["state"]))
            JOBS[jid] = {
                "status": st,
                "step": step,
                "progress_pct": progress_pct,
                "log": log,
                "title": title,
                "publication_title": (row.get("publication_title") or title
                                      if not _profile_uses_thumbnail(profile)
                                      else row.get("publication_title")),
                "profile": profile,
                "inputs": inputs,
                "created_at": row.get("created_at") or datetime.now().isoformat(
                    timespec="seconds"),
                "error": row.get("error") if st in ("error", "retry_wait") else None,
                "result": str(final) if final.exists() else row.get("result"),
                "drive": drive,
                "dispatched_to": row.get("dispatched_to"),
                "dispatched_at": row.get("dispatched_at"),
                "attempt_id": row.get("attempt_id"),
                "export_validation_required": row.get("export_validation_required", False),
                "performance": row.get("performance"),
                "est_chars": row.get("est_chars"),
                "chars": row.get("chars"),
                "est_beats": row.get("est_beats"),
                "size": row.get("size"),
                "retries": row.get("retries", 0),
                "retry_at": row.get("retry_at"),
                "retry_limit": row.get("retry_limit"),
                "priority_at": row.get("priority_at"),
                "updated_at": row.get("updated_at") or row.get("created_at"),
                "video_group": row.get("video_group") or inputs.get("video_group"),
                "local_only": bool(row.get("local_only") or inputs.get("local_only")),
                "research_attempt": int(row.get("research_attempt") or 0),
                "title_options": (row.get("title_options")
                                  if isinstance(row.get("title_options"), dict)
                                  else {}),
                "package": (row.get("package")
                            if isinstance(row.get("package"), dict) else {}),
                "thumbnail": restored_thumbnail,
                "description": (row.get("description")
                                if isinstance(row.get("description"), dict)
                                else {"state": "waiting_for_rules"}),
            }
            package_state = (JOBS[jid].get("package") or {}).get("state")
            if (has_local_deliverable and row.get("status") == "uploading"
                    and not row.get("dispatched_to") and not drive_complete):
                delivery_resume.append(jid)
            if (drive_complete and _profile_wants_docx(profile)
                    and package_state != "uploaded"):
                package_resume.append(jid)
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
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "video_group": saved.get("video_group"),
                    "thumbnail": {},
                    "description": {"state": "waiting_for_rules"},
                }
    if restored_q:
        print(f"[queue] restored {restored_q} incomplete job(s) from "
              f"{QUEUE_STATE_FILE.name}")
    _persist_queue()
    _kick_queue()
    for jid in research_resume:
        _start_profile_research(jid)
    for jid in package_resume:
        threading.Thread(
            target=_upload_profile_docx,
            args=(jid, config.OUTPUT_DIR / jid), daemon=True,
            name=f"dmiqwi-docx-{jid}").start()
    for jid in delivery_resume:
        threading.Thread(target=_upload_job_deliverables,
                         args=(jid, config.OUTPUT_DIR / jid), daemon=True,
                         name=f"drive-resume-{jid}").start()
    for jid, thumb_state in thumbnail_resume:
        if thumb_state == "generating_image":
            threading.Thread(target=_thumbnail_image_task, args=(jid,), daemon=True,
                             name=f"dmiqwi-thumbnail-{jid}").start()
        else:
            _start_thumbnail_words(jid)


def _schedule_retry(job_id: str) -> None:
    """Put a failed job back in the queue after a short delay.

    Delayed rather than immediate because the failures this actually
    catches are provider outages (kie.ai 5xx), where retrying instantly
    just spends an attempt on a service that is still down. The job sits in
    "retry_wait" meanwhile, which holds no slot, so other work proceeds.
    Fleet retries stay inside _run_fleet_worker_job and never use this queue.
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
            job["priority_at"] = "1:" + datetime.now().isoformat(timespec="seconds")
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
        # "queued", not "paused": this job didn't ask to stop, so it must
        # come back on its own once a slot frees.
        victim["_stop_target"] = "queued"
        # Priority is "<class>:<time>" and sorts as a plain string, so the
        # class prefix decides who goes first. The victim MUST rank below
        # the retry it is making room for ("1:" vs "0:") — ranking it first
        # meant it instantly reclaimed the slot it had just freed, which
        # defeated the whole preemption.
        victim["priority_at"] = "1:" + datetime.now().isoformat(timespec="seconds")
        victim["log"].append({
            "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
            "message": (f"Paused at {victim.get('progress_pct', 0)}% so a "
                        f"manual retry could start — resumes next")})
    token.cancel()
    return True


def _abort_local_job(job_id: str) -> None:
    """Stop a job running on THIS machine because main asked us to.

    Used on a fleet worker: main has no ffmpeg of its own to kill for a
    dispatched job, so the stop has to be carried out where the work is.
    """
    with LOCK:
        job = JOBS.get(job_id)
        token = job.get("_cancel") if job else None
        if job is not None:
            job["_stop_target"] = "cancelled"  # main owns the requeue decision
            if token is None and job.get("status") == "retry_wait":
                job.update(status="cancelled", step="cancelled")
                reporter = job.get("_fleet_reporter")
                if reporter:
                    reporter.fail("stopped by user")
    _persist_queue()
    if token is not None:
        killed = token.cancel()
        print(f"[fleet] aborted {job_id} on request from main "
              f"({killed} process(es) killed)")


def _kick_queue() -> None:
    """Start queued jobs until MAX_CONCURRENT_JOBS workers are busy.

    Safe to call often / concurrently — jobs are claimed (status flipped to
    "running") while still holding LOCK, so two overlapping kicks can never
    double-start the same job.
    """
    if config.queue_setting("queue_paused") or UPDATE_LOCK_FILE.exists():
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
            "_fleet_reporter": reporter,
            "attempt_id": spec.get("attempt_id"),
            "status": "running", "step": "start", "progress_pct": 0,
            "log": [], "title": inputs.get("title") or job_id, "profile": profile,
            "inputs": inputs, "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    try:
        while True:
            _run_job(job_id, inputs, profile, profile_data=profile_data,
                     fleet_reporter=reporter)
            if not _wait_for_fleet_retry(job_id, reporter):
                break
    finally:
        with LOCK:
            job = JOBS.get(job_id)
            if job and job.get("_fleet_reporter") is reporter:
                job.pop("_fleet_reporter", None)
        _persist_queue()
        _kick_queue()


def _wait_for_fleet_retry(job_id: str, reporter: fleet.FleetReporter) -> bool:
    """Keep the same runner, profile snapshot and reserved slot through retries."""
    while True:
        with LOCK:
            job = JOBS.get(job_id)
            if (not job or job.get("_fleet_reporter") is not reporter
                    or job.get("status") != "retry_wait"):
                return False
            if job.get("_stop_target"):
                target = job.pop("_stop_target")
                job.update(status=target, step=target)
                reporter.fail("stopped by user")
                return False
            remaining = float(job.get("retry_at") or 0) - time.time()
            if remaining <= 0:
                job.update(status="running", step="starting")
                job.pop("error", None)
                job.pop("retry_at", None)
                reporter.progress("starting", "Retrying on the same worker", job.get("progress_pct", 0))
                return True
        time.sleep(min(0.5, remaining))


def _fleet_dispatch_for(worker_id: str, free: int) -> list[dict]:
    """Claim eligible queued jobs up to this worker's available capacity."""
    if config.FLEET_ROLE != "main" or free <= 0 or UPDATE_LOCK_FILE.exists():
        return []
    # Re-evaluate against controller policy and unacknowledged deliveries.
    # The free count passed by an old worker can otherwise remain unchanged
    # for several polls and cause multiple jobs to be assigned to one slot.
    with _FLEET_LOCK:
        worker = FLEET_WORKERS.get(worker_id)
        if worker is not None:
            free = min(free, _fleet_capacity(worker)["effective_free"])
    if free <= 0:
        return []
    with LOCK:
        # Workers may prepare available jobs immediately; encode concurrency
        # is budgeted per machine independently of preparation slots.
        # A same-ID retry may deliberately be pinned to main so an older
        # terminal reporter on a worker cannot reclaim it and replay its
        # stale error. Scan past pinned rows so they do not block dispatch of
        # other eligible work.
        job_ids = [jid for jid in _next_queued_ids(len(JOBS))
                   if not JOBS[jid].get("local_only")]
        cached_jobs = set((worker or {}).get("cached_jobs") or [])
        job_ids.sort(key=lambda jid: jid not in cached_jobs)
        job_ids = job_ids[:free]
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
            job["attempt_id"] = secrets.token_hex(12) if int((worker or {}).get("protocol_version", 1)) >= 3 else None
            dispatch.append({"job_id": job_id, "inputs": job["inputs"],
                             "profile": job["profile"], "profile_data": profile_data,
                             "attempt_id": job.get("attempt_id")})
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
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _drive_deliverables(job_dir: Path) -> list[tuple[str, Path]]:
    """Return the real deliverables, never final.mp4's Patreon duplicate."""
    part1 = job_dir / "final_part1.mp4"
    part2 = job_dir / "final_part2.mp4"
    if (video_render.is_valid_video(part1)
            or video_render.is_valid_video(part2)):
        return [(name, path) for name, path in (("part1", part1), ("part2", part2))
                if video_render.is_valid_video(path)]
    final = job_dir / "final.mp4"
    return [("final", final)] if video_render.is_valid_video(final) else []


def _verified_delivery_roles(job_dir: Path, remote_files: dict) -> list[tuple[str, Path]]:
    """Require a completed render, including BOTH halves of a Patreon split.

    An individual export receipt is insufficient: part one can be valid when
    part two fails. The render manifest is published only after all exports.
    Already verified remote roles allow interrupted uploads to finish after
    their corresponding local files have been cleaned up.
    """
    records = artifacts.read(job_dir / "render.manifest.json").get("outputs") or []
    signatures = {Path(row[0]).name: row for row in records
                  if isinstance(row, list) and row and isinstance(row[0], str)}
    if set(signatures) == {"final.mp4"}:
        roles = [("final", job_dir / "final.mp4")]
    elif set(signatures) == {"final.mp4", "final_part1.mp4", "final_part2.mp4"}:
        roles = [("part1", job_dir / "final_part1.mp4"),
                 ("part2", job_dir / "final_part2.mp4")]
    else:
        return []
    for role, path in roles:
        if not path.exists() and (remote_files.get(role) or {}).get("id"):
            continue
        signature = artifacts.signature(path)
        if (signature is None or signature[:3] != signatures[path.name][:3]
                or not artifacts.output_matches(artifacts.read(path.with_suffix(".export.json")), path)
                or not video_render.is_valid_video(path)):
            return []
    return roles


def _profile_uses_thumbnail(profile_name: str) -> bool:
    """Profiles opt out explicitly; the standalone thumbnail tool remains."""
    try:
        thumbnail_cfg = orchestrator.load_profile(profile_name).get("thumbnail") or {}
        return thumbnail_cfg.get("enabled", True) is not False
    except Exception:  # noqa: BLE001
        return True


def _profile_wants_docx(profile_name: str) -> bool:
    """Every real profile gets a cumulative titles document by default."""
    try:
        drive_cfg = orchestrator.load_profile(profile_name).get("drive") or {}
        return drive_cfg.get("titles_docx", True) is not False
    except Exception:  # noqa: BLE001
        return False


def _delivery_title_stem(title: str) -> str:
    """Turn a publication title into a readable, portable Drive filename."""
    stem = re.sub(r"[\x00-\x1f\x7f]+", " ", str(title or ""))
    stem = re.sub(r"\s*[\\/]\s*", " - ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        raise ValueError("naming error: the video title cannot form a filename")
    # Leave room for " - Part 2.mp4" while keeping filenames manageable on
    # Windows workers and in browser Content-Disposition headers.
    return stem[:180].rstrip(" .")


def _video_drive_name(title: str, role: str = "final") -> str:
    stem = _delivery_title_stem(title)
    suffix = {"part1": " - Part 1", "part2": " - Part 2"}.get(role, "")
    return f"{stem}{suffix}.mp4"


def _local_title_conflict(profile_name: str, title: str,
                          *, exclude_job_id: str = "") -> str:
    wanted = _delivery_title_stem(title).casefold()
    with LOCK:
        rows = [(job_id, dict(job)) for job_id, job in JOBS.items()]
    for job_id, job in rows:
        if job_id == exclude_job_id or job.get("profile") != profile_name:
            continue
        if job.get("status") == "cancelled":
            continue
        existing = str(job.get("publication_title") or "").strip()
        # A direct-title profile has no title gate, so its submitted title is
        # already the future Drive filename. Research themes are not filenames.
        if not existing and not (job.get("title_options") or {}):
            existing = str(job.get("title") or "").strip()
        if existing and _delivery_title_stem(existing).casefold() == wanted:
            return job_id
    return ""


def _title_naming_error(profile_name: str, title: str,
                        *, exclude_job_id: str = "") -> str:
    """Return a blocking naming error without starting expensive production."""
    try:
        conflict = _local_title_conflict(
            profile_name, title, exclude_job_id=exclude_job_id)
        if conflict:
            return ("naming error: this profile already has a job with the same "
                    f"video title ({conflict})")
        if DRIVE.configured and DRIVE.authorized:
            with _DRIVE_UPLOAD_LOCK:
                folder_id = DRIVE.ensure_profile_folder(profile_name)
                for role in ("final", "part1", "part2"):
                    name = _video_drive_name(title, role)
                    if DRIVE.find_file(name, folder_id):
                        return f'naming error: "{name}" already exists in Drive'
    except Exception as exc:  # noqa: BLE001
        return f"naming error: Drive could not verify that this title is unique ({exc})"
    return ""


def _upload_profile_docx(job_id: str, job_dir: Path) -> None:
    """Append a finished title to the profile's one cumulative Drive DOCX."""
    with LOCK:
        job = JOBS.get(job_id)
        if not job or not _profile_wants_docx(job.get("profile", "")):
            return
        title = str(job.get("publication_title") or job.get("title") or job_id)
        profile_name = str(job.get("profile") or "")
        drive = dict(job.get("drive") or {})
        files = dict(drive.get("files") or {})
        job["package"] = {"state": "creating"}
    if not DRIVE.configured or not DRIVE.authorized:
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["package"] = {"state": "pending_drive_authorization"}
        _persist_queue()
        return
    try:
        docx_dir = config.OUTPUT_DIR / "profile-documents" / profile_name
        docx_path = docx_dir / delivery_doc.DOCX_NAME
        docx_dir.mkdir(parents=True, exist_ok=True)
        with _DRIVE_UPLOAD_LOCK:
            folder_id = DRIVE.ensure_profile_folder(profile_name)
            remote = DRIVE.find_file(delivery_doc.DOCX_NAME, folder_id)
            if remote:
                DRIVE.download_to_path(remote["id"], docx_path)
                # Retain the last downloaded version as a local recovery copy.
                import shutil
                shutil.copy2(docx_path, docx_path.with_suffix(".previous.docx"))
                changed = delivery_doc.append_titles([title], docx_path)
            else:
                delivery_doc.create([title], docx_path)
                changed = True
            with LOCK:
                current = JOBS.get(job_id)
                if current:
                    current["package"] = {
                        "state": "uploading", "path": str(docx_path)}
            if remote and not changed:
                result = remote
            else:
                if remote and remote.get("modifiedTime"):
                    latest = DRIVE.find_file(delivery_doc.DOCX_NAME, folder_id)
                    if not latest or latest.get("id") != remote["id"] or latest.get("modifiedTime") != remote["modifiedTime"]:
                        raise storage.DriveError("Word document changed during this update; retry to include the latest edits")
                result = DRIVE.upload(
                    docx_path, dest_name=delivery_doc.DOCX_NAME,
                    state_file=docx_dir / ".gdrive-titles.json",
                    content_type=("application/vnd.openxmlformats-officedocument."
                                  "wordprocessingml.document"),
                    folder_id=folder_id,
                    file_id=(remote or {}).get("id"))
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                drive = dict(job.get("drive") or {})
                files = dict(drive.get("files") or {})
                files["package"] = result
                drive["files"] = files
                job["drive"] = drive
                job["package"] = {"state": "uploaded", "file": result}
        _append_job_log(
            job_id, "drive",
            f'Title added to {delivery_doc.DOCX_NAME} in the {profile_name} folder')
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["package"] = {"state": "error", "error": str(exc)}
        _append_job_log(job_id, "drive", f"DOCX package failed ({exc})")
    _persist_queue()


def _upload_job_deliverables(job_id: str, job_dir: Path) -> None:
    """Best-effort cloud handoff; a Drive issue must never erase the local file."""
    if not DRIVE.configured:
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "not_configured", "files": (job.get("drive") or {}).get("files") or {}}
        _append_job_log(job_id, "drive", "Drive is not configured — keeping local deliverable")
        _persist_queue()
        return
    if not DRIVE.authorized:
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": "pending_authorization", "files": (job.get("drive") or {}).get("files") or {}}
        _append_job_log(job_id, "drive", "Drive needs one-time browser authorization — keeping local deliverable")
        _persist_queue()
        return

    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        profile_name = str(job.get("profile") or "")
        title = str(job.get("publication_title") or job.get("title") or job_id)
        drive = dict(job.get("drive") or {})
        files = dict(drive.get("files") or {})
        job["drive"] = {"state": "uploading", "files": files}
        requires_validation = bool(job.get("export_validation_required"))
    _append_job_log(job_id, "drive", "Uploading verified deliverable(s) to Drive…")
    _persist_queue()

    try:
        with _DRIVE_UPLOAD_LOCK:
            folder_id = DRIVE.ensure_profile_folder(profile_name)
        deliverables = (_verified_delivery_roles(job_dir, files) if requires_validation
                        else _drive_deliverables(job_dir))
        if requires_validation and not deliverables:
            raise storage.DriveError("Render is incomplete or changed; every expected final export must be verified before delivery")
        if not deliverables and not files:
            raise storage.DriveError("No local deliverable or verified remote file is available")
        expected_roles = [role for role, _ in deliverables]
        for role, path in deliverables:
            if role in files and not path.exists():
                continue
            if (requires_validation and not artifacts.output_matches(
                    artifacts.read(path.with_suffix(".export.json")), path)):
                raise storage.DriveError(f"{path.name} has no matching verified final export; rerender before delivery")
            dest_name = _video_drive_name(title, role)
            with _DRIVE_UPLOAD_LOCK:
                existing = DRIVE.find_file(dest_name, folder_id)
                known_id = (files.get(role) or {}).get("id")
                if existing and existing.get("id") != known_id:
                    raise storage.DriveError(
                        f'naming error: "{dest_name}" already exists in the '
                        f"{profile_name} Drive folder")

                def upload_progress(update):
                    with LOCK:
                        current = JOBS.get(job_id)
                        if current:
                            current["drive"] = {"state": "uploading", "files": dict(files),
                                                "current_file": role, "expected_files": expected_roles,
                                                "progress": update}
                    reporter = getattr(cancel.current(), "fleet_reporter", None)
                    if reporter:
                        reporter.uploading({"state": "uploading", "files": dict(files),
                                             "current_file": role, "progress": update})
                result = DRIVE.upload(
                    path, dest_name=dest_name,
                    state_file=job_dir / f".gdrive-{role}.json",
                    folder_id=folder_id, file_id=known_id, on_progress=upload_progress)
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
                job["drive"] = {"state": "uploaded", "files": files,
                                "profile_folder_id": folder_id}
        _append_job_log(job_id, "drive", "Drive handoff complete; local deliverable removed")
        _upload_profile_docx(job_id, job_dir)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["drive"] = {"state": ("pending_authorization" if isinstance(e, storage.DriveAuthorizationRequired)
                                           else "error"), "files": files, "error": str(e)}
        _append_job_log(job_id, "drive", f"Drive upload failed; local file kept ({e})")
    finally:
        _persist_queue()


def _submission_outcome_uncertain(error: BaseException) -> bool:
    """Do not replay a billable submission, even through an error wrapper."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (llm.LLMSubmissionUncertain,
                              clients.KieSubmissionUncertain, ai33_tasks.SubmissionUncertain,
                              ai33_tasks.UsageLimitReached)):
            return True
        error = error.__cause__ or error.__context__
    return False


def _run_job(job_id: str, inputs: dict, profile: str, *,
             profile_data: dict | None = None,
             fleet_reporter: fleet.FleetReporter | None = None) -> None:
    try:
        return _run_job_impl(job_id, inputs, profile, profile_data=profile_data,
                             fleet_reporter=fleet_reporter)
    finally:
        metrics = telemetry.current()
        if metrics:
            try:
                telemetry.save(config.OUTPUT_DIR / job_id / "performance.json")
                with LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["performance"] = metrics.snapshot()
                _persist_queue()
            except OSError:
                pass
        cancel.set_current(None)


def _run_job_impl(job_id: str, inputs: dict, profile: str, *,
                  profile_data=None, fleet_reporter=None) -> None:
    job_dir = config.OUTPUT_DIR / job_id

    # Ensure status is running (kick_queue usually set this already), and
    # publish a fresh cancel token so Stop/preempt can reach this job's
    # ffmpeg children. Underscore-prefixed so _persist_queue never tries to
    # serialize it.
    token = cancel.CancelToken(job_id)
    token.profile = profile
    token.fleet_reporter = fleet_reporter
    token.metrics = telemetry.Metrics(job_id=job_id, profile=profile,
                                      worker=config.FLEET_WORKER_ID or "main",
                                      revision=fleet.application_revision())
    cancel.set_current(token)
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["_cancel"] = token
            JOBS[job_id]["export_validation_required"] = True
            if JOBS[job_id].get("_stop_target"):
                token.cancel()

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
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
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
        # Stop does not publish an approval. Wake periodically to observe
        # its token instead of leaving the worker parked here forever.
        while not event.wait(timeout=0.5):
            token.check()
        token.check()  # Stop may race with the approval notification.
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
        token.metrics.context.update(chars=meta["chars"], beats=meta["est_beats"])
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

    def _finish_stopped() -> None:
        # Deliberately stopped (Stop button, or preempted by a manual
        # retry) — NOT a failure. Everything generated so far stays on disk,
        # so resuming re-uses it instead of re-billing the provider APIs.
        with LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                # "paused"  — a deliberate Stop: waits for the user, and is
                #             NOT auto-started, or it would restart in the
                #             same second the slot it just freed reopens.
                # "queued"  — preempted to make room for a manual retry:
                #             should come back on its own.
                target = job.pop("_stop_target", "paused")
                job["status"] = target
                job["step"] = target
                job.pop("_cancel", None)
                job.pop("awaiting", None)
                job.pop("_gate_event", None)
                job.pop("approval", None)
                job["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                    "message": {
                        "paused": ("Stopped. Work so far is kept — Resume "
                                   "continues from here instead of restarting"),
                        "queued": ("Paused to free a slot — resumes next, "
                                   "continuing from what's on disk"),
                    }.get(target, "Stopped and cancelled")})
        _persist_queue()
        _jm_call("manual_log", "STOPPED by user")
        _jm_call("manual_finish", 1)
        if fleet_reporter:
            fleet_reporter.fail("stopped by user")

    try:
        token.check()
        out = orchestrator.run_pipeline(inputs, profile, job_dir, progress,
                                        gate, voice_gate, beat_progress,
                                        on_planned, profile_data)
        token.check()
        token.metrics.context.update(output_duration_seconds=video_render.probe_duration(out),
                                     encoder=config.gpu_encoder() or "libx264")
        if not video_render.is_valid_video(out):
            raise RuntimeError(
                f"render did not produce a valid playable video: {out.name}")
        stats.record_performance(profile, job_id, token.metrics.snapshot(), datetime.now().isoformat(timespec="seconds"))
        with LOCK:
            JOBS[job_id]["status"] = "uploading"
            JOBS[job_id]["step"] = "drive"
            JOBS[job_id]["result"] = str(out)
            JOBS[job_id]["drive"] = {"state": "uploading", "files": {}}
        _persist_queue()
        if fleet_reporter:
            fleet_reporter.uploading({"state": "uploading", "files": {}})
        _kick_queue()  # upload has its own bounded capacity; release compute now
        with telemetry.span("upload"):
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
                fleet_reporter.complete(dict(JOBS.get(job_id, {}).get("drive") or {}),
                                        token.metrics.snapshot())
    except cancel.JobCancelled:
        _finish_stopped()
    except Exception as e:  # noqa: BLE001
        # Auto-retry a genuine failure a couple of times before giving up —
        # most failures here are provider outages, and the on-disk caches
        # mean a retry resumes rather than restarting from scratch. The
        # delay matters: an instant retry into a provider that is down just
        # burns an attempt.
        limit = int(config.queue_setting("job_retry_limit", profile) or 0)
        retry_delay = max(0.0, float(config.queue_setting("retry_delay_seconds", profile) or 60))
        uncertain = _submission_outcome_uncertain(e)
        with LOCK:
            job = JOBS.get(job_id)
            attempts = int((job or {}).get("retries", 0))
            # Stop may arrive while an HTTP call is in flight. The provider
            # can then raise an ordinary timeout instead of JobCancelled.
            # Decide under the same lock as Stop's _stop_target update so
            # an acknowledged Stop never becomes an automatic retry.
            stopped = token.cancelled or bool((job or {}).get("_stop_target"))
            will_retry = (job is not None and not stopped and not uncertain
                          and attempts < limit)
            if job is not None and not stopped:
                job.pop("_cancel", None)
                if will_retry:
                    job["retries"] = attempts + 1
                    job["status"] = "retry_wait"
                    job["step"] = "retry_wait"
                    job["error"] = str(e)
                    job["retry_at"] = time.time() + retry_delay
                    job["retry_limit"] = limit
                    if fleet_reporter:
                        fleet_reporter.retry_wait(str(e), attempts + 1, limit, job["retry_at"])
                    job["log"].append({
                        "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                        "message": (f"Failed ({str(e)[:120]}) — auto-retry "
                                    f"{attempts + 1}/{limit} shortly")})
                else:
                    job["status"] = "error"
                    job["step"] = "error"
                    job["error"] = str(e)
                    job["log"].append({
                        "t": datetime.now().strftime("%H:%M:%S"),
                        "step": "error", "message": (
                            f"{e} — automatic retry disabled: the provider may "
                            "already have accepted this paid request; review "
                            "its result before retrying manually"
                            if uncertain else str(e))})
        if stopped:
            _finish_stopped()
            return
        traceback.print_exc()
        print(f"[job {job_id}] ✗ pipeline failed: {e}")
        _persist_queue()
        _jm_call("manual_log", f"ERROR: {e}")
        _jm_call("manual_finish", 1)
        if fleet_reporter and not will_retry:
            fleet_reporter.fail(str(e))
        if will_retry and not fleet_reporter:
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
        profile_broll_defaults=_profile_broll_defaults(),
        owner_access=not ACCESS or getattr(g, "user", {}).get("profiles") is None,
    )


def _statistics(days=30, profile=None, allowed_profiles=None):
    with LOCK:
        rows = [{"job_id": jid, "profile": j.get("profile"), "status": j.get("status"),
                 "created_at": j.get("created_at"), "performance": j.get("performance")}
                for jid, j in JOBS.items()]
    return analytics.snapshot(rows, days=days, profile=profile, allowed_profiles=allowed_profiles)


@app.route("/stats")
def statistics_page():
    return render_template("stats.html", owner_access=not ACCESS or getattr(g, "user", {}).get("profiles") is None)


def _cluster_job_rows() -> list[dict]:
    with LOCK:
        rows = []
        for jid, job in JOBS.items():
            log = job.get("worker_events") or job.get("log") or []
            rows.append(node_monitor.sanitize({
                "job_id": jid,
                "title": job.get("publication_title") or job.get("title") or jid,
                "profile": job.get("profile") or "",
                "worker_id": job.get("dispatched_to") or "main",
                "status": job.get("status") or "unknown",
                "step": job.get("step") or "",
                "progress_pct": job.get("progress_pct") or 0,
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at") or job.get("created_at"),
                "error": str(job.get("error") or "")[-1500:],
                "recent_events": [
                    {key: event.get(key) for key in ("at", "t", "step", "message")}
                    for event in log[-8:] if isinstance(event, dict)
                ],
            }))
    return rows


def _cluster_snapshot() -> dict:
    """One central, secret-free view of the main and every known worker."""
    jobs_rows = _cluster_job_rows()
    worker_rows = _fleet_worker_rows()
    current_ids = {row["worker_id"] for row in worker_rows}
    for previous in _cluster_history().known_workers():
        wid = str(previous.get("worker_id") or "")
        if not wid or wid in current_ids:
            continue
        capacity = dict(previous.get("capacity") or {})
        capacity.update(free=0, effective_free=0, pending=0)
        worker_rows.append({**previous, "online": False, "stale": True,
                            "health": "offline", "age_seconds": None,
                            "capacity": capacity, "jobs": []})
    for worker in worker_rows:
        worker["role"] = "worker"
        worker["agent"] = {"name": "Fleet reporter",
                           "state": "reporting" if worker.get("online") else "disconnected",
                           "mode": "lightweight"}
        worker["jobs"] = [row for row in jobs_rows
                          if row.get("worker_id") == worker.get("worker_id")
                          and row.get("status") not in ("done", "cancelled")]

    main_node = _node_monitor().snapshot()
    main_jobs = [row for row in jobs_rows if row.get("worker_id") == "main"
                 and row.get("status") not in ("done", "cancelled")]
    local_active = sum(1 for row in main_jobs if row.get("status") in _SLOT_STATUSES)
    local_max = config.max_concurrent_jobs()
    ops_state = OPS.status() if OPS is not None else {
        "name": "SalCube", "running": False, "interval_seconds": 300,
        "model": config.OPENROUTER_MONITOR_MODEL,
    }
    main = {
        "worker_id": "main", "name": "Main computer", "role": "main",
        "online": True, "stale": False, "health": "available" if local_active < local_max else "busy",
        "last_seen": datetime.now().isoformat(timespec="seconds"), "age_seconds": 0,
        "hardware": {"cpu_cores": config._detect_cpu_count(),
                     "ram_gb": round(config._detect_ram_gb(), 1),
                     "hw_encoder": config.gpu_encoder() or "libx264",
                     "platform": platform.system(), "platform_release": platform.release(),
                     "app_revision": fleet.application_revision()},
        "metrics": {"disk": operations.disk_snapshot(), "render": resources.snapshot()},
        "capacity": {"max_jobs": local_max, "effective_max_jobs": local_max,
                     "active": local_active, "free": max(0, local_max - local_active),
                     "effective_free": max(0, local_max - local_active), "pending": 0},
        "policy": {"mode": "active", "max_jobs": local_max},
        "node": main_node, "agent": {"name": "SalCube",
                     "state": "running" if ops_state.get("running") else "starting",
                     "mode": "central", "model": ops_state.get("model")},
        "jobs": main_jobs,
    }
    nodes = [main, *sorted(worker_rows, key=lambda row: (
        not row.get("online"), str(row.get("name") or "").lower()))]
    history = _cluster_history().observe(nodes, jobs_rows)
    active_statuses = _ACTIVE - {"awaiting_approval"}
    update_state = _update_status()
    return node_monitor.sanitize({
        "name": "SouthCube", "captured_at": datetime.now().isoformat(timespec="seconds"),
        "observer": {key: ops_state.get(key) for key in (
            "name", "running", "interval_seconds", "model")},
        "update": update_state,
        "summary": {"total": len(nodes), "online": sum(bool(n.get("online")) for n in nodes),
                    "active_jobs": sum(j.get("status") in active_statuses for j in jobs_rows),
                    "free_slots": sum(int((n.get("capacity") or {}).get("effective_free") or 0)
                                      for n in nodes if n.get("online"))},
        "nodes": nodes, **history,
        "notes": [
            "Worker telemetry is metadata only; commands, environment variables, credentials and file contents are never reported.",
            "Heartbeat loss means stopped or unreachable. A crash is only confirmed when job or process evidence says so.",
            "Detailed CPU, RAM, GPU, process and file-change data appears after each worker installs this version and restarts.",
        ],
    })


@app.route("/cluster")
def cluster_page():
    return render_template("cluster.html", owner_access=True)


@app.route("/api/cluster")
def cluster_api():
    response = jsonify(_cluster_snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/update/status")
def update_status_api():
    response = jsonify(_update_status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/stats")
def statistics_api():
    try:
        days = int(request.args.get("days", "30"))
        if not 1 <= days <= 365:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "days must be between 1 and 365"}), 400
    profile = request.args.get("profile") or None
    if profile and not _allowed_profile(profile):
        abort(403)
    allowed = getattr(g, "user", {}).get("profiles") if ACCESS else None
    response = jsonify(_statistics(days, profile, allowed))
    response.headers["Cache-Control"] = "no-store"
    return response


def _build_inputs(data: dict, title: str) -> dict:
    """Shared input packing for /api/render (one title → one job)."""
    broll = [k for k in ("stock", "stock_image", "ai_video", "animation")
             if data.get(f"broll_{k}")]
    anim_templates = data.get("anim_templates")
    video_group = (data.get("video_group") or "").strip().lower()
    if video_group == "small":
        length_chars = 32000 + secrets.randbelow(47000 - 32000 + 1)
    elif video_group == "big":
        length_chars = 170000 + secrets.randbelow(195000 - 170000 + 1)
    else:
        video_group = "custom"
        length_chars = data.get("length_chars")
    spotify_popup_style = str(
        data.get("spotify_popup_style") or "standard").strip().lower()
    if spotify_popup_style not in ("standard", "geistmeister"):
        spotify_popup_style = "standard"
    return {
        "title": title,
        "language": (data.get("language") or "").strip(),
        "length_chars": length_chars,
        "video_group": video_group,
        "comment_cta": bool(data.get("comment_cta", True)),
        "rating_cta": bool(data.get("rating_cta", False)),
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
        "script_model": (data.get("script_model")
                         or config.DEFAULT_SCRIPT_MODEL).strip(),
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
        "spotify_popup_style": spotify_popup_style,
        "spotify_url": (data.get("spotify_url") or "").strip(),
        "hyperframes_style": (data.get("hyperframes_style") or "").strip(),
        "hyperframes_pace": (data.get("hyperframes_pace") or "").strip(),
        # Operational retry hint, set by the controller rather than exposed
        # as a normal creative parameter. Persisted so restarts keep the job
        # on the machine that owns its cached assets.
        "local_only": bool(data.get("local_only", False)),
    }


def _enqueue_job(inputs: dict, profile: str, *, resume: str = "") -> str:
    """Create a job dir + registry entry with status=queued. Does not start it."""
    with LOCK:
        taken = tuple(JOBS)
    job_id = resume or identity.numeric_id(
        taken=taken, directories=[config.OUTPUT_DIR])
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

    # Known at enqueue time, before any LLM call: the user's requested
    # target length (or that profile's own default if left blank) — same
    # fallback run_pipeline() itself applies. Lets the queue show a size
    # badge/ETA immediately; replaced with the real narration length once
    # the script stage actually finishes (see on_planned in _run_job).
    try:
        profile_data = orchestrator.load_profile(profile)
        defaults = profile_data.get("defaults", {})
    except Exception:  # noqa: BLE001
        profile_data = {}
        defaults = {}
    # The UI language field is an override. When it is blank, persist the
    # profile language so every parallel subsystem (script, title, thumbnail,
    # metadata) sees the same explicit value instead of independently guessing.
    inputs["language"] = ((inputs.get("language") or "").strip()
                          or str(defaults.get("language") or "English").strip())
    length_min = int(defaults.get("length_chars_min") or 0)
    length_max = int(defaults.get("length_chars_max") or 0)
    if not resume and length_min > 0 and length_max >= length_min:
        inputs["length_chars"] = length_min + secrets.randbelow(
            length_max - length_min + 1)
        inputs["video_group"] = "profile"
    inputs_file.write_text(json.dumps({**inputs, "_profile": profile}, indent=1))
    est_chars = int(inputs.get("length_chars") or defaults.get("length_chars", 15000))
    needs_research = bool((profile_data.get("research") or {}).get("enabled"))
    uses_thumbnail = _profile_uses_thumbnail(profile)

    created_at = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        existing_thumb = (JOBS.get(job_id) or {}).get("thumbnail") if resume else None
        JOBS[job_id] = {
            "status": "researching" if needs_research else "queued",
            "step": "research" if needs_research else "queued",
            "log": [{"t": datetime.now().strftime("%H:%M:%S"),
                     "step": "research" if needs_research else "queue",
                     "message": ("Terra is researching the theme and preparing three titles"
                                 if needs_research else "Added to queue")}],
            "title": title,
            "publication_title": None if needs_research else title,
            "profile": profile,
            "inputs": inputs,
            "created_at": created_at,
            "updated_at": created_at,
            "est_chars": est_chars,
            "chars": None,
            "est_beats": None,
            "video_group": inputs.get("video_group"),
            "local_only": bool(inputs.get("local_only")),
            "research_attempt": 1 if needs_research else 0,
            "title_options": ({"state": "generating", "theme": title}
                              if needs_research else {}),
            "package": ({"state": "waiting_for_video"}
                        if _profile_wants_docx(profile)
                        else {}),
            "thumbnail": (existing_thumb if isinstance(existing_thumb, dict)
                          else ({"state": "disabled"}
                                if not uses_thumbnail else
                                ({"state": "waiting_for_title"}
                                 if needs_research else
                                 {"state": "generating_words"}))),
            # Deliberately inactive until the owner supplies channel rules.
            "description": {"state": "waiting_for_rules"},
        }
    _persist_queue()
    if needs_research:
        _start_profile_research(job_id)
    elif uses_thumbnail and not resume:
        _start_thumbnail_words(job_id)
    return job_id


def _profile_research_task(job_id: str, attempt: int) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if (not job or job.get("status") != "researching"
                or int(job.get("research_attempt") or 0) != attempt):
            return
        profile_name = job["profile"]
        theme = str((job.get("title_options") or {}).get("theme")
                    or job.get("title") or "").strip()
    try:
        package = research.build_package(profile_name, theme)
        candidates = [str(x).strip() for x in package.get("titles") or []]
        if len(candidates) != 3 or len({x.casefold() for x in candidates}) != 3:
            raise RuntimeError("Terra research did not return exactly three distinct titles")
        job_dir = config.OUTPUT_DIR / job_id
        with LOCK:
            current = JOBS.get(job_id)
            if (not current or current.get("status") != "researching"
                    or int(current.get("research_attempt") or 0) != attempt):
                return
        research_file = job_dir / "research.json"
        research_file.write_text(json.dumps({"theme": theme, **package}, indent=1))
        with LOCK:
            job = JOBS.get(job_id)
            if (not job or job.get("status") != "researching"
                    or int(job.get("research_attempt") or 0) != attempt):
                return
            job_inputs = job.setdefault("inputs", {})
            job_inputs.update({
                "theme": theme,
                "research_brief": str(package.get("research_brief") or "").strip(),
                "source_material": str(package.get("source_material") or "").strip(),
                "source_metadata": package.get("source_metadata") or {},
            })
            (job_dir / "inputs.json").write_text(json.dumps(
                {**job_inputs, "_profile": profile_name}, indent=1))
            job["title_options"] = {
                "state": "ready", "theme": theme, "candidates": candidates,
            }
            job["status"] = "awaiting_title"
            job["step"] = "title"
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            job.setdefault("log", []).append({
                "t": datetime.now().strftime("%H:%M:%S"), "step": "title",
                "message": "Research ready — choose one of the three titles",
            })
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with LOCK:
            job = JOBS.get(job_id)
            if (job and job.get("status") == "researching"
                    and int(job.get("research_attempt") or 0) == attempt):
                job["status"] = "error"
                job["step"] = "research"
                job["error"] = str(exc)
                job["title_options"] = {
                    "state": "error", "theme": theme, "error": str(exc),
                }
    _persist_queue()


def _start_profile_research(job_id: str) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "researching":
            return
        attempt = int(job.get("research_attempt") or 0)
    threading.Thread(target=_profile_research_task, args=(job_id, attempt), daemon=True,
                     name=f"dmiqwi-research-{job_id}").start()


@app.route("/api/title/job/<job_id>/approve", methods=["POST"])
def approve_researched_title(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    data = request.get_json(force=True) or {}
    chosen = str(data.get("title") or "").strip()
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        options = dict(job.get("title_options") or {})
        candidates = [str(x).strip() for x in options.get("candidates") or []]
        if job.get("status") != "awaiting_title" or options.get("state") != "ready":
            return jsonify({"error": "job is not awaiting a title choice"}), 409
        if chosen not in candidates:
            return jsonify({"error": "choose one of the three proposed titles"}), 400
        profile_name = job["profile"]
    naming_error = _title_naming_error(
        profile_name, chosen, exclude_job_id=job_id)
    if naming_error:
        return jsonify({"error": naming_error}), 409
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        options = dict(job.get("title_options") or {})
        candidates = [str(x).strip() for x in options.get("candidates") or []]
        if (job.get("status") != "awaiting_title"
                or options.get("state") != "ready" or chosen not in candidates):
            return jsonify({"error": "title choices changed; select again"}), 409
        job["title"] = chosen
        job["publication_title"] = chosen
        job_inputs = job.setdefault("inputs", {})
        job_inputs["title"] = chosen
        options.update(state="approved", selected=chosen,
                       approved_at=datetime.now().isoformat(timespec="seconds"))
        job["title_options"] = options
        uses_thumbnail = _profile_uses_thumbnail(profile_name)
        job["thumbnail"] = ({"state": "generating_words"}
                            if uses_thumbnail else {"state": "disabled"})
        job["status"] = "queued"
        job["step"] = "queued"
        job["error"] = None
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        job.setdefault("log", []).append({
            "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
            "message": "Title approved — video moved into the production queue",
        })
        inputs_copy = dict(job_inputs)
    (config.OUTPUT_DIR / job_id / "inputs.json").write_text(json.dumps(
        {**inputs_copy, "_profile": profile_name}, indent=1))
    _persist_queue()
    if uses_thumbnail:
        _start_thumbnail_words(job_id)
    _kick_queue()
    return jsonify({"ok": True, "job_id": job_id, "title": chosen})


@app.route("/api/title/job/<job_id>/retry", methods=["POST"])
def retry_profile_research(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        try:
            enabled = bool((orchestrator.load_profile(job["profile"]).get(
                "research") or {}).get("enabled"))
        except Exception:  # noqa: BLE001
            enabled = False
        if not enabled:
            return jsonify({"error": "this profile has no research gate"}), 409
        theme = str((job.get("title_options") or {}).get("theme")
                    or (job.get("inputs") or {}).get("theme")
                    or job.get("title") or "").strip()
        job["status"] = "researching"
        job["step"] = "research"
        job["error"] = None
        job["research_attempt"] = int(job.get("research_attempt") or 0) + 1
        job["title_options"] = {"state": "generating", "theme": theme}
    _persist_queue()
    _start_profile_research(job_id)
    return jsonify({"ok": True})


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

    selected_profile = (_job_profile(resume) or profile) if resume else profile
    try:
        profile_data = orchestrator.load_profile(selected_profile)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"profile could not be loaded: {exc}"}), 400
    try:
        if hyperframes.is_hyperframes_profile(profile_data):
            hyperframes.validate_profile(profile_data)
            hyperframes.ensure_runtime()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"profile preflight failed: {exc}"}), 503

    if not resume:
        needs_research = bool((profile_data.get("research") or {}).get("enabled"))
        if not needs_research:
            batch_names = [_delivery_title_stem(title).casefold() for title in titles]
            if len(set(batch_names)) != len(batch_names):
                return jsonify({
                    "error": "naming error: the batch contains the same video title twice"
                }), 409
            for title in titles:
                naming_error = _title_naming_error(profile, title)
                if naming_error:
                    return jsonify({"error": naming_error}), 409

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
            needs_slot = job is not None and job.get("status") == "queued"
            retry_is_local_only = bool(job and job.get("local_only"))
            if needs_slot:
                # "0:" ranks retries above preempted jobs ("1:"), which in
                # turn rank above ordinary FIFO work.
                job["priority_at"] = "0:" + datetime.now().isoformat(timespec="seconds")
                job["retries"] = 0  # manual attempt resets the auto-retry budget
            free_now = config.max_concurrent_jobs() - _active_slot_count()
        # Only interrupt local work as a last resort: if a fleet worker has
        # a free slot it can take this job without anything being stopped.
        remote_free = 0
        with _FLEET_LOCK:
            for w in FLEET_WORKERS.values():
                try:
                    age = (datetime.now()
                           - datetime.fromisoformat(w["last_seen"])).total_seconds()
                except Exception:  # noqa: BLE001
                    continue
                if age <= config.FLEET_STALE_SECONDS:
                    remote_free += _fleet_capacity(w)["effective_free"]
        # _enqueue_job() drains the queue before returning. If this retry is
        # already running, it has its slot and must not preempt a different
        # running retry merely because capacity is now full.
        if (needs_slot and free_now <= 0
                and (retry_is_local_only or remote_free <= 0)
                and not config.queue_setting("queue_paused")):
            _preempt_for(job_ids[0])

    _kick_queue()
    with LOCK:
        created = [{"video_id": jid,
                    "length_chars": JOBS[jid].get("est_chars"),
                    "video_group": JOBS[jid].get("video_group")}
                   for jid in job_ids]
    return jsonify({
        "job_id": job_ids[0],
        "job_ids": job_ids,
        "queued": len(job_ids),
        "jobs": created,
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
    if not isinstance(data, dict):
        return jsonify({"error": "heartbeat must be an object"}), 400
    worker_id = str(data.get("worker_id") or "").strip()
    if not worker_id or len(worker_id) > 120:
        return jsonify({"error": "valid worker_id is required"}), 400
    capacity = data.get("capacity") if isinstance(data.get("capacity"), dict) else {}
    try:
        free = max(0, min(8, int(capacity.get("free") or 0)))
    except (TypeError, ValueError):
        free = 0
    reported = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    reported = node_monitor.sanitize(reported)
    if not isinstance(reported, list):
        reported = []
    try:
        protocol_version = max(1, min(99, int(data.get("protocol_version") or 1)))
    except (TypeError, ValueError):
        protocol_version = 1
    now = datetime.now()
    with _FLEET_LOCK:
        worker = FLEET_WORKERS.setdefault(worker_id, {"approvals": [], "dispatch": {}})
        worker.update({"worker_id": worker_id, "name": str(data.get("name") or worker_id),
                       "protocol_version": protocol_version,
                       "hardware": data.get("hardware") if isinstance(data.get("hardware"), dict) else {},
                       "capacity": capacity,
                       "metrics": data.get("metrics") if isinstance(data.get("metrics"), dict) else {},
                       "node": _safe_node_report(data.get("node")),
                       "cached_jobs": [str(jid) for jid in (data.get("cached_jobs") or [])
                                       if str(jid).isdigit()][:200],
                       "jobs": reported,
                       "last_seen": now.isoformat(timespec="seconds")})
        approvals = list(worker.get("approvals") or [])
        worker["approvals"] = []
    acknowledged_jobs: list[str] = []
    with LOCK:
        for report in reported:
            if not isinstance(report, dict):
                continue
            job_id = str(report.get("job_id") or "")
            if report.get("status") in ("done", "error") and job_id:
                acknowledged_jobs.append(job_id)
            job = JOBS.get(job_id)
            if not job or job.get("dispatched_to") != worker_id:
                continue
            if job.get("attempt_id") and report.get("attempt_id") != job["attempt_id"]:
                continue
            if job.get("status") in ("done", "error") and report.get("status") in ("running", "awaiting_approval"):
                continue
            state = str(report.get("status") or "running")
            if state not in ("running", "awaiting_approval", "retry_wait", "uploading", "done", "error"):
                continue
            # A fleet worker reports a deliberate stop through its terminal
            # error channel ("stopped by user"). Preserve the owner's
            # requested destination instead of turning that acknowledgement
            # back into a failed/running job on later heartbeats.
            stop_target = str(job.get("_stop_target") or "")
            if stop_target in ("paused", "cancelled") and state in ("done", "error"):
                job["status"] = stop_target
                job["step"] = stop_target
                job.pop("_stop_target", None)
                job.pop("error", None)
                job.pop("awaiting", None)
                job.pop("dispatched_to", None)
                job.pop("dispatched_at", None)
                job.setdefault("log", []).append({
                    "t": now.strftime("%H:%M:%S"), "step": "queue",
                    "message": ("Worker stopped; cached work is ready to resume"
                                if stop_target == "paused"
                                else "Worker stopped and job removed"),
                })
                job["updated_at"] = now.isoformat(timespec="seconds")
                continue
            job["status"] = state
            job["step"] = str(report.get("step") or state)
            job["updated_at"] = now.isoformat(timespec="seconds")
            job["progress_pct"] = max(job.get("progress_pct", 0), int(report.get("progress_pct") or 0))
            # A fleet job may recover after an earlier provider failure and
            # continue from its cached assets.  Do not keep presenting that
            # old failure (or feeding it to the monitor) once the worker has
            # reported a healthy state again.
            if state not in ("error", "retry_wait"):
                job.pop("error", None)
                job.pop("retry_at", None)
            if state == "retry_wait":
                job["error"] = str(report.get("error") or "remote worker will retry")
                for field in ("retries", "retry_limit", "retry_at"):
                    if isinstance(report.get(field), (int, float)):
                        job[field] = report[field]
            if state == "awaiting_approval" and isinstance(report.get("awaiting"), dict):
                job["awaiting"] = report["awaiting"]
            if isinstance(report.get("drive"), dict):
                job["drive"] = report["drive"]
            if isinstance(report.get("performance"), dict):
                job["performance"] = report["performance"]
            events = report.get("recent_events")
            if isinstance(events, list):
                job["worker_events"] = node_monitor.sanitize([
                    {k: event.get(k) for k in ("at", "step", "message")}
                    for event in events[-12:] if isinstance(event, dict)])
            if state == "error":
                job["error"] = str(report.get("error") or "remote worker failed")
    # A job appearing in this heartbeat acknowledges receipt of any previous
    # dispatch. Until then we resend it; the worker ignores duplicate IDs.
    with _FLEET_LOCK:
        worker = FLEET_WORKERS[worker_id]
        for report in reported:
            if isinstance(report, dict):
                jid = str(report.get("job_id") or "")
                pending = worker.get("dispatch", {}).get(jid, {})
                if not pending.get("attempt_id") or pending.get("attempt_id") == report.get("attempt_id"):
                    worker.get("dispatch", {}).pop(jid, None)
    # Do not hold _FLEET_LOCK while claiming queue work: approval requests
    # acquire LOCK then _FLEET_LOCK, so reversing that order would deadlock.
        allowed_free = _fleet_capacity(worker)["effective_free"]
    new_dispatch = _fleet_dispatch_for(worker_id, min(free, allowed_free))
    with _FLEET_LOCK:
        worker = FLEET_WORKERS[worker_id]
        for spec in new_dispatch:
            worker.setdefault("dispatch", {})[spec["job_id"]] = spec
        dispatch = list(worker.get("dispatch", {}).values())
        # Abort requests are drained here: the worker acts on them and the
        # resulting state change comes back as an ordinary status report.
        aborts = list(worker.get("aborts") or [])
        worker["aborts"] = []
        policy = _fleet_policy(worker_id)
    _persist_queue()
    return jsonify({"dispatch": dispatch, "approvals": approvals,
                    "aborts": aborts, "policy": policy,
                    "acknowledged_attempts": [{"job_id": str(r.get("job_id") or ""),
                                               "attempt_id": r.get("attempt_id")}
                                              for r in reported if isinstance(r, dict)
                                              and r.get("status") in ("done", "error")],
                    "acknowledged_jobs": sorted(set(acknowledged_jobs))})


@app.route("/api/workers")
def fleet_workers():
    return jsonify(_fleet_worker_rows())


@app.route("/api/workers/<worker_id>/policy", methods=["POST"])
def set_fleet_worker_policy(worker_id: str):
    """Change lifecycle/capacity without killing active render processes."""
    if config.FLEET_ROLE != "main":
        return jsonify({"error": "this machine is not configured as fleet main"}), 409
    with _FLEET_LOCK:
        if worker_id not in FLEET_WORKERS:
            return jsonify({"error": "unknown worker"}), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not ({"mode", "max_jobs"} & set(data)):
        return jsonify({"error": "send mode and/or max_jobs"}), 400
    patch: dict = {}
    if "mode" in data:
        mode = str(data.get("mode") or "")
        if mode not in _FLEET_MODES:
            return jsonify({"error": "mode must be active, draining, or paused"}), 400
        patch["mode"] = mode
    if "max_jobs" in data:
        if data.get("max_jobs") is None or data.get("max_jobs") == "":
            patch["max_jobs"] = None
        else:
            try:
                patch["max_jobs"] = max(1, min(8, int(data["max_jobs"])))
            except (TypeError, ValueError):
                return jsonify({"error": "max_jobs must be 1-8 or null"}), 400

    settings = config.save_settings({"fleet_workers": {worker_id: patch}})
    policy = _fleet_policy(worker_id, settings)
    released: list[str] = []
    # Enforce the new policy against deliveries the worker has not accepted
    # yet. Active reports are never interrupted by a policy change.
    with LOCK:
        with _FLEET_LOCK:
            worker = FLEET_WORKERS.get(worker_id)
            if worker is None:
                return jsonify({"error": "worker disconnected"}), 409
            capacity = _fleet_capacity(worker, policy)
            pending_ids = list((worker.get("dispatch") or {}).keys())
            allowed_pending = (max(0, capacity["effective_max_jobs"] - capacity["active"])
                               if policy["mode"] == "active" else 0)
            for jid in pending_ids[allowed_pending:]:
                job = JOBS.get(jid)
                if (job and job.get("status") == "dispatched"
                        and job.get("dispatched_to") == worker_id):
                    job.update(status="queued", step="queued")
                    job.pop("dispatched_to", None)
                    job.pop("dispatched_at", None)
                    job.setdefault("log", []).append({
                        "t": datetime.now().strftime("%H:%M:%S"),
                        "step": "fleet",
                        "message": f"Released before start because {worker_id} policy changed",
                    })
                    released.append(jid)
                worker.get("dispatch", {}).pop(jid, None)
    _persist_queue()
    if released:
        _kick_queue()
    row = next((r for r in _fleet_worker_rows() if r["worker_id"] == worker_id), None)
    return jsonify({"ok": True, "worker": row, "released_jobs": released})


@app.route("/api/dispatch/<job_id>", methods=["POST"])
def dispatch_to_worker(job_id: str):
    """Manually send one queued job to an available worker immediately."""
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    if config.FLEET_ROLE != "main":
        return jsonify({"error": "this machine is not configured as fleet main"}), 409
    if UPDATE_LOCK_FILE.exists():
        return jsonify({"error": "SouthCube is preparing an automatic update; the job remains queued"}), 409
    requested = str((request.get_json(silent=True) or {}).get("worker_id") or "").strip()
    now = datetime.now().timestamp()
    settings = config.load_settings()
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
        spec = {"job_id": job_id, "inputs": job["inputs"],
                "profile": job["profile"], "profile_data": profile_data}
        # Choose and reserve while holding the fleet lock, so two simultaneous
        # manual sends cannot both consume the same last free slot.
        with _FLEET_LOCK:
            choices: list[tuple[int, str, dict]] = []
            for worker in FLEET_WORKERS.values():
                wid = str(worker.get("worker_id") or "")
                try:
                    age = now - datetime.fromisoformat(worker.get("last_seen", "")).timestamp()
                except Exception:  # noqa: BLE001
                    continue
                capacity = _fleet_capacity(worker, _fleet_policy(wid, settings))
                if (age <= config.FLEET_STALE_SECONDS and capacity["effective_free"] > 0
                        and (not requested or requested == wid)):
                    hardware = worker.get("hardware") or {}
                    render_metrics = (worker.get("metrics") or {}).get("render") or {}
                    score = (capacity["effective_free"] * 10 +
                             (20 if hardware.get("hw_encoder") else 0) +
                             min(16, int(hardware.get("cpu_cores") or 1)) -
                             int(render_metrics.get("waiting_encoders") or 0) * 5)
                    choices.append((score, wid, worker))
            if not choices:
                return jsonify({"error": "selected worker is unavailable, paused, or full"}), 409
            choices.sort(key=lambda item: (-item[0], item[1]))
            _, worker_id, worker = choices[0]
            job.update(status="dispatched", step="fleet", dispatched_to=worker_id,
                       dispatched_at=datetime.now().isoformat(timespec="seconds"),
                       local_only=False)
            job["attempt_id"] = secrets.token_hex(12) if int(worker.get("protocol_version", 1)) >= 3 else None
            spec["attempt_id"] = job.get("attempt_id")
            worker.setdefault("dispatch", {})[job_id] = spec
    _persist_queue()
    return jsonify({"ok": True, "worker_id": worker_id})


@app.route("/api/dispatch/<job_id>/release", methods=["POST"])
def release_worker_dispatch(job_id: str):
    """Return an unacknowledged dispatch to the queue without duplication."""
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        worker_id = str(job.get("dispatched_to") or "")
        if job.get("status") != "dispatched" or not worker_id:
            return jsonify({"error": "job is no longer waiting for a worker"}), 409
        with _FLEET_LOCK:
            worker = FLEET_WORKERS.get(worker_id)
            if not worker or job_id not in (worker.get("dispatch") or {}):
                return jsonify({
                    "error": "worker already accepted this job; use Stop to halt it safely"
                }), 409
            worker["dispatch"].pop(job_id, None)
        job.update(status="queued", step="queued")
        job.pop("dispatched_to", None)
        job.pop("dispatched_at", None)
        job.setdefault("log", []).append({
            "t": datetime.now().strftime("%H:%M:%S"), "step": "fleet",
            "message": f"Released from {worker_id} before it started",
        })
    _persist_queue()
    _kick_queue()
    return jsonify({"ok": True, "job_id": job_id, "released_from": worker_id})


@app.route("/api/performance/<job_id>")
def job_performance(job_id):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        metrics = getattr(job.get("_cancel"), "metrics", None)
        result = metrics.snapshot() if metrics else job.get("performance")
    return jsonify(result or artifacts.read(config.OUTPUT_DIR / job_id / "performance.json"))


@app.route("/api/usage/ai33")
def ai33_usage():
    rows = [r for r in ai33_tasks.usage() if _allowed_profile(r.get("profile", ""))]
    totals = {}
    for row in rows:
        key = (str(row.get("at", ""))[:10], row.get("profile", ""), row.get("account", "unknown"))
        total = totals.setdefault(key, {"date": key[0], "profile": key[1], "account": key[2],
                                       "submitted_characters": 0, "pending_characters": 0, "tasks": 0})
        if row.get("event") in ("submitted", "succeeded"):
            total["submitted_characters"] += int(row.get("characters") or 0)
            total["tasks"] += 1
        elif row.get("event") == "reserved":
            total["pending_characters"] += int(row.get("characters") or 0)
    return jsonify({"timezone": "UTC", "totals": sorted(totals.values(), key=lambda r: r["date"], reverse=True),
                    "note": "Characters are submitted request amounts, not invoiced credits. Pending amounts include uncertain acceptance.",
                    "limits": {"daily_characters": int(config.get("AI33_DAILY_CHARACTER_LIMIT", "0")),
                               "job_characters": int(config.get("AI33_JOB_CHARACTER_LIMIT", "0"))}})


@app.route("/broll/<job_id>")
def broll_review(job_id):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job:
        abort(404)
    root = config.OUTPUT_DIR / job_id
    plan = artifacts.read(root / "plan.json")
    rows = []
    for i, beat in enumerate(plan.get("beats", [])):
        found = next((p for ext in ("mp4", "jpg", "png")
                      for p in (root / "assets" / f"beat_{i:03d}.{ext}", root / "assets" / f"beat_{i:03d}_fb.{ext}")
                      if p.exists()), None)
        row = artifacts.read(found.with_suffix(".stock.json")) if found else {}
        rows.append({"index": i, "text": beat.get("text", ""), "query": beat.get("query", ""),
                     "type": beat.get("type"), "filename": found.name if found else None,
                     "video": found.suffix == ".mp4" if found else False,
                     "source": row.get("nid", ""), "description": row.get("meta", "")})
    return render_template("broll_review.html", job_id=job_id, job=job, beats=rows)


@app.route("/broll/<job_id>/asset/<filename>")
def broll_review_asset(job_id, filename):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    if not re.fullmatch(r"beat_\d{3,6}(?:_fb)?\.(?:mp4|jpg|png)", filename):
        abort(404)
    root = (config.OUTPUT_DIR / job_id / "assets").resolve()
    path = root / filename
    if path.is_symlink() or not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


@app.route("/api/broll/<job_id>/<int:index>", methods=["POST"])
def broll_replace(job_id, index):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    query = str((request.get_json(silent=True) or {}).get("query") or "").strip()
    if not query or len(query) > 250:
        return jsonify({"error": "Enter a search query of 1–250 characters"}), 400
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        if job.get("status") not in ("paused", "error") or job.get("dispatched_to"):
            return jsonify({"error": "Pause the local job before replacing footage"}), 409
        path = config.OUTPUT_DIR / job_id / "plan.json"
        plan = artifacts.read(path)
        beats = plan.get("beats", [])
        if index >= len(beats) or beats[index].get("type") not in ("stock", "stock_image"):
            return jsonify({"error": "Only a stock-footage or stock-photo beat can be replaced here"}), 400
        beats[index]["query"] = query
        for suffix in (".stock.json", "_fb.stock.json"):
            previous = artifacts.read(path.parent / "assets" / f"beat_{index:03d}{suffix}")
            if previous.get("nid"):
                rejected = beats[index].setdefault("rejected_stock_ids", [])
                if previous["nid"] not in rejected:
                    rejected.append(previous["nid"])
        # A per-beat revision supports retrying the same query deliberately.
        beats[index]["selection_revision"] = secrets.token_hex(8)
        artifacts.write(path, plan)
    return jsonify({"ok": True, "message": "Search saved. Resume the job to replace this beat; narration stays cached."})


@app.route("/api/jobs")
def api_jobs():
    """Active/recent jobs — lets a reloaded page re-attach + show the queue.
    Restricted users only see jobs from their allowed profiles.
    Queued jobs are ordered FIFO (oldest first); others newest first after."""
    with LOCK:
        items = list(JOBS.items())
    with _FLEET_LOCK:
        pending_dispatches = {
            jid for worker in FLEET_WORKERS.values()
            for jid in (worker.get("dispatch") or {})
        }
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
            "title": j.get("publication_title") or j.get("title", ""),
            "source_title": j.get("title", ""),
            "publication_title": j.get("publication_title"),
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
            "dispatched_to": j.get("dispatched_to"),
            "dispatch_pending": jid in pending_dispatches,
            "drive": j.get("drive") or {},
            "performance": (getattr(j.get("_cancel"), "metrics", None).snapshot()
                            if getattr(j.get("_cancel"), "metrics", None) else j.get("performance")),
            "queue_reason": ("Waiting for preparation capacity" if j.get("status") == "queued"
                             else "Waiting for worker acknowledgement" if jid in pending_dispatches else ""),
            # Drives the Stop button: only a job with a live cancel token
            # can actually be stopped, and retry_wait can be pulled out of
            # its wait.
            "stoppable": (bool(j.get("_cancel"))
                          or j.get("status") == "retry_wait"
                          # dispatched work is stopped via the worker
                          or bool(j.get("dispatched_to")
                                  and j.get("status") in ("running", "dispatched"))),
            "prioritized": bool(j.get("priority_at")),
            "video_group": j.get("video_group"),
            "title_options": j.get("title_options") or {},
            "package": j.get("package") or {},
            "thumbnail": j.get("thumbnail") or {},
            "description": j.get("description") or {},
        }

    # Show: active first, then queued (FIFO), then recent finished
    active, queued, done = [], [], []
    for jid, j in items:
        if not _allowed_profile(j.get("profile", "")):
            continue
        st = j.get("status")
        # Removed jobs are deliberately excluded from persistence and from
        # the live/recent library immediately; otherwise they reappear as
        # misleading recent rows until the next restart.
        if st == "cancelled":
            continue
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
    """Remove any job that is not actively running.

    Cached files are retained on disk, so this is a recoverable library/queue
    removal rather than destructive output deletion.
    """
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") not in (
                "researching", "awaiting_title", "queued", "retry_wait",
                "paused", "error", "done"):
            return jsonify({
                "error": f"can only remove inactive jobs (status={job.get('status')})"
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
    target = "cancelled" if data.get("cancel") else "paused"
    # NOTE: _persist_queue() acquires LOCK itself and threading.Lock is not
    # reentrant, so it must NEVER be called while holding LOCK — doing so
    # deadlocks the whole app permanently. Decide under the lock, act after.
    token = None
    remote = None
    pending_release = False
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") == "retry_wait" and not job.get("dispatched_to"):
            # Waiting to auto-retry — no thread to stop, just take it out.
            job["status"] = target
            job["step"] = target
            reporter = job.get("_fleet_reporter")
            if reporter:
                reporter.fail("stopped by user")
        elif job.get("dispatched_to") and job.get("_cancel") is None:
            remote = job["dispatched_to"]
            with _FLEET_LOCK:
                worker = FLEET_WORKERS.get(remote)
                not_started = bool(
                    worker and job_id in (worker.get("dispatch") or {}))
                if not_started:
                    worker["dispatch"].pop(job_id, None)
            if not_started:
                # No process exists yet, so an abort would never receive a
                # terminal acknowledgement and leave this job stuck. Apply
                # the requested destination immediately instead.
                job.update(status=target, step=target)
                job.pop("dispatched_to", None)
                job.pop("dispatched_at", None)
                job["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                    "message": f"Stopped before {remote} accepted the dispatch"})
                remote = None
                pending_release = True
            else:
                # Running on a fleet worker: there is no local ffmpeg to kill,
                # so ask that machine to stop it. The worker reports the result
                # back through its normal heartbeat.
                job["_stop_target"] = target
                job["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"), "step": "queue",
                    "message": f"Stop requested — telling {remote} to halt it"})
        else:
            token = job.get("_cancel")
            if token is None:
                return jsonify({
                    "error": (f"job can't be stopped right now "
                              f"(status={job.get('status')}) — it has no active "
                              f"worker on this machine")
                }), 409
            job["_stop_target"] = target

    # --- everything below runs WITHOUT LOCK held ---
    if pending_release:
        _persist_queue()
        return jsonify({"ok": True, "job_id": job_id,
                        "paused": target == "paused", "not_started": True})
    if remote is not None:
        with _FLEET_LOCK:
            # Only queue an abort for a worker that has actually registered.
            # setdefault() here would invent a nameless phantom worker that
            # then shows up in the Workers panel with no hardware and never
            # goes away.
            w = FLEET_WORKERS.get(remote)
            if w is None:
                return jsonify({
                    "error": f"worker '{remote}' is not connected — it can't be "
                             f"told to stop. It will report in when it returns."}), 409
            w.setdefault("aborts", []).append(job_id)
            w.get("dispatch", {}).pop(job_id, None)  # don't re-send it
        _persist_queue()
        return jsonify({"ok": True, "job_id": job_id, "remote": remote,
                        "paused": target == "paused",
                        "note": "asked the worker to stop; may take a heartbeat"})
    killed = token.cancel() if token is not None else 0
    _persist_queue()
    return jsonify({"ok": True, "job_id": job_id, "paused": target == "paused",
                    "killed_processes": killed})


@app.route("/api/queue/<job_id>/resume", methods=["POST"])
def resume_paused(job_id: str):
    """Put a paused job back in the queue. It continues from whatever is
    already on disk rather than restarting."""
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "paused":
            return jsonify({
                "error": f"job is not paused (status={job.get('status')})"}), 409
        job["status"] = "queued"
        job["step"] = "queued"
        job["priority_at"] = "0:" + datetime.now().isoformat(timespec="seconds")
        job["log"].append({"t": datetime.now().strftime("%H:%M:%S"),
                           "step": "queue", "message": "Resumed"})
    _persist_queue()
    _kick_queue()
    return jsonify({"ok": True, "job_id": job_id})


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


@app.route("/api/queue/profile/<profile>/pause", methods=["POST"])
def pause_profile_queue(profile: str):
    """Pause pending work for one profile without halting other channels."""
    if not _allowed_profile(profile):
        abort(403)
    data = request.get_json(silent=True) or {}
    paused = bool(data.get("paused", True))
    changed = []
    with LOCK:
        for job_id, job in JOBS.items():
            if job.get("profile") != profile:
                continue
            if paused and job.get("status") in ("queued", "retry_wait"):
                job["status"] = "paused"
                job["step"] = "paused"
                job["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "step": "queue", "message": "Paused with profile queue",
                })
                changed.append(job_id)
    _persist_queue()
    return jsonify({"ok": True, "profile": profile, "paused": paused,
                    "paused_jobs": changed})


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
        "final": video_render.is_valid_video(job_dir / "final.mp4"),
        "part1": video_render.is_valid_video(job_dir / "final_part1.mp4"),
        "part2": video_render.is_valid_video(job_dir / "final_part2.mp4"),
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
        title = str(job.get("publication_title") or job.get("title") or job_id)
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
    if not title:  # server restarted since — the job dir remembers
        f = job_dir / "inputs.json"
        if f.exists():
            title = (json.loads(f.read_text()).get("title") or "").strip()
    local_role = "part1" if path.name == "final_part1.mp4" else (
        "part2" if path.name == "final_part2.mp4" else "final")
    return send_file(path, mimetype="video/mp4", as_attachment=download,
                     download_name=_video_drive_name(title or job_id, local_role))


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


@app.route("/api/drive/disconnect", methods=["POST"])
def drive_disconnect():
    with LOCK:
        active = [job_id for job_id, job in JOBS.items()
                  if job.get("status") == "uploading" or (job.get("drive") or {}).get("state") == "uploading"]
    if active:
        return jsonify({
            "error": "Drive is uploading a video; wait for it to finish before changing accounts"
        }), 409
    with _DRIVE_UPLOAD_LOCK:
        revoked = DRIVE.disconnect(revoke=True)
    return jsonify({"ok": True, "revoked": revoked})


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
    job_dir = config.OUTPUT_DIR / job_id
    upload_video = False
    upload_package = False
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        drive_state = (job.get("drive") or {}).get("state")
        if drive_state == "uploading":
            return jsonify({"error": "Drive upload already running"}), 409
        if job.get("status") not in ("done", "error", "paused"):
            return jsonify({"error": "Wait for this job to finish or stop before retrying its upload"}), 409
        upload_video = bool(_drive_deliverables(job_dir))
        package_state = (job.get("package") or {}).get("state")
        remote_files = ((job.get("drive") or {}).get("files") or {})
        has_uploaded_video = any(role in remote_files
                                 for role in ("final", "part1", "part2"))
        upload_package = bool(
            _profile_wants_docx(job.get("profile", ""))
            and has_uploaded_video and package_state != "uploaded")
        if not upload_video and not upload_package:
            return jsonify({
                "error": "no local video or finished titles document is available to upload"
            }), 409
        if upload_video:
            job["drive"] = {"state": "uploading",
                            "files": (job.get("drive") or {}).get("files") or {}}
    _persist_queue()
    target = _upload_job_deliverables if upload_video else _upload_profile_docx
    threading.Thread(target=target, args=(job_id, job_dir), daemon=True).start()
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
            "gpu_encoder": config.gpu_encoder(),
        },
    })


# ── Read-only AI operations center ──────────────────────────────
def _ops_snapshot() -> dict:
    """Small, secret-free snapshot suitable for a remote language model."""
    now = datetime.now()
    stale_after = float(config.queue_setting("stage_stale_after_minutes") or 90)
    with LOCK:
        jobs_copy = [(jid, dict(job)) for jid, job in JOBS.items()]
    job_rows = []
    for jid, job in jobs_copy:
        updated = job.get("updated_at") or job.get("created_at")
        age_minutes = None
        if updated:
            try:
                age_minutes = max(0.0, (now - datetime.fromisoformat(updated)).total_seconds() / 60)
            except (TypeError, ValueError):
                pass
        status_now = job.get("status") or "unknown"
        job_rows.append({
            "video_id": jid,
            "title": str(job.get("title") or "")[:240],
            "profile": job.get("profile"),
            "status": status_now,
            "step": job.get("step"),
            "progress_pct": job.get("progress_pct", 0),
            "worker": job.get("dispatched_to") or "main",
            "age_since_update_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            "stalled": bool(status_now == "running" and age_minutes is not None
                            and age_minutes >= stale_after),
            "error": str(job.get("error") or "")[-1500:],
            "thumbnail_state": (job.get("thumbnail") or {}).get("state"),
            "recent_events": [
                {"step": str(x.get("step") or "")[:80],
                 "message": str(x.get("message") or "")[:500]}
                for x in (job.get("log") or [])[-3:] if isinstance(x, dict)
            ],
        })
    workers = _fleet_worker_rows()
    counts: dict[str, int] = {}
    for row in job_rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    # Keep every non-terminal/error job plus only a small recent-completion
    # sample. Counts still cover the whole library; this keeps a once/minute
    # observer from repeatedly paying to reread dozens of healthy old jobs.
    important = [r for r in job_rows if r["status"] not in ("done", "cancelled")]
    recent_done = [r for r in reversed(job_rows) if r["status"] == "done"][:5]
    monitored_jobs = (important[:30] + recent_done)[:35]
    statistics = _statistics()
    statistics.pop("daily", None)  # Keep minute-by-minute prompts compact.
    statistics.pop("filters", None)
    cluster_state = _cluster_snapshot()
    cluster_for_ai = {
        "summary": cluster_state.get("summary"),
        "update": cluster_state.get("update"),
        "nodes": [{
            "worker_id": node.get("worker_id"), "name": node.get("name"),
            "role": node.get("role"), "online": node.get("online"),
            "health": node.get("health"), "capacity": node.get("capacity"),
            "cpu": (node.get("node") or {}).get("cpu"),
            "memory": (node.get("node") or {}).get("memory"),
            "gpu": (node.get("node") or {}).get("gpu"),
            "agent": node.get("agent"), "jobs": node.get("jobs"),
            "changes": ((node.get("node") or {}).get("changes") or [])[-10:],
        } for node in cluster_state.get("nodes", [])],
        "events": (cluster_state.get("events") or [])[:25],
    }
    return {
        "captured_at": now.isoformat(timespec="seconds"),
        "fleet_role": config.FLEET_ROLE,
        "queue_paused": bool(config.queue_setting("queue_paused")),
        "job_counts": counts,
        "jobs": monitored_jobs,
        "workers": workers,
        "main_hardware": {"cpu_cores": config._detect_cpu_count(),
                          "ram_gb": round(config._detect_ram_gb(), 1),
                          "h264_encoder": config.gpu_encoder() or "libx264"},
        "disk": operations.disk_snapshot(),
        "drive": {"configured": DRIVE.configured, "authorized": DRIVE.authorized},
        "ai33": clients.ai33_key_status(),
        "statistics": statistics,
        "cluster": cluster_for_ai,
    }


def _cluster_observer_loop() -> None:
    while not _CLUSTER_STOP.wait(10):
        try:
            _cluster_snapshot()
        except Exception as exc:  # noqa: BLE001
            print(f"[cluster] snapshot failed: {exc}")


def _ops_center() -> operations.OperationsCenter:
    global OPS
    if OPS is None:
        OPS = operations.OperationsCenter(_ops_snapshot, interval=300)
    return OPS


@app.route("/api/ops/status")
def ops_status():
    return jsonify(_ops_center().status())


@app.route("/api/ops/chat", methods=["POST"])
def ops_chat():
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    statistics = None
    if "days" in body:
        try:
            days = int(body["days"])
            if not 1 <= days <= 365:
                raise ValueError()
        except (TypeError, ValueError):
            return jsonify({"error": "days must be between 1 and 365"}), 400
        statistics = _statistics(days, str(body.get("profile") or "") or None)
    try:
        return jsonify(_ops_center().chat(question, statistics=statistics))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/ops/ai33-key", methods=["GET", "POST"])
def ops_ai33_key():
    """Owner control for AI33 account preference; never returns key values."""
    if request.method == "GET":
        return jsonify(clients.ai33_key_status())
    mode = str((request.get_json(silent=True) or {}).get("mode") or "").strip()
    try:
        status = clients.set_ai33_key_mode(mode, "owner API request")
        _ops_center()._append({
            "kind": "action", "model": "owner",
            "severity": "info", "headline": "AI33 account preference changed",
            "message": f"New AI33 tasks now prefer the {status['active']} account.",
            "root_cause_risk": "low", "report_recommended": False,
            "evidence": [f"mode={status['mode']}", f"active={status['active']}"],
            "ai33": status,
        })
        return jsonify(status)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ops/report", methods=["POST"])
def ops_report():
    reason = str((request.get_json(silent=True) or {}).get("reason")
                 or "Owner-requested system review").strip()
    try:
        return jsonify(_ops_center().create_report(reason))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/ops/report/<report_id>")
def ops_report_file(report_id: str):
    if not identity.is_numeric_id(report_id):
        abort(404)
    path = config.OUTPUT_DIR / "reports" / f"{report_id}.md"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="text/markdown", as_attachment=True,
                     download_name=f"dmiqwi-report-{report_id}.md")


# ── Thumbnail engine — used by video approvals and the standalone page ──
THUMB_DIR = config.ROOT / "thumbnails"
THUMB_TEMPLATES_DIR = THUMB_DIR / "templates"
THUMB_OUT_DIR = THUMB_DIR / "generated"
THUMB_UPLOADS_DIR = THUMB_DIR / "uploads"
THUMB_REFERENCES_DIR = THUMB_DIR / "references"
for _d in (THUMB_TEMPLATES_DIR, THUMB_OUT_DIR, THUMB_UPLOADS_DIR,
           THUMB_REFERENCES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/webp": ".webp"}


def _thumbnail_character(profile_name: str, inputs: dict) -> tuple[Path | None, str]:
    """Resolve a profile/cited-author character without asking for an upload."""
    try:
        profile = orchestrator.load_profile(profile_name)
    except Exception:  # noqa: BLE001
        profile = {}
    thumb_profile = profile.get("thumbnail") or {}
    if thumb_profile.get("no_character"):
        return None, str(thumb_profile.get("display_name") or
                         profile_name.replace("-", " ").upper())
    cited = (inputs.get("cited_philosophers") or "").split(",", 1)[0].strip()
    lowered = cited.lower()
    author_map = {
        "murphy": (config.ROOT / "figures/murphy.png", "Joseph Murphy"),
        "jung": (config.ROOT / "figures/jung.png", "Carl Gustav Jung"),
        "freud": (config.ROOT / "figures/freud.png", "Sigmund Freud"),
    }
    for token, pair in author_map.items():
        if token in lowered:
            return pair
    fig = ((profile.get("video") or {}).get("figure") or {}).get("image")
    if fig:
        path = config.ROOT / fig
        if path.exists():
            stem = path.stem.lower()
            name = {"murphy": "Joseph Murphy", "jung": "Carl Gustav Jung",
                    "freud": "Sigmund Freud"}.get(stem, cited or path.stem.title())
            return path, name
    host = profile.get("host") or {}
    if host.get("image"):
        path = config.ROOT / str(host["image"])
        if path.exists():
            return path, cited or profile_name.replace("-", " ").title()
    path = config.HOSTS_DIR / f"{profile_name}.png"
    if path.exists():
        names = {"grandma-hannah": "Grandma Hannah",
                 "matthias-richter": "Matthias Richter"}
        return path, names.get(profile_name, cited or profile_name.replace("-", " ").title())
    # Philosophy profiles without a corner figure still get a safe default.
    fallback = config.ROOT / "figures/murphy.png"
    return (fallback if fallback.exists() else None), (cited or "Joseph Murphy")


def _thumbnail_reference_folder(character_name: str) -> Path | None:
    reference_sets = {"joseph murphy": "joseph-murphy"}
    folder_name = reference_sets.get(" ".join(character_name.casefold().split()))
    return THUMB_REFERENCES_DIR / folder_name if folder_name else None


def _thumbnail_reference_templates(character_name: str) -> list[Path]:
    """Approved style examples for a specific displayed person.

    These are full thumbnails, not portrait cutouts: the remote editor uses
    the chosen example as image 1 (composition/style) and the clean figure as
    image 2 (identity). Keeping the mapping explicit prevents Murphy's visual
    language from leaking into Jung, Freud, or host thumbnails.
    """
    folder = _thumbnail_reference_folder(character_name)
    if folder is None:
        return []
    return sorted(
        path for path in folder.iterdir()
        if (path.is_file()
            and path.suffix.casefold() in (".png", ".jpg", ".jpeg", ".webp"))
    ) if folder.exists() else []


def _thumbnail_reference_portraits(character_name: str) -> list[Path]:
    """High-resolution identity references, kept separate from layouts."""
    root = _thumbnail_reference_folder(character_name)
    folder = root / "portraits" if root is not None else None
    if folder is None or not folder.exists():
        return []
    return sorted(
        path for path in folder.iterdir()
        if (path.is_file()
            and path.suffix.casefold() in (".png", ".jpg", ".jpeg", ".webp"))
    )


def _thumbnail_template_for(job_id: str, generation: int,
                            character_name: str,
                            fallback: Path | None) -> tuple[Path | None, str | None]:
    """Rotate reproducibly through one person's approved reference pool."""
    references = _thumbnail_reference_templates(character_name)
    if not references:
        return fallback, None
    seed = sum(f"{job_id}:{max(0, generation)}".encode("utf-8"))
    selected = references[seed % len(references)]
    try:
        label = selected.relative_to(config.ROOT).as_posix()
    except ValueError:
        label = selected.name
    return selected, label


def _thumbnail_portrait_for(job_id: str, generation: int,
                            character_name: str,
                            fallback: Path | None) -> tuple[Path | None, str | None]:
    """Rotate through approved identity portraits independently of layout."""
    references = _thumbnail_reference_portraits(character_name)
    if not references:
        return fallback, None
    seed = sum(f"{job_id}:{max(0, generation)}:portrait".encode("utf-8"))
    selected = references[seed % len(references)]
    try:
        label = selected.relative_to(config.ROOT).as_posix()
    except ValueError:
        label = selected.name
    return selected, label


def _thumbnail_language(profile_name: str, inputs: dict) -> str:
    explicit = str(inputs.get("language") or "").strip()
    if explicit:
        aliases = {
            "de": "German", "de-de": "German", "deutsch": "German",
            "german": "German", "en": "English", "en-us": "English",
            "en-gb": "English", "english": "English",
        }
        return aliases.get(explicit.casefold(), explicit)
    try:
        defaults = orchestrator.load_profile(profile_name).get("defaults", {})
        return str(defaults.get("language") or "English").strip()
    except Exception:  # noqa: BLE001
        return "English"


def _thumbnail_shows_name(profile_name: str) -> bool:
    """Whether the thumbnail includes the bottom person/channel label."""
    try:
        thumb = orchestrator.load_profile(profile_name).get("thumbnail") or {}
        return bool(thumb.get("show_name", True))
    except Exception:  # noqa: BLE001
        return True


def _profile_locks_submitted_title(profile_name: str) -> bool:
    """Whether thumbnail packaging must preserve the job's selected title."""
    try:
        profile = orchestrator.load_profile(profile_name)
        return bool((profile.get("title") or {}).get("lock_submitted")
                    or (profile.get("research") or {}).get("enabled"))
    except Exception:  # noqa: BLE001
        return False


_TITLE_THUMBNAIL_GUIDE = """
TITLE + THUMBNAIL PACKAGING RULES:
- The emotional trigger is: "this can change my life; I need to watch now."
- Promise a truthful improvement such as money, health, luck, sleep, peace,
  success, or inner change. Bold, exciting clickbait is welcome; lying is not.
- The title and thumbnail are a team and MUST use different wording. The title
  gives one compelling idea; the thumbnail supplies a second reward, secret,
  or curiosity gap. Never merely shorten or repeat the title on the thumbnail.
- Keep both short, clear, and readable immediately. A number, mystery, or
  secret feeling can help when truthful. Add the famous teacher's name at the
  end of the title with a dash when it fits.
- Thumbnail main text uses very few words and may use one exclamation mark.
- Final check: life-change trigger, complementary wording, fast readability,
  exciting but supported by the video's actual topic.
""".strip()


def _thumbnail_words_task(job_id: str) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        title, profile_name = job["title"], job["profile"]
        inputs = dict(job.get("inputs") or {})
    if not _profile_uses_thumbnail(profile_name):
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id]["thumbnail"] = {"state": "disabled"}
        _persist_queue()
        return
    _, character_name = _thumbnail_character(profile_name, inputs)
    language = _thumbnail_language(profile_name, inputs)
    show_name = _thumbnail_shows_name(profile_name)
    lock_title = _profile_locks_submitted_title(profile_name)
    try:
        words = llm.chat_json(
            f"Act as a senior native-{language} YouTube packaging editor. "
            "Silently draft at least six genuinely different title/thumbnail "
            "packages, critique them for specificity, curiosity, readability, "
            "and natural phrasing, then return only the strongest one. "
            f"Every audience-facing word MUST be idiomatic {language}; English "
            "is forbidden unless it is part of a proper person's name. Do not "
            "translate word-for-word from the source. Write as a native editor. "
            "The thumbnail must express a concrete second idea that complements "
            "the title without repeating or paraphrasing it. Avoid generic filler "
            "such as 'change your life', 'the power within', 'secret', or 'now' "
            "unless the supplied topic specifically supports it. Never return "
            "weak isolated modifiers such as 'old', 'new', or 'important' as a "
            "box label. The main thumbnail line must be 3-6 words. Each colored "
            "box must contain 1-3 meaningful words and the two boxes must add "
            "useful context rather than splitting one ordinary phrase. Do not "
            "change the topic or promise an outcome the source cannot support. "
            "Return ONLY JSON with: localized_title (natural high-click title in "
            f"{language}), main_text, red_word, yellow_word, and name. The name "
            "is the displayed person/teacher and is not translated. Avoid "
            "quotation marks and fake certainty. The owner approves the package "
            "before image generation.\n\n"
            f"{_TITLE_THUMBNAIL_GUIDE}\n\n"
            f"Source title: {title}\nTarget language: {language}\n"
            f"Profile: {profile_name}\nDisplayed person: {character_name}\n"
            + ("Keep the name field as the displayed person above."
               if show_name else
               "This profile has no bottom name label: return name as an empty string."),
            "gpt-5.6-terra", max_tokens=500, attempts=2, retries=1, timeout=90)
        localized_title = (title if lock_title else
                           str(words.get("localized_title") or "").strip())[:220]
        clean = {
            "main_text": str(words.get("main_text") or "").strip()[:120],
            "red_word": str(words.get("red_word") or "").strip()[:50],
            "yellow_word": str(words.get("yellow_word") or "").strip()[:50],
            "name": (str(words.get("name") or character_name).strip()[:80]
                     if show_name else ""),
        }
        required_words = ("main_text", "red_word", "yellow_word")
        if (not localized_title
                or not all(clean[key] for key in required_words)
                or (show_name and not clean["name"])):
            raise RuntimeError("Terra returned an incomplete localized title/thumbnail package")
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["thumbnail"] = {
                    "state": "words_ready", "words": clean,
                    "localized_title": localized_title,
                    "language": language,
                }
                job["updated_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["thumbnail"] = {"state": "error", "stage": "words",
                                    "error": str(exc)}
    _persist_queue()


def _start_thumbnail_words(job_id: str) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        profile_name = str((job or {}).get("profile") or "")
    if not job or not _profile_uses_thumbnail(profile_name):
        return
    threading.Thread(target=_thumbnail_words_task, args=(job_id,), daemon=True,
                     name=f"dmiqwi-thumb-words-{job_id}").start()


def _thumbnail_image_task(job_id: str) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        profile_name = job["profile"]
        inputs = dict(job.get("inputs") or {})
        thumb_state = dict(job.get("thumbnail") or {})
        words = dict(thumb_state.get("words") or {})
        localized_title = str(thumb_state.get("localized_title") or "").strip()
        language = str(thumb_state.get("language") or
                       _thumbnail_language(profile_name, inputs)).strip()
        try:
            generation = max(0, int(thumb_state.get("generation") or 0))
        except (TypeError, ValueError):
            generation = 0
    default_character, default_name = _thumbnail_character(profile_name, inputs)
    show_name = _thumbnail_shows_name(profile_name)
    default_template = next(iter(THUMB_TEMPLATES_DIR.glob("default.*")), None)
    template_path, reference_template = _thumbnail_template_for(
        job_id, generation, default_name, default_template)
    character_path, reference_portrait = _thumbnail_portrait_for(
        job_id, generation, default_name, default_character)
    try:
        if not template_path:
            raise RuntimeError("save a default thumbnail template on the Thumbnails page")
        if character_path is not None and not character_path.exists():
            raise RuntimeError(f"no thumbnail character image is configured for {profile_name}")
        thumbnail_id = identity.numeric_id(
            taken=(p.stem for p in THUMB_OUT_DIR.glob("*.png")))
        dest = THUMB_OUT_DIR / f"{thumbnail_id}.png"
        thumbnail.generate(
            template_path, character_path, dest,
            main_text=words["main_text"], red_word=words["red_word"],
            yellow_word=words["yellow_word"],
            name=((words.get("name") or default_name) if show_name else ""),
            show_name=show_name, topic_hint=default_name)
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["thumbnail"] = {
                    "state": "image_ready", "words": words,
                    "localized_title": localized_title,
                    "language": language,
                    "generation": generation,
                    "reference_template": reference_template,
                    "reference_portrait": reference_portrait,
                    "thumbnail_id": thumbnail_id,
                    "url": f"/api/thumbnail/file/{thumbnail_id}",
                }
                job["updated_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                job["thumbnail"] = {"state": "error", "stage": "image",
                                    "words": words,
                                    "localized_title": localized_title,
                                    "language": language,
                                    "generation": generation,
                                    "reference_template": reference_template,
                                    "reference_portrait": reference_portrait,
                                    "error": str(exc)}
    _persist_queue()


@app.route("/api/thumbnail/job/<job_id>/words/approve", methods=["POST"])
def approve_thumbnail_words(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    data = request.get_json(force=True) or {}
    words = {k: str(data.get(k) or "").strip() for k in
             ("main_text", "red_word", "yellow_word", "name")}
    localized_title = str(data.get("localized_title") or "").strip()
    with LOCK:
        job = JOBS.get(job_id)
        profile_name = job.get("profile", "") if job else ""
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if not _profile_uses_thumbnail(profile_name):
            return jsonify({"error": "thumbnail generation is disabled for this profile"}), 409
        if _profile_locks_submitted_title(profile_name):
            localized_title = str(job.get("title") or localized_title).strip()
    show_name = _thumbnail_shows_name(profile_name)
    required_words = ("main_text", "red_word", "yellow_word")
    if (not localized_title
            or not all(words[key] for key in required_words)
            or (show_name and not words["name"])):
        return jsonify({"error": "the localized title and required thumbnail word fields are required"}), 400
    naming_error = _title_naming_error(
        profile_name, localized_title, exclude_job_id=job_id)
    if naming_error:
        return jsonify({"error": naming_error}), 409
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        language = _thumbnail_language(job["profile"], job.get("inputs") or {})
        if _profile_locks_submitted_title(job["profile"]):
            localized_title = str(job.get("title") or localized_title).strip()
        job["publication_title"] = localized_title[:220]
        job["thumbnail"] = {
            "state": "generating_image", "words": words,
            "localized_title": localized_title[:220], "language": language,
            "generation": 0,
            "words_approved_at": datetime.now().isoformat(timespec="seconds"),
        }
    _persist_queue()
    threading.Thread(target=_thumbnail_image_task, args=(job_id,), daemon=True,
                     name=f"dmiqwi-thumbnail-{job_id}").start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/thumbnail/job/<job_id>/words/retry", methods=["POST"])
def retry_thumbnail_words(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if not _profile_uses_thumbnail(job.get("profile", "")):
            return jsonify({"error": "thumbnail generation is disabled for this profile"}), 409
        job["thumbnail"] = {"state": "generating_words"}
    _persist_queue()
    _start_thumbnail_words(job_id)
    return jsonify({"ok": True})


@app.route("/api/thumbnail/job/<job_id>/image/approve", methods=["POST"])
def approve_thumbnail_image(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        state = dict(job.get("thumbnail") or {})
        if state.get("state") != "image_ready":
            return jsonify({"error": "thumbnail image is not ready for approval"}), 409
        state["state"] = "approved"
        state["approved_at"] = datetime.now().isoformat(timespec="seconds")
        job["thumbnail"] = state
    _persist_queue()
    return jsonify({"ok": True, "thumbnail_id": state.get("thumbnail_id")})


@app.route("/api/thumbnail/job/<job_id>/image/regenerate", methods=["POST"])
def regenerate_thumbnail_image(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if not _profile_uses_thumbnail(job.get("profile", "")):
            return jsonify({"error": "thumbnail generation is disabled for this profile"}), 409
        state = dict(job.get("thumbnail") or {})
        if not state.get("words"):
            return jsonify({"error": "approve thumbnail words first"}), 409
        try:
            next_generation = max(0, int(state.get("generation") or 0)) + 1
        except (TypeError, ValueError):
            next_generation = 1
        job["thumbnail"] = {
            "state": "generating_image", "words": state["words"],
            "localized_title": state.get("localized_title") or
                               job.get("publication_title") or job.get("title"),
            "language": state.get("language") or
                        _thumbnail_language(job["profile"], job.get("inputs") or {}),
            # Every regeneration deliberately advances to the next approved
            # reference rather than producing a near-duplicate of the last.
            "generation": next_generation,
        }
    _persist_queue()
    threading.Thread(target=_thumbnail_image_task, args=(job_id,), daemon=True,
                     name=f"dmiqwi-thumbnail-{job_id}").start()
    return jsonify({"ok": True})


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
        default_model=config.KIE_THUMBNAIL_MODEL,
        owner_access=not ACCESS or getattr(g, "user", {}).get("profiles") is None)


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

    job_id = identity.numeric_id(
        taken=(p.stem for p in THUMB_OUT_DIR.glob("*.png")))
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
    return jsonify({"job_id": job_id, "thumbnail_id": job_id,
                    "url": f"/api/thumbnail/file/{job_id}"})


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
    port = int(os.environ.get("PORT", 5066))
    host = (os.environ.get("HOST") or "").strip() or (
        "0.0.0.0" if ACCESS else "127.0.0.1")
    _encoder = config.gpu_encoder() or "libx264"
    print(f"Dmiqwi → http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port} "
         f"(MAX_CONCURRENT_JOBS={config.MAX_CONCURRENT_JOBS}, "
         f"beat_workers: {config.resolve_beat_workers(False)} CPU / "
         f"{config.resolve_beat_workers(True)} GPU, "
         f"H.264 encoder: {_encoder})")
    # Restore any queue that survived the previous process, then drain it.
    _restore_queue()
    atexit.register(_persist_queue)
    if config.FLEET_ROLE != "worker":
        OPS = _ops_center()
        OPS.start()
        atexit.register(OPS.stop)
        _node_monitor()
        _CLUSTER_STOP.clear()
        threading.Thread(target=_cluster_observer_loop, daemon=True,
                         name="southcube-cluster-history").start()
        atexit.register(_CLUSTER_STOP.set)
        atexit.register(_node_monitor().stop)
        print(f"[ops] read-only {config.OPENROUTER_MONITOR_MODEL} monitor active "
              "(GPT-5.6 Terra fallback, 5-minute updates)")
    if config.FLEET_ROLE == "worker":
        FLEET_WORKER = fleet.FleetWorker(_run_fleet_worker_job,
                                         aborter=_abort_local_job)
        FLEET_WORKER.start()
        atexit.register(FLEET_WORKER.stop)
        print(f"[fleet] worker {FLEET_WORKER.worker_id} polling {config.FLEET_MAIN_URL}")
    app.run(host=host, port=port, debug=False, threaded=True)
