"""Script-model client: Claude Sonnet 5 (Anthropic format) or GPT 5.6
(terra/luna/sol, OpenAI Responses format), both via kie.ai.

The model is resolved per video with resolve_model() and passed explicitly
into every chat()/chat_json() call — it must NOT be module-global state,
since multiple videos can render concurrently on different script models.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Dict, Optional

import requests

from . import cancel, config

_WRITER_INSTRUCTIONS = ("You are a world-class long-form scriptwriter and "
                        "video planner. Follow the user's instructions "
                        "exactly and completely.")


def resolve_model(name: Optional[str]) -> str:
    """Validate a requested script model, falling back to sonnet-5."""
    return name if name in config.SCRIPT_MODELS else "sonnet-5"


def _payload(spec: Dict[str, str], content: str, max_tokens: int) -> dict:
    if spec["format"] == "responses":
        return {
            "model": spec["model"],
            "instructions": _WRITER_INSTRUCTIONS,
            "input": [{"role": "user",
                       "content": [{"type": "input_text", "text": content}]}],
            # reasoning tokens count against the output cap — keep effort low
            # (writing, not math) and pad the cap so long JSON never truncates
            "max_output_tokens": max_tokens + 4000,
            "reasoning": {"effort": "low"},
            "stream": False,
        }
    if spec["format"] == "openai":
        payload = {
            "model": spec["model"],
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "stream": False,
        }
        if spec.get("reasoning"):
            payload["reasoning"] = {
                "enabled": True,
                "max_tokens": int(spec.get("reasoning_max_tokens") or 256),
                "exclude": True,
            }
        return payload
    return {  # anthropic
        "model": spec["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "stream": False,
    }


def _parse(spec: Dict[str, str], data: dict) -> str:
    if spec["format"] == "responses":
        parts = []
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
        return "".join(parts).strip()
    if spec["format"] == "openai":
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content")
                   if choices else "")
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict)
            ).strip()
        return str(content or "").strip()
    return "".join(
        b.get("text", "") for b in data.get("content", []) or []
        if b.get("type") == "text"
    ).strip()


def chat(user: str, model: str, system: Optional[str] = None,
         max_tokens: int = 8000, timeout: int = 300, retries: int = 5,
         allow_cli_fallback: bool = True) -> str:
    """Send one message to `model` (from resolve_model()), return its text.

    Transient 5xx/network/empty responses are retried with exponential
    backoff — long scripts make dozens of calls and must not die on one
    provider hiccup (kie.ai occasionally returns
    ``api_error: Server exception, please try again later``).
    """
    spec = config.SCRIPT_MODELS[model]

    if spec.get("format") == "cli":
        # Primary generation via the local Claude CLI — no kie.ai HTTP call
        # at all, so a kie outage never even touches this path.
        return _chat_via_cli(user, system)

    if not spec.get("key") or not spec.get("base"):
        raise RuntimeError(f"script model '{model}' is not configured "
                           f"(missing key/base url)")

    content = f"{system.strip()}\n\n{user}" if system else user
    url = f"{spec['base']}{spec['path']}"
    payload = _payload(spec, content, max_tokens)

    last_err: Exception = RuntimeError("LLM: no attempt made")
    for attempt in range(retries + 1):
        is_5xx = False
        try:
            r = requests.post(url, headers={
                "Authorization": f"Bearer {spec['key']}",
                "Content-Type": "application/json",
            }, json=payload, timeout=timeout)
            if r.status_code == 200:
                # kie.ai sometimes wraps a real, non-transient error in an
                # HTTP 200 body instead of a proper error status — e.g.
                # {"code": 401, "msg": "The API key is not authorized to
                # use this model.", "data": null} for a not-yet-authorized
                # key, or a 402 for insufficient credits. Neither heals on
                # retry, so surface the message immediately instead of
                # burning the whole retry budget on "empty content" and
                # hiding what's actually wrong. Gated on "output" being
                # absent so a genuine success payload that happens to carry
                # an incidental "code" field is never misread as an error.
                try:
                    body_json = r.json()
                except ValueError:
                    body_json = None
                if (isinstance(body_json, dict) and "output" not in body_json
                        and isinstance(body_json.get("code"), int)
                        and body_json["code"] not in (0, 200)):
                    raise RuntimeError(
                        f"LLM ({model}) error {body_json['code']}: "
                        f"{body_json.get('msg') or r.text[:400]}")
                text = _parse(spec, body_json if body_json is not None else {})
                if text:
                    return text
                # 200 with empty content happens on provider hiccups — retry
                last_err = RuntimeError(
                    f"LLM ({model}) returned empty content: "
                    f"{r.text[:300]}")
            elif r.status_code == 429:
                # Rate-limited — retryable, and more likely to happen with
                # several videos hitting this same key concurrently. Honor
                # a Retry-After header if the provider sends one.
                last_err = RuntimeError(
                    f"LLM ({model}) rate limited (429): {r.text[:300]}")
                retry_after = r.headers.get("Retry-After")
                is_5xx = True  # reuse the 5xx backoff/retry path below
            else:
                last_err = RuntimeError(
                    f"LLM ({model}) error {r.status_code}: {r.text[:400]}")
                if r.status_code < 500:
                    raise last_err  # client errors don't heal with retries
                is_5xx = True
                retry_after = None
        except requests.RequestException as e:
            last_err = e
            is_5xx = True  # treat network blips like 5xx for backoff
            retry_after = None
        if attempt < retries:
            # 5xx/429: 8s, 16s, 32s, 64s, 90s… — give the provider time to
            # recover; a Retry-After header (429) wins if it asks for longer.
            # empty-200: shorter linear backoff
            if is_5xx:
                wait = min(90.0, 8.0 * (2 ** attempt))
                if retry_after:
                    try:
                        wait = max(wait, min(float(retry_after), 90.0))
                    except ValueError:
                        pass
            else:
                wait = 4.0 * (attempt + 1)
            print(f"[llm] {model} attempt {attempt + 1}/{retries + 1} "
                  f"failed ({last_err}); retrying in {wait:.0f}s…", flush=True)
            cancel.sleep(wait)

    if allow_cli_fallback and config.CLAUDE_CLI_FALLBACK:
        try:
            print(f"[llm] {model}: kie.ai retry budget exhausted "
                  f"({last_err}) — falling back to local Claude CLI "
                  f"({config.CLAUDE_CLI_MODEL})…", flush=True)
            return _claude_cli_chat(content_for_cli=user, system=system)
        except Exception as cli_err:  # noqa: BLE001
            print(f"[llm] Claude CLI fallback also failed: {cli_err}",
                  flush=True)
    raise last_err


def _chat_via_cli(user: str, system: Optional[str], retries: int = 2) -> str:
    """Entry point for the "claude-cli" script model — script generation
    goes straight through the local Claude CLI, never touching kie.ai.
    A short retry for transient local hiccups only: an auth failure
    ("Not logged in") won't heal by waiting, so this stays brief instead
    of mirroring kie's long provider-outage backoff.
    """
    last_err: Exception = RuntimeError("claude CLI: no attempt made")
    for attempt in range(retries + 1):
        try:
            return _claude_cli_chat(content_for_cli=user, system=system)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                print(f"[llm] claude-cli attempt {attempt + 1}/{retries + 1} "
                      f"failed ({e}); retrying in 5s…", flush=True)
                cancel.sleep(5)
    raise last_err


def _claude_cli_chat(content_for_cli: str, system: Optional[str]) -> str:
    """Shell out to the locally installed `claude` CLI, authenticated under
    this machine's own Claude subscription (usage against the plan, not
    the kie/API-key billing above). Used both as the "claude-cli" script
    model's primary generation path and as chat()/chat_json()'s fallback
    when kie.ai itself is the one failing.

    The prompt is piped via stdin (not a positional CLI argument) so a
    very long script section never risks the OS argv length limit.
    --tools "" disables all tool use so this is a pure one-shot text
    completion, never an agentic session touching the filesystem.
    """
    cmd = [config.CLAUDE_CLI_PATH, "-p", "--tools", "",
           "--no-session-persistence", "--output-format", "text",
           "--model", config.CLAUDE_CLI_MODEL]
    if system:
        cmd += ["--system-prompt", system]
    proc = subprocess.run(cmd, input=content_for_cli, capture_output=True,
                          text=True, timeout=config.CLAUDE_CLI_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
    text = proc.stdout.strip()
    if not text:
        raise RuntimeError("claude CLI returned empty output")
    return text


def chat_json(user: str, model: str, max_tokens: int = 8000, attempts: int = 3,
              allow_cli_fallback: bool = True, **kw) -> Dict[str, Any]:
    """chat() + extract_json() with self-healing: models sometimes preface
    the JSON with commentary or skip it entirely — re-ask firmly.

    This is a distinct failure mode from chat()'s own kie.ai-down fallback:
    kie can return a fast, healthy HTTP 200 whose content just isn't valid
    JSON (a quality issue, not a transport failure), which chat()'s retry
    loop has no reason to catch. If every re-ask here still comes back
    unparseable, try the Claude CLI once as a last resort before giving up.
    """
    prompt = user
    last: Exception = RuntimeError("no attempt made")
    for a in range(attempts):
        try:
            return extract_json(chat(
                prompt, model, max_tokens=max_tokens,
                allow_cli_fallback=allow_cli_fallback, **kw))
        except (json.JSONDecodeError, RuntimeError) as e:
            last = e
            prompt = (user + "\n\nIMPORTANT: your previous reply was not "
                      "valid JSON. Respond with ONLY the JSON object, "
                      "starting with '{' — no preamble, no commentary.")

    if allow_cli_fallback and config.CLAUDE_CLI_FALLBACK:
        try:
            print(f"[llm] {model}: {attempts} JSON attempts all unparseable "
                  f"({last}) — trying local Claude CLI for strict JSON…",
                  flush=True)
            cli_text = _claude_cli_chat(content_for_cli=prompt, system=None)
            return extract_json(cli_text)
        except Exception as cli_err:  # noqa: BLE001
            print(f"[llm] Claude CLI JSON fallback also failed: {cli_err}",
                  flush=True)
    raise last


def extract_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of the model's text, tolerating code fences,
    unescaped quotes, and truncation (long outlines/plans inside one JSON
    string get mangled by every model sooner or later — repair, don't die)."""
    text = text.strip()
    if not text:
        raise RuntimeError("LLM returned an empty response")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            text = text[s:e + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import json_repair
        data = json_repair.loads(text)
        if not isinstance(data, dict) or not data:
            raise
        return data
