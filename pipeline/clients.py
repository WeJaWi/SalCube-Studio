"""Shared async-job clients for ai33.pro and kie.ai.

Both providers use a submit-then-poll model, so a local app polls for
results (no webhook / public URL required). kie also exposes a file
upload API (3-day retention) used to hand local files to kie models.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from . import ai33_tasks, cancel, config, telemetry


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


def download(url: str, dest: Path, *, timeout: int = 300) -> Path:
    """Publish only a complete download; never turn a partial into a cache hit."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _dl():
        cancel.check()
        response = None
        partial = None
        try:
            response = requests.get(url, timeout=timeout, stream=True)
            response.raise_for_status()
            # Each attempt gets its own sibling so concurrent jobs/retries
            # cannot overwrite each other's temporary file. The final rename
            # stays on one filesystem and also works on Windows after close.
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=dest.parent, prefix=f".{dest.name}.",
                    suffix=".part", delete=False) as out:
                partial = Path(out.name)
                received = 0
                for block in response.iter_content(chunk_size=1024 * 1024):
                    cancel.check()
                    if block:
                        out.write(block)
                        received += len(block)
            cancel.check()
            expected = None
            if response.headers.get("Content-Encoding", "identity").lower() == "identity":
                try:
                    expected = int(response.headers.get("Content-Length", ""))
                except (ValueError, TypeError):
                    pass
            if received == 0 or (expected is not None and received != expected):
                raise requests.exceptions.ChunkedEncodingError(
                    f"Incomplete download: received {received} bytes, expected {expected}")
            partial.replace(dest)
            return dest
        finally:
            if response is not None:
                response.close()
            if partial is not None:
                partial.unlink(missing_ok=True)

    return _retrying(_dl)


# ───────────────────────── ai33.pro ─────────────────────────────
class AI33CreditsExhausted(RuntimeError):
    """The selected ai33 account explicitly reported no credits/quota."""


class AI33VoiceNotFound(RuntimeError):
    """A TTS voice is not available to the selected AI33 account.

    This is a definitive rejection: no provider task was created, so trying
    the other configured account is safe.  It deliberately does *not* cover
    generic TTS failures, whose acceptance state could be uncertain.
    """


_AI33_KEY_LOCK = threading.Lock()
_AI33_TASK_KEYS: Dict[str, str] = {}
_AI33_PRIMARY_DEPLETED = False


def _ai33_headers(api_key: Optional[str] = None) -> dict:
    return {"xi-api-key": api_key or config.AI33_API_KEY}


def _ai33_keys() -> list[str]:
    """Configured keys in selection order, without duplicates.

    ``ai33_key_mode`` is live and persisted in settings.json: ``backup``
    immediately prefers the secondary account, ``primary`` explicitly
    prefers the primary, and ``auto`` preserves the original credit-aware
    process-sticky behavior.
    """
    with _AI33_KEY_LOCK:
        depleted = _AI33_PRIMARY_DEPLETED
    primary = (config.AI33_API_KEY or "").strip()
    backup = (config.AI33_API_KEY_BACKUP or "").strip()
    mode = ai33_key_mode()
    if mode == "backup" and backup:
        ordered = [backup, primary]
    elif mode == "primary":
        ordered = [primary, backup]
    else:
        ordered = [backup, primary] if depleted and backup else [primary, backup]
    keys: list[str] = []
    for key in ordered:
        if key and key not in keys:
            keys.append(key)
    return keys


def ai33_key_mode() -> str:
    """Return the persisted account preference without exposing credentials."""
    mode = str(config.load_settings().get("ai33_key_mode") or "auto").strip().lower()
    return mode if mode in ("auto", "primary", "backup") else "auto"


def ai33_key_status() -> dict:
    """Secret-free AI33 account-selection state for the owner/observer."""
    mode = ai33_key_mode()
    primary = (config.AI33_API_KEY or "").strip()
    backup = (config.AI33_API_KEY_BACKUP or "").strip()
    with _AI33_KEY_LOCK:
        depleted = _AI33_PRIMARY_DEPLETED
    active = "backup" if backup and (mode == "backup" or (
        mode == "auto" and depleted)) else "primary"
    if active == "primary" and not primary and backup:
        active = "backup"
    return {
        "mode": mode,
        "active": active,
        "primary_configured": bool(primary),
        "backup_configured": bool(backup),
        "primary_depleted_this_process": bool(depleted),
    }


