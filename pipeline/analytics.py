"""Sanitized dashboard aggregates. Recorded usage is not an invoice estimate."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from . import ai33_tasks, config


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else 0
    except (TypeError, ValueError):
        return 0


def date_of(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (TypeError, ValueError):
        return None


def kie_tasks():
    """Merge submitted/terminal events, preserving the original submission day."""
    rows = {}
    try:
        with (config.OUTPUT_DIR / "kie_usage.jsonl").open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    key = event.get("task_id") or f"unknown-{index}"
                    old = rows.get(key, {})
                    rows[key] = {**old, **event, "at": old.get("at", event.get("at"))}
                    if event.get("credits_consumed") is None and "credits_consumed" in old:
                        rows[key]["credits_consumed"] = old["credits_consumed"]
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return list(rows.values())


def snapshot(jobs, *, days=30, profile=None, allowed_profiles=None, now=None):
    """Jobs are grouped by submission date; usage by original request date (UTC).

    Worker billing ledgers are not transferred to the controller. Expose that
    limitation rather than presenting the controller's recorded spend as fleet
    spend. Profile-restricted users never see unattributed billing records.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    def allowed(name):
        return (allowed_profiles is None or name in allowed_profiles) and (not profile or name == profile)
    def in_window(value):
        day = date_of(value)
        return day if day is not None and start <= day <= today else None

    by_id = {str(j["job_id"]): j for j in jobs}
    visible = [j for j in jobs if allowed(j.get("profile")) and j.get("status") != "cancelled"]
    selected = [j for j in visible if in_window(j.get("created_at"))]
    daily = {(start + timedelta(days=i)).isoformat(): {"date": (start + timedelta(days=i)).isoformat(),
             "jobs": 0, "characters": 0} for i in range(days)}
    profiles = {}
    def bucket(name):
        return profiles.setdefault(name or "unattributed", {"profile": name or "unattributed", "jobs": 0,
            "done": 0, "error": 0, "characters": 0, "retrying": 0, "render_seconds": []})
    statuses = Counter()
    stages = {}
    render_samples = []
    for job in selected:
        state = str(job.get("status") or "unknown")
        statuses[state] += 1
        group = bucket(job.get("profile"))
        group["jobs"] += 1
        group["done"] += state == "done"
        group["error"] += state == "error"
        group["retrying"] += state == "retry_wait"
        daily[in_window(job.get("created_at")).isoformat()]["jobs"] += 1
        perf = job.get("performance") or {}
        if not isinstance(perf, dict):
            continue
        stage_data = perf.get("stages") or {}
        if not isinstance(stage_data, dict):
            continue
        for stage, sample in stage_data.items():
            if isinstance(sample, dict):
                stages[stage] = stages.get(stage, 0) + number(sample.get("seconds"))
        render_stage = stage_data.get("video_render") or {}
        duration = number(render_stage.get("seconds")) if isinstance(render_stage, dict) else 0
        if state == "done" and duration:
            render_samples.append(duration)
            group["render_seconds"].append(duration)

    characters = pending = tasks = rejected = 0
    for row in ai33_tasks.usage():
        name = row.get("profile") or by_id.get(str(row.get("job_id")), {}).get("profile")
        day = in_window(row.get("at"))
        if not day or not allowed(name):
            continue
        amount = int(number(row.get("characters")))
        if row.get("event") in ("submitted", "succeeded"):
            characters += amount
            tasks += 1
            bucket(name)["characters"] += amount
            daily[day.isoformat()]["characters"] += amount
        elif row.get("event") == "reserved":
            pending += amount
        elif row.get("event") == "rejected":
            rejected += 1

    kie_count = reported = 0
    credits = 0.0
    for row in kie_tasks():
        jid = str(row.get("context") or "").split(":", 1)[0]
        name = by_id.get(jid, {}).get("profile")
        if not in_window(row.get("at")) or not allowed(name):
            continue
        kie_count += 1
        value = row.get("credits_consumed")
        try:
            known = value is not None and math.isfinite(float(value)) and float(value) >= 0
        except (ValueError, TypeError):
            known = False
        if known:
            reported += 1
            credits += float(value)

    for group in profiles.values():
        measured = group.pop("render_seconds")
        group["median_render_seconds"] = round(median(measured), 1) if measured else None
        group["render_samples"] = len(measured)
        group["daily_average_characters"] = round(group["characters"] / days)
    return {
        "period": {"days": days, "from": start.isoformat(), "to": today.isoformat(), "timezone": "UTC"},
        "filters": {"profile": profile, "profiles": sorted({str(j.get("profile")) for j in jobs
                    if j.get("profile") and (allowed_profiles is None or j.get("profile") in allowed_profiles)})},
        "jobs": {"total": len(selected), "statuses": dict(statuses), "current": dict(Counter(j.get("status") for j in visible)),
                 "median_render_seconds": round(median(render_samples), 1) if render_samples else None,
                 "render_samples": len(render_samples)},
        "ai33": {"submitted_characters": characters, "daily_average_characters": round(characters / days),
                 "pending_characters": pending, "tasks": tasks, "rejected_requests": rejected},
        "kie": {"tasks": kie_count, "reported_credits": round(credits, 3) if reported else None,
                "tasks_with_reported_credits": reported},
        "daily": list(daily.values()),
        "profiles": sorted(profiles.values(), key=lambda row: (-row["jobs"], row["profile"])),
        "stages": [{"stage": k, "seconds": round(v, 1)} for k, v in sorted(stages.items(), key=lambda item: -item[1])],
        "notes": ["Job counts use submission date; statuses reflect their current state.",
                  "Usage covers records on this machine only; other workers' billing and deleted history may be missing.",
                  "Kie credits include only provider-reported amounts, not estimates or the account balance.",
                  "Daily averages include every calendar day in the selected period (UTC).",
                  "Stage durations overlap for nested or parallel work. Render samples describe the latest recorded attempt."],
    }
