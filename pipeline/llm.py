"""Script-model client: Claude Sonnet 5 (Anthropic format) or GPT 5.6
(terra/luna/sol, OpenAI Responses format), both via kie.ai.

The active model is selected per video (UI dropdown) with set_model();
everything downstream calls chat()/chat_json() unaware of the backend.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

import requests

from . import config

ACTIVE_MODEL = "sonnet-5"

_WRITER_INSTRUCTIONS = ("You are a world-class long-form scriptwriter and "
                        "video planner. Follow the user's instructions "
                        "exactly and completely.")


def set_model(name: Optional[str]) -> str:
    """Select the script model for this run (falls back to sonnet-5)."""
    global ACTIVE_MODEL
    ACTIVE_MODEL = name if name in config.SCRIPT_MODELS else "sonnet-5"
    return ACTIVE_MODEL


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
    return "".join(
        b.get("text", "") for b in data.get("content", []) or []
        if b.get("type") == "text"
    ).strip()


def chat(user: str, system: Optional[str] = None, max_tokens: int = 8000,
         timeout: int = 300, retries: int = 2) -> str:
    """Send one message to the active script model, return its text.

    Transient 5xx/network/empty responses are retried with backoff — long
    scripts make dozens of calls and must not die on one provider hiccup.
    """
    spec = config.SCRIPT_MODELS[ACTIVE_MODEL]
    if not spec.get("key") or not spec.get("base"):
        raise RuntimeError(f"script model '{ACTIVE_MODEL}' is not configured "
                           f"(missing key/base url)")

    content = f"{system.strip()}\n\n{user}" if system else user
    url = f"{spec['base']}{spec['path']}"
    payload = _payload(spec, content, max_tokens)

    last_err: Exception = RuntimeError("LLM: no attempt made")
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers={
                "Authorization": f"Bearer {spec['key']}",
                "Content-Type": "application/json",
            }, json=payload, timeout=timeout)
            if r.status_code == 200:
                text = _parse(spec, r.json())
                if text:
                    return text
                # 200 with empty content happens on provider hiccups — retry
                last_err = RuntimeError(
                    f"LLM ({ACTIVE_MODEL}) returned empty content: "
                    f"{r.text[:300]}")
            else:
                last_err = RuntimeError(
                    f"LLM ({ACTIVE_MODEL}) error {r.status_code}: {r.text[:400]}")
                if r.status_code < 500:
                    raise last_err  # client errors don't heal with retries
        except requests.RequestException as e:
            last_err = e
        if attempt < retries:
            time.sleep(4 * (attempt + 1))
    raise last_err


def chat_json(user: str, max_tokens: int = 8000, attempts: int = 3,
              **kw) -> Dict[str, Any]:
    """chat() + extract_json() with self-healing: models sometimes preface
    the JSON with commentary or skip it entirely — re-ask firmly."""
    prompt = user
    last: Exception = RuntimeError("no attempt made")
    for a in range(attempts):
        try:
            return extract_json(chat(prompt, max_tokens=max_tokens, **kw))
        except (json.JSONDecodeError, RuntimeError) as e:
            last = e
            prompt = (user + "\n\nIMPORTANT: your previous reply was not "
                      "valid JSON. Respond with ONLY the JSON object, "
                      "starting with '{' — no preamble, no commentary.")
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