def set_ai33_key_mode(mode: str, reason: str = "") -> dict:
    """Persist a secret-free account preference and apply it to new tasks.

    Existing provider task IDs remain pinned to the exact account that
    created them, so a live switch cannot strand an in-flight poll.
    """
    global _AI33_PRIMARY_DEPLETED
    mode = str(mode or "").strip().lower()
    if mode not in ("auto", "primary", "backup"):
        raise ValueError("AI33 key mode must be auto, primary, or backup")
    if mode == "backup" and not (config.AI33_API_KEY_BACKUP or "").strip():
        raise RuntimeError("AI33 backup key is not configured")
    config.save_settings({
        "ai33_key_mode": mode,
        "ai33_key_mode_updated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "ai33_key_mode_reason": str(reason or "owner request")[:240],
    })
    if mode in ("auto", "primary"):
        with _AI33_KEY_LOCK:
            _AI33_PRIMARY_DEPLETED = False
    return ai33_key_status()


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


def ai33_credit_exhaustion_in_text(message: str) -> bool:
    """Public, credential-free detector used by the operations observer."""
    return _ai33_credit_error(None, None, message)


def _mark_ai33_primary_depleted(api_key: str) -> None:
    global _AI33_PRIMARY_DEPLETED
    primary = (config.AI33_API_KEY or "").strip()
    backup = (config.AI33_API_KEY_BACKUP or "").strip()
    if api_key == primary and backup and backup != primary:
        with _AI33_KEY_LOCK:
            _AI33_PRIMARY_DEPLETED = True


def _remember_ai33_task(task_id: str, api_key: str) -> None:
    ai33_tasks.remember(task_id, api_key)
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
            cancel.check()
            try:
                response = request_fn(api_key)
            except requests.RequestException as exc:
                raise ai33_tasks.SubmissionUncertain("AI33 acceptance is unknown after a network failure; submission was not replayed") from exc
            if response.status_code >= 500:
                raise ai33_tasks.SubmissionUncertain("AI33 returned a server error; acceptance is unknown")
            try:
                response.json()
            except ValueError as exc:
                raise ai33_tasks.SubmissionUncertain("AI33 returned an unreadable response; acceptance is unknown") from exc
            data = _ai33_check(response)
        except AI33CreditsExhausted:
            _mark_ai33_primary_depleted(api_key)
            if index + 1 >= len(keys):
                raise
            print("[ai33] selected account credits exhausted — switching account")
            continue
        except AI33VoiceNotFound:
            if index + 1 >= len(keys):
                raise
            print("[ai33] selected account cannot access this ElevenLabs voice — switching account")
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
            print("[ai33] selected account credits exhausted — switching account")
            continue
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise ai33_tasks.SubmissionUncertain("AI33 response omitted the task ID; acceptance is unknown")
        try:
            _remember_ai33_task(task_id, api_key)
        except OSError as exc:
            raise ai33_tasks.SubmissionUncertain("AI33 accepted the task but its account record could not be saved") from exc
        return task_id
    raise AI33CreditsExhausted(
        "ai33 credits are exhausted for every configured key")


def ai33_submit_json(path: str, body: dict, *, ledger_path: Optional[Path] = None) -> str:
    """POST a JSON job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    if ledger_path is not None:
        return ai33_tasks.submit(ledger_path, body,
            lambda: ai33_submit_json(path, body), kind=path)
    return _ai33_submit(lambda api_key: requests.post(
        url, headers={**_ai33_headers(api_key),
                      "Content-Type": "application/json"},
        json=body, timeout=60))


def ai33_submit_form(path: str, fields: dict, files: Optional[dict] = None, *,
                     ledger_path: Optional[Path] = None) -> str:
    """POST a multipart/form job to ai33, return task_id."""
    url = f"{config.AI33_BASE_URL}{path}"
    if ledger_path is not None:
        return ai33_tasks.submit(ledger_path, fields,
            lambda: ai33_submit_form(path, fields, files), kind=path)
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
        # AI33 returns this exact provider code before creating a TTS task.
        # It happens when a cloned/custom ElevenLabs voice belongs to the
        # other configured account.  Restrict account failover to this known,
        # deterministic rejection so network/server failures are never replayed.
        if "elevenlabs_voice_not_found" in message.casefold():
            raise AI33VoiceNotFound("ai33 ElevenLabs voice is not available to this account")
        raise RuntimeError(f"ai33 error {r.status_code}: {message}")
    return data


def ai33_poll(task_id: str, timeout_s: int = 1200, interval: float = 5.0,
              on_progress=None) -> Dict[str, Any]:
    """Poll GET /v1/task/{id} until done. Returns the full task object."""
    url = f"{config.AI33_BASE_URL}/v1/task/{task_id}"
    with _AI33_KEY_LOCK:
        task_key = _AI33_TASK_KEYS.get(task_id)
    if not task_key:
        task_key = ai33_tasks.key_for(task_id)
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
            ai33_tasks.complete(task_id, data)
            _forget_ai33_task(task_id)
            return data
        if status in ("error", "failed"):
            message = str(data.get("error_message") or data.get("message") or "")
            _forget_ai33_task(task_id)
            if _ai33_credit_error(r.status_code, data.get("code"), message):
                _mark_ai33_primary_depleted(task_key)
                raise AI33CreditsExhausted(
                    "ai33 task failed because account credits are exhausted")
            if "elevenlabs_voice_not_found" in message.casefold():
                raise AI33VoiceNotFound(
                    "ai33 ElevenLabs voice is not available to this account")
            raise RuntimeError(f"ai33 task failed: {message}")
        cancel.sleep(interval)
    _forget_ai33_task(task_id)
    raise RuntimeError(f"ai33 task {task_id} timed out after {timeout_s}s")


# ───────────────────────── kie.ai ───────────────────────────────
class KieSubmissionUncertain(RuntimeError):
    """Kie may have accepted a createTask request whose response was lost.

    createTask is billable and Kie does not document an idempotency key, so
    callers must not blindly replay this failure.  The orchestrator persists
    an intent before submission and will use a free fallback on resume.
    """


class KieTaskFailed(RuntimeError):
    """A Kie task reached the provider's terminal failure state."""


