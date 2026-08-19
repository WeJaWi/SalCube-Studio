"""Shared async-job clients for ai33.pro and kie.ai.

Both providers use a submit-then-poll model, so a local app polls for
results (no webhook / public URL required). kie also exposes a file
upload API (3-day retention) used to hand local files to kie models.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from . import cancel, config


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
                cancel.sleep(backoff * (a + 1))
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
                cancel.sleep(min(wait, 60.0))
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
class AI33CreditsExhausted(RuntimeError):
    """The selected ai33 account explicitly reported no credits/quota."""


_AI33_KEY_LOCK = threading.Lock()
_AI33_TASK_KEYS: Dict[str, str] = {}
_AI33_PRIMARY_DEPLETED = False


def _ai33_headers(api_key: Optional[str] = None) -> dict:
    return {"xi-api-key": api_key or config.AI33_API_KEY}


def _ai33_keys() -> list[str]:
    """Configured keys in selection order, without duplicates.

    Once the primary explicitly reports exhausted credits, keep new work on
    the backup for the life of this process. A restart probes the primary
    again, which is useful after its balance has been topped up.
    """
    with _AI33_KEY_LOCK:
        depleted = _AI33_PRIMARY_DEPLETED
    primary = (config.AI33_API_KEY or "").strip()
    backup = (config.AI33_API_KEY_BACKUP or "").strip()
    ordered = ([backup] if depleted and backup else [primary, backup])
    keys: list[str] = []
    for key in ordered:
        if key and key not in keys:
            keys.append(key)
    return keys


def _ai33_credit_error(status_code: Optional[int], provider_code: Any,
                       message: str) -> bool:
    try:
        code = int(provider_code)
    except (TypeError, ValueError):
        code = None
    if status_code == 402 or code == 402:
        return True
    text = (message or "").casefold()
    direct = (
        "insufficient credits", "insufficient credit", "credits exhausted",
        "credit exhausted", "out of credits", "out of credit",
        "insufficient balance", "balance insufficient", "payment required",
        "quota exhausted", "quota exceeded", "insufficient quota",
    )
    return any(marker in text for marker in direct)


def _mark_ai33_primary_depleted(api_key: str) -> None:
    global _AI33_PRIMARY_DEPLETED
    primary = (config.AI33_API_KEY or "").strip()
    backup = (config.AI33_API_KEY_BACKUP or "").strip()
    if api_key == primary and backup and backup != primary:
        with _AI33_KEY_LOCK:
            _AI33_PRIMARY_DEPLETED = True


def _remember_ai33_task(task_id: str, api_key: str) -> None:
    with _AI33_KEY_LOCK:
        _AI33_TASK_KEYS[task_id] = api_key


def _forget_ai33_task(task_id: str) -> None:
    with _AI33_KEY_LOCK:
        _AI33_TASK_KEYS.pop(task_id, None)


def _ai33_submit(request_fn) -> str:
    keys = _ai33_keys()
    if not keys:
        raise RuntimeError("ai33 API key is not configured")
    for index, api_key in enumerate(keys):
        try:
            response = _retrying(lambda: request_fn(api_key))
            data = _ai33_check(response)
        except AI33CreditsExhausted:
            _mark_ai33_primary_depleted(api_key)
            if index + 1 >= len(keys):
                raise
            print("[ai33] primary credits exhausted — switching to backup key")
            continue
        except RuntimeError as exc:
            # A final 429 from _retrying is raised before _ai33_check sees the
            # response. Only treat it as a billing failover when its body says
            # credits/quota; an ordinary rate limit stays on the primary.
            if not _ai33_credit_error(None, None, str(exc)):
                raise
            _mark_ai33_primary_depleted(api_key)
            if index + 1 >= len(keys):
                raise AI33CreditsExhausted(
                    "ai33 credits are exhausted for every configured key") from exc
            print("[ai33] primary credits exhausted — switching to backup key")
            continue
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError("ai33 response did not include a task_id")
        _remember_ai33_task(task_id, api_key)
        return task_id
    raise AI33CreditsExhausted(
        "ai33 credits are exhausted for every configured key")


def ai33_submit_json(path: str, body: dict) -> str:
    """POST a JSON job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    return _ai33_submit(lambda api_key: requests.post(
        url, headers={**_ai33_headers(api_key),
                      "Content-Type": "application/json"},
        json=body, timeout=60))


