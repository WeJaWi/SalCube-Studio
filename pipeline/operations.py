"""AI operations center for the local render fleet.

The model never receives mutation tools or credentials. The host grants one
deterministic, narrowly scoped action: prefer the configured AI33 backup only
after explicit credit/quota-exhaustion evidence. Everything else stays
read-only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from . import clients, config, identity, llm


_MODEL = "openrouter-monitor"
_DISPLAY_MODEL = config.OPENROUTER_MONITOR_MODEL
_FALLBACK_MODEL = "gpt-5.6-terra"
_FALLBACK_DISPLAY_MODEL = "gpt-5.6-terra"
_MODEL_CHAIN = f"{_DISPLAY_MODEL} → {_FALLBACK_DISPLAY_MODEL} fallback"
_SYSTEM = (
    "You are SalCube, SalCube Studio's production observer. You inspect "
    "sanitized video-job, queue, storage, and worker health. Never claim you "
    "changed, restarted, stopped, retried, edited, or contacted anything. "
    "You have no mutation tools or credential access. The host may perform "
    "one deterministic action outside the model: prefer the configured AI33 "
    "backup after explicit credit/quota exhaustion. Separate evidence from "
    "inference. Be concise. You can read the statistics supplied in the snapshot: "
    "job counts, profile summaries, daily average narration characters, recorded "
    "Kie credits, and measured processing times. Respect their period and coverage "
    "notes: local usage is not fleet-wide billing; missing credits are unknown, "
    "not zero. Never infer an account balance from usage. "
    "If evidence suggests a broad or uncertain root cause, recommend a report."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chat_with_fallback(user: str, **kwargs) -> tuple[str, str]:
    """Use OpenRouter Qwen first, then Terra; never cascade to Claude CLI."""
    try:
        return (llm.chat(user, _MODEL, allow_cli_fallback=False, **kwargs),
                _DISPLAY_MODEL)
    except Exception as primary_exc:  # noqa: BLE001
        try:
            return (llm.chat(user, _FALLBACK_MODEL,
                             allow_cli_fallback=False, **kwargs),
                    _FALLBACK_DISPLAY_MODEL)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"OpenRouter Qwen failed: {primary_exc}; "
                f"Terra fallback failed: {fallback_exc}") from fallback_exc


def _chat_json_with_fallback(user: str, **kwargs) -> tuple[dict, str]:
    """JSON equivalent of _chat_with_fallback with the same strict chain."""
    try:
        return (llm.chat_json(user, _MODEL, allow_cli_fallback=False, **kwargs),
                _DISPLAY_MODEL)
    except Exception as primary_exc:  # noqa: BLE001
        try:
            return (llm.chat_json(user, _FALLBACK_MODEL,
                                  allow_cli_fallback=False, **kwargs),
                    _FALLBACK_DISPLAY_MODEL)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"OpenRouter Qwen failed: {primary_exc}; "
                f"Terra fallback failed: {fallback_exc}") from fallback_exc


class OperationsCenter:
    def __init__(self, snapshot_provider: Callable[[], dict], *, interval: int = 300):
        self.snapshot_provider = snapshot_provider
        self.interval = max(60, int(interval))
        self.log_file = config.OUTPUT_DIR / "operations_log.jsonl"
        self.reports_dir = config.OUTPUT_DIR / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._entries: deque[dict] = deque(maxlen=500)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_snapshot: dict = {}
        self._reported_fingerprints: set[str] = set()
        self._load_entries()

    def _load_entries(self) -> None:
        if not self.log_file.exists():
            return
        try:
            lines = self.log_file.read_text().splitlines()[-500:]
            for line in lines:
                row = json.loads(line)
                if isinstance(row, dict):
                    self._entries.append(row)
                    fp = row.get("report_fingerprint")
                    if fp:
                        self._reported_fingerprints.add(str(fp))
        except Exception:  # noqa: BLE001
            pass

    def _append(self, entry: dict) -> dict:
        row = {"at": _now(), **entry}
        with self._lock:
            self._entries.append(row)
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="dmiqwi-operations-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        self.tick()
        while not self._stop.wait(self.interval):
            self.tick()

    @staticmethod
    def _deterministic_health(snapshot: dict) -> dict:
        issues = []
        errors = [j for j in snapshot.get("jobs", []) if j.get("status") == "error"]
        stale = [w for w in snapshot.get("workers", []) if w.get("stale")]
        stalled = [j for j in snapshot.get("jobs", []) if j.get("stalled")]
        disk = snapshot.get("disk", {})
        if errors:
            issues.append(f"{len(errors)} job(s) are in error")
        if stale:
            issues.append(f"{len(stale)} worker(s) are stale")
        if stalled:
            issues.append(f"{len(stalled)} running job(s) may be stalled")
        if disk.get("free_gb", 999) < 10 or disk.get("free_percent", 100) < 10:
            issues.append("main storage is low")
        drive = snapshot.get("drive", {})
        if drive.get("configured") and not drive.get("authorized"):
            issues.append("Google Drive awaits one-time owner authorization")
        severity = "critical" if stalled or (errors and stale) else (
            "warning" if issues else "ok")
        return {"severity": severity, "issues": issues}

    @staticmethod
    def _automatic_report_triggers(snapshot: dict) -> list[str]:
        """Stable, evidence-backed causes that may justify an automatic report.

        Drive awaiting initial OAuth is a setup task, not a root-cause event.
        Restored/done jobs are likewise healthy and never trigger reports.
        """
        triggers = []
        jobs = snapshot.get("jobs", [])
        error_ids = sorted(str(j.get("job_id") or j.get("id") or "unknown")
                           for j in jobs if j.get("status") == "error")
        stalled_ids = sorted(str(j.get("job_id") or j.get("id") or "unknown")
                             for j in jobs if j.get("stalled"))
        stale_workers = sorted(str(w.get("name") or w.get("worker_id")
                                   or w.get("id") or "unknown")
                               for w in snapshot.get("workers", [])
                               if w.get("stale"))
        if error_ids:
            triggers.append("job_errors:" + ",".join(error_ids))
        if stalled_ids:
            triggers.append("stalled_jobs:" + ",".join(stalled_ids))
        if stale_workers:
            triggers.append("stale_workers:" + ",".join(stale_workers))
        disk = snapshot.get("disk", {})
        if disk.get("free_gb", 999) < 10 or disk.get("free_percent", 100) < 10:
            triggers.append("low_main_storage")
        return triggers

    @staticmethod
    def _ai33_credit_evidence(snapshot: dict) -> list[str]:
        """Return job IDs with explicit AI33 credit exhaustion evidence."""
        evidence = []
        for job in snapshot.get("jobs", []):
            texts = [str(job.get("error") or "")]
            texts.extend(str(event.get("message") or "")
                         for event in (job.get("recent_events") or [])
                         if isinstance(event, dict))
            if any(clients.ai33_credit_exhaustion_in_text(text) for text in texts):
                evidence.append(str(job.get("video_id") or job.get("job_id")
                                    or job.get("id") or "unknown"))
        return evidence

    def _maybe_switch_ai33(self, snapshot: dict) -> Optional[dict]:
        """Apply the observer's sole mutation behind a deterministic gate."""
        evidence = self._ai33_credit_evidence(snapshot)
        status = clients.ai33_key_status()
        if (not evidence or not status["backup_configured"]
                or status["active"] == "backup"):
            return None
        new_status = clients.set_ai33_key_mode(
            "backup", "observer detected explicit AI33 credit exhaustion")
        return self._append({
            "kind": "action", "model": "deterministic",
            "severity": "warning",
            "headline": "AI33 switched to backup account",
            "message": ("Explicit credit exhaustion was reported for job(s) "
                        + ", ".join(evidence[:4])
                        + ". New AI33 tasks now use the configured backup; "
                          "existing task polls remain pinned to their creator account."),
            "root_cause_risk": "low", "report_recommended": False,
            "evidence": [f"ai33 credit exhaustion: {jid}"
                         for jid in evidence[:4]],
            "ai33": new_status,
        })

    def tick(self) -> dict:
        try:
            snapshot = self.snapshot_provider()
            self._last_snapshot = snapshot
            self._maybe_switch_ai33(snapshot)
            health = self._deterministic_health(snapshot)
            report_triggers = self._automatic_report_triggers(snapshot)
            prompt = (
                "Return ONLY JSON with keys severity (ok|warning|critical), "
                "headline, message (maximum 70 words), root_cause_risk "
                "(low|medium|high), report_recommended (boolean), and evidence "
                "(array of at most 4 short strings). Do not suggest an action "
                "unless it needs owner approval. Ground rules: `drive` means "
                "Google Drive deliverable OAuth on the main controller, never "
                "a worker or mounted external disk. `authorized: false` means "
                "owner consent is pending, not a system failure. Storage is "
                "healthy unless deterministic health explicitly says it is "
                "low. Done/restored jobs are healthy. Recommend an automatic "
                "root-cause report only when automatic_report_triggers is "
                "non-empty.\n\nDeterministic health:\n"
                + json.dumps(health, ensure_ascii=False)
                + "\n\nAutomatic report triggers:\n"
                + json.dumps(report_triggers, ensure_ascii=False)
                + "\n\nSanitized system snapshot:\n"
                + json.dumps(snapshot, ensure_ascii=False)
            )
            analysis, used_model = _chat_json_with_fallback(
                prompt, system=_SYSTEM, max_tokens=650, attempts=2,
                retries=1, timeout=90)
            severity = str(analysis.get("severity") or health["severity"])
            if severity not in ("ok", "warning", "critical"):
                severity = health["severity"]
            severity_rank = {"ok": 0, "warning": 1, "critical": 2}
            if severity_rank[severity] < severity_rank[health["severity"]]:
                severity = health["severity"]
            report_recommended = (bool(analysis.get("report_recommended"))
                                  and bool(report_triggers))
            entry = self._append({
                "kind": "update", "model": used_model,
                "severity": severity,
                "headline": str(analysis.get("headline") or "System update")[:160],
                "message": str(analysis.get("message") or "Health snapshot completed.")[:1200],
                "root_cause_risk": str(analysis.get("root_cause_risk") or "low"),
                "report_recommended": report_recommended,
                "evidence": [str(x)[:300] for x in (analysis.get("evidence") or [])[:4]],
            })
            if (report_triggers and (entry["severity"] == "critical"
                    or entry["root_cause_risk"] == "high"
                    or entry["report_recommended"])):
                fingerprint = hashlib.sha256(
                    json.dumps(report_triggers, sort_keys=True).encode()
                ).hexdigest()[:20]
                if fingerprint not in self._reported_fingerprints:
                    report = self.create_report(
                        "Automatic report: possible broad root cause", analysis=entry,
                        snapshot=snapshot, use_model=False)
                    self._reported_fingerprints.add(fingerprint)
                    self._append({"kind": "report", "severity": entry["severity"],
                                  "headline": "Root-cause report prepared",
                                  "message": report["summary"],
                                  "report_id": report["report_id"],
                                  "report_fingerprint": fingerprint})
            return entry
        except Exception as exc:  # noqa: BLE001
            snapshot = self._last_snapshot or {}
            health = self._deterministic_health(snapshot)
            return self._append({
                "kind": "update", "model": _DISPLAY_MODEL,
                "severity": "warning" if health["severity"] == "ok" else health["severity"],
                "headline": "AI monitor could not complete its review",
                "message": f"Deterministic monitoring remains active. Qwen and Terra review failed: {str(exc)[:500]}",
                "root_cause_risk": "medium", "report_recommended": False,
                "evidence": health["issues"][:4],
            })

    def chat(self, question: str, *, statistics: Optional[dict] = None) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("question is required")
        snapshot = self.snapshot_provider()
        if statistics is not None:
            snapshot = {**snapshot, "statistics": {k: v for k, v in statistics.items() if k not in ("daily", "filters")}}
        with self._lock:
            recent = list(self._entries)[-12:]
        answer, used_model = _chat_with_fallback(
            "Answer the owner's question using only the supplied sanitized "
            "state. If the answer is not present, say what evidence is missing. "
            "Any proposed change must be labeled 'Needs your approval'.\n\n"
            f"Question: {question}\n\nState: {json.dumps(snapshot, ensure_ascii=False)}"
            f"\n\nRecent monitor updates: {json.dumps(recent, ensure_ascii=False)}",
            system=_SYSTEM, max_tokens=1400, retries=1, timeout=120)
        self._append({"kind": "chat", "severity": "info",
                      "headline": "Owner question", "message": question[:500],
                      "answer": answer[:8000]})
        return {"answer": answer, "model": used_model}

    def create_report(self, reason: str, *, analysis: Optional[dict] = None,
                      snapshot: Optional[dict] = None, use_model: bool = True) -> dict:
        snapshot = snapshot or self.snapshot_provider()
        reason = (reason or "System review").strip()
        if use_model:
            body, used_model = _chat_with_fallback(
                "Prepare a compact Markdown root-cause report. Sections: "
                "Summary, Evidence, Likely causes (clearly mark inference), "
                "Impact, Safe checks, and Proposed changes requiring owner approval. "
                "Do not claim any action was performed.\n\n"
                f"Reason: {reason}\nSnapshot: {json.dumps(snapshot, ensure_ascii=False)}",
                system=_SYSTEM, max_tokens=2400, retries=1, timeout=120)
        else:
            used_model = str((analysis or {}).get("model") or "deterministic")
            evidence = "\n".join(
                f"- {x}" for x in ((analysis or {}).get("evidence") or ["See snapshot below."]))
            body = (
                f"# SalCube root-cause report\n\n## Summary\n\n{reason}. "
                f"{(analysis or {}).get('message', '')}\n\n## Evidence\n\n{evidence}\n\n"
                "## Safety boundary\n\nNo job, worker, setting, credential, or file was "
                "changed. Any corrective change requires owner approval.\n\n"
                "## Sanitized snapshot\n\n```json\n"
                + json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n```\n"
            )
        report_id = identity.numeric_id(
            taken=(p.stem for p in self.reports_dir.glob("*.md")))
        path = self.reports_dir / f"{report_id}.md"
        header = (f"<!-- report_id: {report_id}; created: {_now()}; "
                  f"model: {used_model} -->\n\n")
        path.write_text(header + body, encoding="utf-8")
        return {"report_id": report_id, "summary": reason[:240],
                "url": f"/api/ops/report/{report_id}"}

    def status(self) -> dict:
        with self._lock:
            entries = list(self._entries)[-120:]
        reports = []
        for path in sorted(self.reports_dir.glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            reports.append({"report_id": path.stem,
                            "created_at": datetime.fromtimestamp(
                                path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                            "url": f"/api/ops/report/{path.stem}"})
        return {"name": "SalCube", "running": self.running, "interval_seconds": self.interval,
                "model": _MODEL_CHAIN, "entries": entries,
                "reports": reports, "ai33": clients.ai33_key_status()}


def disk_snapshot() -> dict:
    usage = shutil.disk_usage(config.OUTPUT_DIR)
    return {"total_gb": round(usage.total / 1e9, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "free_percent": round(100 * usage.free / max(1, usage.total), 1)}