class KieTaskTimeout(RuntimeError):
    """A Kie task is still non-terminal after the local polling window."""


_KIE_AUDIT_LOCK = threading.Lock()
_KIE_TASK_META: Dict[str, Dict[str, str]] = {}
_KIE_TERMINAL_TASKS: set[str] = set()
_KIE_TERMINAL_LOG_PATH = ""


def _kie_key_label(api_key: Optional[str], requested: Optional[str] = None) -> str:
    """Return a secret-free account label for usage records."""
    if requested:
        return requested
    key = api_key or config.KIE_API_KEY
    candidates = (
        ("thumbnail", config.KIE_THUMBNAIL_API_KEY),
        ("video", config.KIE_VIDEO_API_KEY),
        ("main", config.KIE_API_KEY),
        ("script-gpt", config.LLM_GPT_API_KEY),
        ("script-grok", config.LLM_GROK_API_KEY),
    )
    labels = [label for label, known in candidates if key and known and key == known]
    return "+".join(labels) if labels else "custom"


def _kie_credits(data: Dict[str, Any]) -> Any:
    """Extract the task-detail credit field without guessing a price."""
    for key in ("creditsConsumed", "credits_consumed"):
        if key in data:
            return data.get(key)
    return None


def _record_kie_event(event: str, *, task_id: str = "", model: str = "",
                      api_key: Optional[str] = None, key_label: str = "",
                      context: str = "", credits: Any = None,
                      message: str = "") -> None:
    """Append a credential-free Kie billing/audit event as JSONL."""
    row: Dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "task_id": task_id,
        "model": model,
        "account": _kie_key_label(api_key, key_label),
    }
    if context:
        row["context"] = str(context)[:160]
    if credits is not None:
        row["credits_consumed"] = credits
    if message:
        row["message"] = str(message)[:300]
    global _KIE_TERMINAL_LOG_PATH
    path = config.OUTPUT_DIR / "kie_usage.jsonl"
    try:
        with _KIE_AUDIT_LOCK:
            path_key = str(path.resolve())
            if path_key != _KIE_TERMINAL_LOG_PATH:
                _KIE_TERMINAL_TASKS.clear()
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            prior = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if (prior.get("event") in ("succeeded", "failed")
                                and prior.get("task_id")):
                            _KIE_TERMINAL_TASKS.add(str(prior["task_id"]))
                _KIE_TERMINAL_LOG_PATH = path_key
            if (event in ("succeeded", "failed") and task_id
                    and task_id in _KIE_TERMINAL_TASKS):
                return
            with path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            if event in ("succeeded", "failed") and task_id:
                _KIE_TERMINAL_TASKS.add(task_id)
    except OSError:
        # Audit logging must never turn a completed paid task into a retry.
        pass


def kie_stop_error_in_text(message: str) -> bool:
    """Whether more paid submissions should stop for the rest of a job."""
    text = str(message or "").casefold()
    markers = (
        "insufficient credit", "credits insufficient", "credit exhausted",
        "out of credit", "insufficient balance", "payment required",
        "invalid api key", "unauthorized", "forbidden", "authentication",
        "status 401", "status 403", "http 401", "http 403",
    )
    return any(marker in text for marker in markers)


