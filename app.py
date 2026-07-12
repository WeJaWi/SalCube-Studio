"""Local web UI for the Grandma Hannah senior-health video studio."""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, g, jsonify, make_response, redirect,
                   render_template, request, send_file, abort)

from pipeline import animation, config, orchestrator

app = Flask(__name__)

# In-memory job registry. Fine for a single-user local tool.
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()

# ── Access control (only active when ACCESS_KEYS is set in .env) ──
# Each key belongs to a named user with an allowed-profiles set; None = all.
ACCESS = config.access_keys()

_LOGIN_PAGE = """<!doctype html><html><head><title>Health Studio</title>
<style>body{background:#0d1117;color:#e6edf3;font-family:-apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#161b22;padding:36px;border-radius:12px;border:1px solid #30363d;
text-align:center}input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;
border-radius:8px;padding:10px 14px;font-size:15px;width:240px;margin:14px 0}
button{background:#2C8C8C;color:#fff;border:0;border-radius:8px;padding:10px 22px;
font-size:15px;cursor:pointer}h2{margin:0}</style></head><body>
<form method="post" action="/login"><h2>🎬 Video Studio</h2>
<input type="password" name="key" placeholder="Access key" autofocus />
<br/><button>Enter</button>{ERR}</form></body></html>"""


def _user_for(key):
    return next((u for u in ACCESS if key and u["key"] == key), None)


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


def _run_job(job_id: str, inputs: dict, profile: str) -> None:
    job_dir = config.OUTPUT_DIR / job_id

    def progress(step: str, message: str) -> None:
        with LOCK:
            job = JOBS[job_id]
            job["step"] = step
            job["log"].append({"t": datetime.now().strftime("%H:%M:%S"),
                               "step": step, "message": message})

    def gate(hooks: dict, outline: str):
        """Publish hooks+outline, block until /api/approve supplies the picks."""
        event = threading.Event()
        with LOCK:
            JOBS[job_id]["status"] = "awaiting_approval"
            JOBS[job_id]["awaiting"] = {"hooks": hooks, "outline": outline}
            JOBS[job_id]["_gate_event"] = event
        event.wait()
        with LOCK:
            approval = JOBS[job_id].pop("approval")
            JOBS[job_id].pop("awaiting", None)
            JOBS[job_id].pop("_gate_event", None)
            JOBS[job_id]["status"] = "running"
        hook_text = approval.get("hook_text") or hooks.get(
            approval.get("hook_type", "bold_statement"), "")
        return hook_text, (approval.get("outline") or outline)

    def voice_gate(info: dict) -> None:
        """Pause after TTS: show total/cut time, wait for /api/approve_voice."""
        event = threading.Event()
        with LOCK:
            JOBS[job_id]["status"] = "awaiting_voice_approval"
            JOBS[job_id]["voice_info"] = info
            JOBS[job_id]["_voice_event"] = event
        event.wait()
        with LOCK:
            JOBS[job_id].pop("voice_info", None)
            JOBS[job_id].pop("_voice_event", None)
            JOBS[job_id]["status"] = "running"

    try:
        with LOCK:
            JOBS[job_id]["status"] = "running"
        out = orchestrator.run_pipeline(inputs, profile, job_dir, progress,
                                        gate, voice_gate)
        with LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["result"] = str(out)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["log"].append({"t": datetime.now().strftime("%H:%M:%S"),
                                       "step": "error", "message": str(e)})


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


