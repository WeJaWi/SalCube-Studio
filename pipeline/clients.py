"""Shared async-job clients for ai33.pro and kie.ai.

Both providers use a submit-then-poll model, so a local app polls for
results (no webhook / public URL required). kie also exposes a file
upload API (3-day retention) used to hand local files to kie models.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from . import config


def _retrying(fn, attempts: int = 4, backoff: float = 5.0):
    """Run fn(), retrying network failures AND rate-limit/server errors
    (429/5xx) — several videos submitting jobs concurrently makes a 429
    more likely, and it must not fail the call outright. Honors a
    Retry-After header on 429 when the provider sends one.

    fn() may return a raw requests.Response (most callers — the 429/5xx
    check applies) OR an already-validated result like download()'s Path
    (raise_for_status() already ran inside fn() itself) — only Response
    objects are inspected for a retryable status code.
    """
    last: Exception = RuntimeError("no attempt made")
    for a in range(attempts):
        try:
            r = fn()
        except requests.RequestException as e:
            last = e
            if a < attempts - 1:
                time.sleep(backoff * (a + 1))
            continue
        if (isinstance(r, requests.Response)
                and (r.status_code == 429 or r.status_code >= 500)):
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if a < attempts - 1:
                wait = backoff * (a + 1)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(min(wait, 60.0))
                continue
        return r
    raise last


def download(url: str, dest: Path) -> Path:
    def _dl():
        # Stream downloads to disk. A provider URL normally points at a tiny
        # stock clip, but buffering a multi-GB response in RAM can kill the
        # render process before its retry logic gets a chance to help.
        r = requests.get(url, timeout=300, stream=True)
        r.raise_for_status()
        with dest.open("wb") as out:
            for block in r.iter_content(chunk_size=1024 * 1024):
                if block:
                    out.write(block)
        return dest
    return _retrying(_dl)


# ───────────────────────── ai33.pro ─────────────────────────────
def _ai33_headers() -> dict:
    return {"xi-api-key": config.AI33_API_KEY}


def ai33_submit_json(path: str, body: dict) -> str:
    """POST a JSON job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    r = _retrying(lambda: requests.post(
        url, headers={**_ai33_headers(), "Content-Type": "application/json"},
        json=body, timeout=60))
    data = _ai33_check(r)
    return data["task_id"]


def ai33_submit_form(path: str, fields: dict, files: Optional[dict] = None) -> str:
    """POST a multipart/form job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    r = _retrying(lambda: requests.post(url, headers=_ai33_headers(),
                                        data=fields, files=files, timeout=120))
    data = _ai33_check(r)
    return data["task_id"]


def _ai33_check(r: requests.Response) -> dict:
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"ai33 non-JSON response {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or not data.get("success", True):
        raise RuntimeError(f"ai33 error {r.status_code}: {data.get('message') or r.text[:300]}")
    return data


def ai33_poll(task_id: str, timeout_s: int = 1200, interval: float = 5.0,
              on_progress=None) -> Dict[str, Any]:
    """Poll GET /v1/task/{id} until done. Returns the full task object."""
    url = f"{config.AI33_BASE_URL}/v1/task/{task_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(url, headers={**_ai33_headers(),
                                           "Content-Type": "application/json"},
                             timeout=60)
        except requests.RequestException:
            time.sleep(interval)  # transient — keep polling
            continue
        try:
            data = r.json()
        except ValueError:  # 502/504 HTML page — transient, keep polling
            time.sleep(interval)
            continue
        status = data.get("status")
        if on_progress:
            on_progress(data.get("progress", 0))
        if status == "done":
            return data
        if status in ("error", "failed"):
            raise RuntimeError(f"ai33 task failed: {data.get('error_message')}")
        time.sleep(interval)
    raise RuntimeError(f"ai33 task {task_id} timed out after {timeout_s}s")


# ───────────────────────── kie.ai ───────────────────────────────
def _kie_headers() -> dict:
    return {"Authorization": f"Bearer {config.KIE_API_KEY}", "Content-Type": "application/json"}


def kie_create_task(model: str, inp: dict, api_key: Optional[str] = None) -> str:
    """POST /api/v1/jobs/createTask, return taskId."""
    url = f"{config.KIE_BASE_URL}/api/v1/jobs/createTask"
    headers = {"Authorization": f"Bearer {api_key or config.KIE_API_KEY}",
               "Content-Type": "application/json"}
    r = _retrying(lambda: requests.post(url, headers=headers,
                                        json={"model": model, "input": inp},
                                        timeout=60))
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"kie createTask error: {data.get('msg')} ({r.status_code})")
    return data["data"]["taskId"]


def kie_poll(task_id: str, timeout_s: int = 600, interval: float = 4.0,
             api_key: Optional[str] = None) -> list:
    """Poll recordInfo until success. Returns list of result URLs."""
    url = f"{config.KIE_BASE_URL}/api/v1/jobs/recordInfo"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {api_key or config.KIE_API_KEY}"},
                             params={"taskId": task_id}, timeout=60)
        except requests.RequestException:
            time.sleep(interval)  # transient — keep polling
            continue
        try:
            data = r.json().get("data") or {}
        except ValueError:  # 502/504 HTML page — transient, keep polling
            time.sleep(interval)
            continue
        state = data.get("state")
        if state == "success":
            result = data.get("resultJson")
            if isinstance(result, str):
                result = json.loads(result)
            return (result or {}).get("resultUrls", [])
        if state == "fail":
            raise RuntimeError(f"kie task failed: {data.get('failMsg')}")
        time.sleep(interval)
    raise RuntimeError(f"kie task {task_id} timed out after {timeout_s}s")


def kie_upload(path: Path, upload_dir: str = "health-studio",
              api_key: Optional[str] = None) -> str:
    """Upload a local file to kie's temp storage (3-day retention).

    Returns a downloadUrl usable as image_url/audio_url in kie model inputs.
    """
    url = f"{config.KIE_UPLOAD_BASE_URL}/api/file-stream-upload"
    with open(path, "rb") as f:
        file_bytes = f.read()
    r = _retrying(lambda: requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key or config.KIE_API_KEY}"},
        data={"uploadPath": upload_dir, "fileName": path.name},
        files={"file": file_bytes},
        timeout=300,
    ))
    data = r.json()
    if not data.get("success") or not (data.get("data") or {}).get("downloadUrl"):
        raise RuntimeError(f"kie upload error {r.status_code}: {data.get('msg') or r.text[:300]}")
    return data["data"]["downloadUrl"]