def _kie_headers() -> dict:
    return {"Authorization": f"Bearer {config.KIE_API_KEY}", "Content-Type": "application/json"}


def kie_create_task(model: str, inp: dict, api_key: Optional[str] = None,
                    *, context: str = "", key_label: str = "") -> str:
    """POST /api/v1/jobs/createTask once and return its task ID.

    Unlike downloads and read-only polling, this billable POST is deliberately
    not wrapped in ``_retrying``.  A read timeout can happen after the server
    accepted the task; replaying it would create a second charged task.
    """
    url = f"{config.KIE_BASE_URL}/api/v1/jobs/createTask"
    selected_key = api_key or config.KIE_API_KEY
    headers = {"Authorization": f"Bearer {selected_key}",
               "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers,
                          json={"model": model, "input": inp}, timeout=60)
    except requests.RequestException as exc:
        _record_kie_event(
            "submission_uncertain", model=model, api_key=selected_key,
            key_label=key_label, context=context, message=str(exc))
        raise KieSubmissionUncertain(
            "Kie submission outcome is unknown after a network failure; "
            "it was not retried to avoid a duplicate charged task") from exc
    try:
        data = r.json()
    except ValueError as exc:
        _record_kie_event(
            "submission_uncertain", model=model, api_key=selected_key,
            key_label=key_label, context=context,
            message=f"non-JSON HTTP {r.status_code}")
        raise KieSubmissionUncertain(
            "Kie submission returned an unreadable response; it was not "
            "retried to avoid a duplicate charged task") from exc
    if r.status_code >= 400 or data.get("code") != 200:
        message = str(data.get("msg") or data.get("message") or r.text[:300])
        _record_kie_event(
            "submission_rejected", model=model, api_key=selected_key,
            key_label=key_label, context=context, message=message)
        raise RuntimeError(f"kie createTask error: {message} ({r.status_code})")
    task_id = str((data.get("data") or {}).get("taskId") or "").strip()
    if not task_id:
        _record_kie_event(
            "submission_uncertain", model=model, api_key=selected_key,
            key_label=key_label, context=context, message="response omitted taskId")
        raise KieSubmissionUncertain(
            "Kie accepted a submission but returned no task ID; it was not "
            "retried to avoid a duplicate charged task")
    with _KIE_AUDIT_LOCK:
        _KIE_TASK_META[task_id] = {
            "model": model,
            "account": _kie_key_label(selected_key, key_label),
            "context": str(context or "")[:160],
        }
    _record_kie_event(
        "submitted", task_id=task_id, model=model, api_key=selected_key,
        key_label=key_label, context=context)
    return task_id


def kie_poll(task_id: str, timeout_s: int = 600, interval: float = 4.0,
             api_key: Optional[str] = None, *, model: str = "",
             context: str = "", key_label: str = "") -> list:
    """Poll recordInfo until success. Returns list of result URLs."""
    url = f"{config.KIE_BASE_URL}/api/v1/jobs/recordInfo"
    selected_key = api_key or config.KIE_API_KEY
    with _KIE_AUDIT_LOCK:
        meta = dict(_KIE_TASK_META.get(task_id) or {})
    model = model or meta.get("model", "")
    context = context or meta.get("context", "")
    key_label = key_label or meta.get("account", "")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {selected_key}"},
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
            _record_kie_event(
                "succeeded", task_id=task_id, model=model,
                api_key=selected_key, key_label=key_label, context=context,
                credits=_kie_credits(data))
            result = data.get("resultJson")
            if isinstance(result, str):
                result = json.loads(result)
            with _KIE_AUDIT_LOCK:
                _KIE_TASK_META.pop(task_id, None)
            return (result or {}).get("resultUrls", [])
        if state == "fail":
            message = str(data.get("failMsg") or "unknown provider failure")
            _record_kie_event(
                "failed", task_id=task_id, model=model,
                api_key=selected_key, key_label=key_label, context=context,
                credits=_kie_credits(data), message=message)
            with _KIE_AUDIT_LOCK:
                _KIE_TASK_META.pop(task_id, None)
            raise KieTaskFailed(f"kie task failed: {message}")
        cancel.sleep(interval)
    _record_kie_event(
        "poll_timeout", task_id=task_id, model=model, api_key=selected_key,
        key_label=key_label, context=context)
    raise KieTaskTimeout(
        f"kie task {task_id} timed out after {timeout_s}s; task ID was "
        "retained and will be polled again on resume")


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