@app.route("/api/render", methods=["POST"])
def start_render():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title and not (data.get("resume_job") or "").strip():
        return jsonify({"error": "title is required"}), 400
    profile = (data.get("profile") or "grandma-hannah").strip()
    if not _allowed_profile(profile):
        return jsonify({"error": "this profile is not available to you"}), 403
    rj = (data.get("resume_job") or "").strip()
    if rj and not _allowed_profile(_job_profile(rj) or ""):
        return jsonify({"error": "this job is not available to you"}), 403
    broll = [k for k in ("stock", "stock_image", "ai_video", "animation")
             if data.get(f"broll_{k}")]
    anim_templates = data.get("anim_templates")
    inputs = {
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
    }
    # resume_job: re-run an interrupted job's directory — the pipeline reuses
    # every asset already on disk (script, plan, TTS, HeyGen clips). The
    # original settings are read back from the job's inputs.json, so a bare
    # {"resume_job": id} restores everything; explicit fields still override.
    resume = (data.get("resume_job") or "").strip()
    job_id = resume or uuid.uuid4().hex[:12]
    job_dir = config.OUTPUT_DIR / job_id
    inputs_file = job_dir / "inputs.json"
    if resume and inputs_file.exists():
        saved = json.loads(inputs_file.read_text())
        broll_in_request = any(f"broll_{x}" in data for x in
                               ("stock", "stock_image", "ai_video", "animation"))
        for k, v in saved.items():
            if k == "_profile" or (k == "broll" and broll_in_request):
                continue
            if k not in data:  # only fill what the request didn't specify
                inputs[k] = v
        title = inputs.get("title") or title
        profile = saved.get("_profile") or profile
    job_dir.mkdir(parents=True, exist_ok=True)
    inputs_file.write_text(json.dumps({**inputs, "_profile": profile}, indent=1))
    with LOCK:
        JOBS[job_id] = {"status": "queued", "step": "queued", "log": [],
                        "title": inputs.get("title") or title,
                        "profile": profile, "inputs": inputs}
    threading.Thread(target=_run_job, args=(job_id, inputs, profile), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/approve/<job_id>", methods=["POST"])
def approve(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    data = request.get_json(force=True)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        event = job.get("_gate_event")
        if not event or job.get("status") != "awaiting_approval":
            return jsonify({"error": "job is not awaiting approval"}), 409
        job["approval"] = {
            "hook_type": (data.get("hook_type") or "bold_statement").strip(),
            "hook_text": (data.get("hook_text") or "").strip(),
            "outline": (data.get("outline") or "").strip(),
        }
    event.set()
    return jsonify({"ok": True})


@app.route("/api/jobs")
def jobs():
    """Active/recent jobs, newest first — lets a reloaded page re-attach.
    Restricted users only see jobs from their allowed profiles."""
    with LOCK:
        items = list(JOBS.items())
    return jsonify([
        {"job_id": jid, "status": j["status"], "title": j.get("title", "")}
        for jid, j in reversed(items)
        if _allowed_profile(j.get("profile", ""))
    ])


@app.route("/api/approve_voice/<job_id>", methods=["POST"])
def approve_voice(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        event = job.get("_voice_event")
        if not event or job.get("status") != "awaiting_voice_approval":
            return jsonify({"error": "job is not awaiting voice approval"}), 409
    event.set()
    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: v for k, v in job.items() if not k.startswith("_")})


@app.route("/api/video/<job_id>")
def video(job_id: str):
    if not _allowed_profile(_job_profile(job_id) or ""):
        abort(403)
    path = config.OUTPUT_DIR / job_id / "final.mp4"
    if not path.exists():
        abort(404)
    # ?download=1 → save-as instead of playing in the browser tab
    download = request.args.get("download") == "1"
    with LOCK:
        title = (JOBS.get(job_id) or {}).get("title")
    if not title:  # server restarted since — the job dir remembers
        f = config.OUTPUT_DIR / job_id / "inputs.json"
        if f.exists():
            title = (json.loads(f.read_text()).get("title") or "").strip()
    title = title or job_id
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[:80]
    return send_file(path, mimetype="video/mp4", as_attachment=download,
                     download_name=f"{safe or job_id}.mp4")


@app.route("/api/host/<profile>")
def host_image(profile: str):
    if not _allowed_profile(profile):
        abort(403)
    """Preview of the host fallback still for a profile (404 if none)."""
    path = config.HOSTS_DIR / f"{Path(profile).stem}.png"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/png")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5062))
    print(f"Senior Health Studio → http://127.0.0.1:{port}")
    # With access keys configured, listen on all interfaces so the studio is
    # reachable over Tailscale; without them, stay localhost-only.
    app.run(host="0.0.0.0" if ACCESS else "127.0.0.1",
            port=port, debug=False, threaded=True)