def ai33_submit_form(path: str, fields: dict, files: Optional[dict] = None) -> str:
    """POST a multipart/form job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    return _ai33_submit(lambda api_key: requests.post(
        url, headers=_ai33_headers(api_key), data=fields, files=files,
        timeout=120))


def _ai33_check(r: requests.Response) -> dict:
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"ai33 non-JSON response {r.status_code}: {r.text[:300]}")
    message = str(data.get("message") or data.get("error_message")
                  or r.text[:300])
    provider_code = data.get("code")
    try:
        provider_code_int = int(provider_code)
    except (TypeError, ValueError):
        provider_code_int = None
    bad_provider_code = (provider_code_int is not None
                         and provider_code_int not in (0, 200))
    if r.status_code >= 400 or not data.get("success", True) or bad_provider_code:
        if _ai33_credit_error(r.status_code, provider_code, message):
            raise AI33CreditsExhausted("ai33 account credits are exhausted")
        raise RuntimeError(f"ai33 error {r.status_code}: {message}")
    return data


def ai33_poll(task_id: str, timeout_s: int = 1200, interval: float = 5.0,
              on_progress=None) -> Dict[str, Any]:
    """Poll GET /v1/task/{id} until done. Returns the full task object."""
    url = f"{config.AI33_BASE_URL}/v1/task/{task_id}"
    with _AI33_KEY_LOCK:
        task_key = _AI33_TASK_KEYS.get(task_id)
    if not task_key:
        keys = _ai33_keys()
        if not keys:
            raise RuntimeError("ai33 API key is not configured")
        task_key = keys[0]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(url, headers={**_ai33_headers(task_key),
                                           "Content-Type": "application/json"},
                             timeout=60)
        except requests.RequestException:
            cancel.sleep(interval)  # transient — keep polling
            continue
        try:
            data = r.json()
        except ValueError:  # 502/504 HTML page — transient, keep polling
            cancel.sleep(interval)
            continue
        message = str(data.get("message") or data.get("error_message") or "")
        if r.status_code == 429 or r.status_code >= 500:
            if _ai33_credit_error(r.status_code, data.get("code"), message):
                _mark_ai33_primary_depleted(task_key)
                _forget_ai33_task(task_id)
                raise AI33CreditsExhausted(
                    "ai33 task polling reported exhausted credits")
            cancel.sleep(interval)
            continue
        if r.status_code >= 400 or not data.get("success", True):
            try:
                _ai33_check(r)
            except AI33CreditsExhausted:
                _mark_ai33_primary_depleted(task_key)
                _forget_ai33_task(task_id)
                raise
            except Exception:
                _forget_ai33_task(task_id)
                raise
        status = data.get("status")
        if on_progress:
            on_progress(data.get("progress", 0))
        if status == "done":
            _forget_ai33_task(task_id)
            return data
        if status in ("error", "failed"):
            message = str(data.get("error_message") or data.get("message") or "")
            _forget_ai33_task(task_id)
            if _ai33_credit_error(r.status_code, data.get("code"), message):
                _mark_ai33_primary_depleted(task_key)
                raise AI33CreditsExhausted(
                    "ai33 task failed because account credits are exhausted")
            raise RuntimeError(f"ai33 task failed: {message}")
        cancel.sleep(interval)
    _forget_ai33_task(task_id)
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
            cancel.sleep(interval)  # transient — keep polling
            continue
        try:
            body = r.json()
        except ValueError:  # 502/504 HTML page — transient, keep polling
            cancel.sleep(interval)
            continue
        if (r.status_code >= 400
                or (isinstance(body.get("code"), int)
                    and body["code"] not in (0, 200))):
            raise RuntimeError(
                f"kie poll error {r.status_code}: "
                f"{body.get('msg') or body.get('message') or r.text[:300]}")
        data = body.get("data") or {}
        state = data.get("state")
        if state == "success":
            result = data.get("resultJson")
            if isinstance(result, str):
                result = json.loads(result)
            return (result or {}).get("resultUrls", [])
        if state == "fail":
            raise RuntimeError(f"kie task failed: {data.get('failMsg')}")
        cancel.sleep(interval)
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
