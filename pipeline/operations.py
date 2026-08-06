"""Read-only AI operations center for the local render fleet.

The model never receives mutation tools. It can summarize sanitized state,
answer questions about that state, and write reports for the owner to export.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from . import config, identity, llm


_MODEL = "openrouter-monitor"
_DISPLAY_MODEL = config.OPENROUTER_MONITOR_MODEL
_SYSTEM = (
    "You are Dmiqwi Operations, a read-only production observer. You inspect "
    "sanitized video-job, queue, storage, and worker health. Never claim you "
    "changed, restarted, stopped, retried, edited, or contacted anything. "
    "You have no mutation tools. Separate evidence from inference. Be concise. "
    "If evidence suggests a broad or uncertain root cause, recommend a report."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperationsCenter:
    def __init__(self, snapshot_provider: Callable[[], dict], *, interval: int = 60):
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
        severity = "critical" if stalled or (errors and stale) else (
            "warning" if issues else "ok")
        return {"severity": severity, "issues": issues}

    def tick(self) -> dict:
        try:
            snapshot = self.snapshot_provider()
            self._last_snapshot = snapshot
            health = self._deterministic_health(snapshot)
            prompt = (
                "Return ONLY JSON with keys severity (ok|warning|critical), "
                "headline, message (maximum 70 words), root_cause_risk "
                "(low|medium|high), report_recommended (boolean), and evidence "
                "(array of at most 4 short strings). Do not suggest an action "
                "unless it needs owner approval.\n\nDeterministic health:\n"
                + json.dumps(health, ensure_ascii=False)
                + "\n\nSanitized system snapshot:\n"
                + json.dumps(snapshot, ensure_ascii=False)
            )
            analysis = llm.chat_json(prompt, _MODEL, system=_SYSTEM,
                                     max_tokens=650, attempts=2, retries=1,
                                     timeout=90, allow_cli_fallback=False)
            severity = str(analysis.get("severity") or health["severity"])
            if severity not in ("ok", "warning", "critical"):
                severity = health["severity"]
            entry = self._append({
                "kind": "update", "model": _DISPLAY_MODEL,
                "severity": severity,
                "headline": str(analysis.get("headline") or "System update")[:160],
                "message": str(analysis.get("message") or "Health snapshot completed.")[:1200],
                "root_cause_risk": str(analysis.get("root_cause_risk") or "low"),
                "report_recommended": bool(analysis.get("report_recommended")),
                "evidence": [str(x)[:300] for x in (analysis.get("evidence") or [])[:4]],
            })
            if (entry["severity"] == "critical"
                    or entry["root_cause_risk"] == "high"
                    or entry["report_recommended"]):
                fingerprint = hashlib.sha256(json.dumps({
                    "severity": entry["severity"], "headline": entry["headline"],
                    "evidence": entry["evidence"],
                }, sort_keys=True).encode()).hexdigest()[:20]
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
                "message": f"Deterministic monitoring remains active. OpenRouter review failed: {str(exc)[:500]}",
                "root_cause_risk": "medium", "report_recommended": False,
                "evidence": health["issues"][:4],
            })

    def chat(self, question: str) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("question is required")
        snapshot = self.snapshot_provider()
        with self._lock:
            recent = list(self._entries)[-12:]
        answer = llm.chat(
            "Answer the owner's question using only the supplied sanitized "
            "state. If the answer is not present, say what evidence is missing. "
            "Any proposed change must be labeled 'Needs your approval'.\n\n"
            f"Question: {question}\n\nState: {json.dumps(snapshot, ensure_ascii=False)}"
            f"\n\nRecent monitor updates: {json.dumps(recent, ensure_ascii=False)}",
            _MODEL, system=_SYSTEM, max_tokens=1400, retries=1, timeout=120,
            allow_cli_fallback=False)
        self._append({"kind": "chat", "severity": "info",
                      "headline": "Owner question", "message": question[:500],
                      "answer": answer[:8000]})
        return {"answer": answer, "model": _DISPLAY_MODEL}

    def create_report(self, reason: str, *, analysis: Optional[dict] = None,
                      snapshot: Optional[dict] = None, use_model: bool = True) -> dict:
        snapshot = snapshot or self.snapshot_provider()
        reason = (reason or "System review").strip()
        if use_model:
            body = llm.chat(
                "Prepare a compact Markdown root-cause report. Sections: "
                "Summary, Evidence, Likely causes (clearly mark inference), "
                "Impact, Safe checks, and Proposed changes requiring owner approval. "
                "Do not claim any action was performed.\n\n"
                f"Reason: {reason}\nSnapshot: {json.dumps(snapshot, ensure_ascii=False)}",
                _MODEL, system=_SYSTEM, max_tokens=2400, retries=1, timeout=120,
                allow_cli_fallback=False)
        else:
            evidence = "\n".join(
                f"- {x}" for x in ((analysis or {}).get("evidence") or ["See snapshot below."]))
            body = (
                f"# Dmiqwi root-cause report\n\n## Summary\n\n{reason}. "
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
                  f"model: {_DISPLAY_MODEL} -->\n\n")
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
        return {"running": self.running, "interval_seconds": self.interval,
                "model": _DISPLAY_MODEL, "entries": entries,
                "reports": reports}


def disk_snapshot() -> dict:
    usage = shutil.disk_usage(config.OUTPUT_DIR)
    return {"total_gb": round(usage.total / 1e9, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "free_percent": round(100 * usage.free / max(1, usage.total), 1)}
