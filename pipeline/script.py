"""Grandma Hannah script engine (Sonnet 5) — staged, per the channel profile.

Stage 1  outline_and_hooks() -> a DETAILED outline + exactly 4 hooks
         (bold_statement, question, story, comment) as options.
         The app then STOPS at the approval gate: the user picks ONE hook
         and approves/edits the outline.
Stage 3  write_script()      -> the full continuous-prose script: fixed AI
         disclosure, the chosen hook verbatim, then the approved structure
         with CTAs and safety caveats woven in.

plan_beats()          types every beat as b-roll (stock / ai_video / animation).
mark_host_segments()  then decides where the HeyGen host is visible: one
         full-screen intro (20-50s) and split-screen moments spread through
         the video, all inside a hard host-seconds budget (credits!).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import animation, llm

CHARS_PER_SEC = 1000.0 / 60.0  # ~1000 chars ≈ 1 minute of TTS

HOOK_ORDER = ["bold_statement", "question", "story", "comment"]


def _persona_context(inputs: Dict[str, Any], profile: Dict[str, Any]) -> str:
    host = profile.get("host", {})
    rules = "\n".join(f"- {r}" for r in profile.get("rules", []))
    channel = profile.get("channel_context",
                          "a senior-health YouTube channel for viewers aged 60-80")
    cited = (inputs.get("cited_philosophers") or "").strip()
    cited_line = (f"Cited thinker(s) for THIS video — their authentic concepts "
                  f"and vocabulary carry the teaching: {cited}.\n" if cited else "")
    return (
        f"You are writing for {channel}.\n"
        f"PERSONA / NARRATOR: {host.get('persona', '')}\n"
        f"REGISTER: {host.get('register', '')}\n"
        f"NORTH STAR: {profile.get('north_star', '')}\n"
        f"Video title/topic: {inputs['title']}.\n"
        f"The title is packaging — NEVER mention, correct, qualify, or "
        f"apologize for it in the narration. No 'the title says…', no "
        f"walking back its promises. Teach the content honestly and let the "
        f"claims themselves carry the precision.\n"
        f"{cited_line}"
        f"Target language of the finished script: {inputs['language']}.\n"
        f"HARD RULES:\n{rules}\n"
    )


def _ebook_line(inputs: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """The full e-book CTA instruction: profile wording + QR-badge hint +
    verbatim user block. Shared by the inline section slot and the
    position-targeted weave (insert_ebook_cta)."""
    line = (profile.get("cta_lines", {}) or {}).get("ebook", "")
    if line and inputs.get("qr_badge"):
        # the corner QR card is burned into the whole video this run
        line += (" A small QR-code card sits in the top-left corner of "
                 "the video for its whole duration and leads to the same "
                 "website — add ONE natural half-sentence pointing to it "
                 "(e.g. 'or just point your phone at the little square "
                 "in the corner'). Mention it only here, never again.")
    if inputs.get("promote_ebook") and (inputs.get("ebook_text") or "").strip():
        line += (" Insert this exact CTA block VERBATIM (only convert "
                 "informal address to match the body's formality): "
                 f"»{inputs['ebook_text'].strip()}«")
    return line


def _spotify_line(inputs: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Optional Spotify popup cue woven into midpoint/close CTAs."""
    if not inputs.get("spotify_popup"):
        return ""
    url = (inputs.get("spotify_url") or "").strip()
    custom = (profile.get("cta_lines", {}) or {}).get("spotify", "").strip()
    if custom:
        base = custom
    else:
        artist = ("GEISTMEISTER" if
                  inputs.get("spotify_popup_style") == "geistmeister" else "")
        base = ("A Spotify card sits in the bottom-right corner of the video. "
                "Add ONE natural half-sentence inviting the viewer to listen "
                + (f"to {artist} " if artist else "")
                + "on Spotify (or check the link in the description) — warm, "
                "optional, never salesy. Mention it only once.")
    if url:
        base += f" The Spotify link is: {url}."
    return base


def _disclosure(profile: Dict[str, Any]) -> str:
    """The spoken AI disclosure as plain text. Tolerates a structured value
    (dict with a text-ish field) so a doc-style profile can't crash a job."""
    d = profile.get("ai_disclosure", "")
    if isinstance(d, dict):
        d = d.get("text") or d.get("line") or d.get("spoken") or ""
    return str(d or "").strip()


# ─────────────────────── Stage 1: outline + hooks ───────────────────────
# Models often truncate when asked for 4–5 long hooks + a full outline in
# one JSON blob. Generate hooks and outline in separate calls; re-ask only
# for missing hook keys.
_HOOK_KEY_ALIASES = {
    "story": "short_story",
    "shortstory": "short_story",
    "comment": "you_told_me_in_the_comments",
    "comments": "you_told_me_in_the_comments",
    "you_told_me": "you_told_me_in_the_comments",
    "pattern": "pattern_break",
    "bold": "bold_statement",
}


def _normalize_hooks_dict(raw: Any, hook_order: List[str]) -> Dict[str, str]:
    """Map model hook keys onto the profile's expected keys."""
    if not isinstance(raw, dict):
        return {k: "" for k in hook_order}
    by_lower = {str(k).strip().lower(): v for k, v in raw.items()}
    out: Dict[str, str] = {}
    for want in hook_order:
        val = by_lower.get(want.lower(), "")
        if not val:
            for alias, canon in _HOOK_KEY_ALIASES.items():
                if canon == want and alias in by_lower:
                    val = by_lower[alias]
                    break
        if not val:
            # fuzzy: model used a close key (e.g. "short_story_hook")
            for rk, rv in by_lower.items():
                if want.lower() in rk or rk in want.lower():
                    val = rv
                    break
        out[want] = str(val or "").strip()
    return out


def _generate_hooks(inputs: Dict[str, Any], profile: Dict[str, Any],
                    hook_order: List[str], hook_types: Dict[str, Any],
                    model: str, log=lambda *_: None) -> Dict[str, str]:
    """Generate all hook options; fill gaps with targeted re-asks."""
    audience = profile.get(
        "audience_line", "speaks to ONE viewer aged 60-80")
    opening = ("The hook follows the channel's fixed AI disclosure"
               if _disclosure(profile)
               else "The hook is the literal opening of the video")
    hooks: Dict[str, str] = {k: "" for k in hook_order}
    # Full batch first, then only missing keys (avoids one mega-JSON with outline)
    for attempt in range(4):
        missing = [k for k in hook_order if not hooks[k]]
        if not missing:
            break
        hooks_def = "\n".join(
            f'- "{k}": {hook_types.get(k, "")}' for k in missing)
        hooks_json = ", ".join(f'"{k}": "..."' for k in missing)
        n = len(missing)
        prompt = (
            f"{_persona_context(inputs, profile)}\n"
            f"Target length of the finished video: {inputs['length_chars']} "
            f"characters (~{inputs['length_chars'] // 1000} minutes spoken).\n\n"
            f"Write EXACTLY {n} hook OPTION(S) in ENGLISH (working language), "
            f"one for each type below. Each is a real spoken opening of roughly "
            f"120–200 words that {audience} and makes only promises the video "
            f"can honestly keep. {opening}, so its first sentence must grab "
            f"attention and do real work.\n"
            f"Hook type(s):\n{hooks_def}\n\n"
            f"Return ONLY JSON (no outline, no other keys):\n"
            f"{{\"hooks\": {{{hooks_json}}}}}"
        )
        log(f"hooks attempt {attempt + 1}/4 "
            f"({'all' if n == len(hook_order) else 'fill ' + ', '.join(missing)})…")
        data = llm.chat_json(prompt, model, max_tokens=12000, timeout=240)
        got = data.get("hooks", data) if isinstance(data, dict) else {}
        filled = _normalize_hooks_dict(got, missing)
        for k, v in filled.items():
            if v and not hooks[k]:
                hooks[k] = v
        still = [k for k in hook_order if not hooks[k]]
        if still:
            log(f"hooks still missing: {', '.join(still)}")

    # Last resort: generate each missing hook as plain prose (no JSON)
    for k in [k for k in hook_order if not hooks[k]]:
        log(f"hooks: plain-text fallback for '{k}'…")
        text = llm.chat(
            f"{_persona_context(inputs, profile)}\n"
            f"Write ONE video hook in ENGLISH, type '{k}': "
            f"{hook_types.get(k, '')}\n"
            f"~120–200 spoken words, {audience}. "
            f"Output ONLY the hook prose — no labels, no JSON.",
            model, max_tokens=1500, timeout=120,
        ).strip()
        if text:
            hooks[k] = text

    missing = [k for k, v in hooks.items() if not v]
    if missing:
        raise RuntimeError(
            f"hook generation incomplete (missing: {', '.join(missing)})")
    return hooks


def _generate_outline(inputs: Dict[str, Any], profile: Dict[str, Any],
                      model: str, log=lambda *_: None) -> str:
    """Detailed outline as plain text (or JSON.outline) — separate from hooks."""
    sections = "\n".join(
        f"{i+1}. {s['name']}: {s['hint'][:220]}"
        for i, s in enumerate(profile.get("writing_sections", [])))
    ebook = ("The e-book CTA IS flagged for this video — mark its position "
             "near the midpoint comment prompt." if inputs.get("promote_ebook")
             else "No e-book CTA in this video.")
    prompt = (
        f"{_persona_context(inputs, profile)}\n"
        f"Target length: {inputs['length_chars']} characters (~"
        f"{inputs['length_chars'] // 1000} minutes spoken).\n\n"
        f"Write a DETAILED outline of the whole video IN ENGLISH (working "
        f"language). Cover every section below with its beats sketched in a "
        f"few lines each, flag any safety caveats where they will appear, "
        f"include a short SOURCE LIST for every claim-bearing section (the "
        f"real tradition, book, or teaching in plain words — never "
        f"fabricated), and suggest on-screen evidence moments for the "
        f"editor. {profile.get('outline_extras', '')} {ebook}\n"
        f"Sections (after the chosen hook):\n"
        f"{sections}\n\n"
        f"Return ONLY JSON:\n"
        f'{{\"outline\": \"...full outline as one string, use \\\\n for newlines...\"}}'
    )
    for attempt in range(3):
        log(f"outline attempt {attempt + 1}/3…")
        data = llm.chat_json(prompt, model, max_tokens=12000, timeout=240)
        outline = ""
        if isinstance(data, dict):
            outline = str(data.get("outline") or data.get("body") or "").strip()
            # model sometimes returns sections as a list
            if not outline and isinstance(data.get("sections"), list):
                outline = "\n\n".join(str(s) for s in data["sections"]).strip()
        if len(outline) >= 400:
            return outline
        log(f"outline attempt {attempt + 1}/3 too short "
            f"({len(outline)} chars) — retrying")

    # Plain-text fallback (no JSON wrapper to truncate)
    log("outline: plain-text fallback…")
    text = llm.chat(
        f"{_persona_context(inputs, profile)}\n"
        f"Target length: {inputs['length_chars']} characters.\n\n"
        f"Write a DETAILED video outline in ENGLISH covering every section:\n"
        f"{sections}\n\n"
        f"{ebook} {profile.get('outline_extras', '')}\n"
        f"Include beats, safety flags, source list, and on-screen evidence "
        f"notes. Output ONLY the outline prose — no JSON, no preamble.",
        model, max_tokens=12000, timeout=240,
    ).strip()
    if len(text) < 400:
        raise RuntimeError(
            f"outline generation incomplete after retries ({len(text)} chars)")
    return text


def outline_and_hooks(inputs: Dict[str, Any], profile: Dict[str, Any],
                      model: str, log=lambda *_: None) -> Dict[str, Any]:
    """Return {"outline": str, "hooks": {type: text}} — English, for approval.

    Hooks and outline are separate LLM calls so long Matthias/classic hook
    sets no longer truncate the outline (or vice versa) mid-JSON.
    """
    hook_types = profile.get("hook_types", {}) or {}
    hook_order = list(hook_types) or list(HOOK_ORDER)
    log(f"generating {len(hook_order)} hooks, then outline (split calls)…")
    hooks = _generate_hooks(inputs, profile, hook_order, hook_types, model, log=log)
    outline = _generate_outline(inputs, profile, model, log=log)
    log(f"outline ready ({len(outline)} chars), {len(hooks)} hooks ready")
    return {"outline": outline, "hooks": hooks}


# ─────────────────────── Stage 3: full script ───────────────────────────
def _generate_section(name: str, target_chars: int, ctx: str, tail: str,
                      extra: str, model: str, language: str = "English",
                      log=lambda *_: None) -> str:
    """Generate one structural section, continuing until ~target length.

    Large sections (e.g. 40k teaching) are written in moderate chunks — a
    single 16k-token call often triggers provider 500s on kie. Smaller
    chunks + retries in llm.chat() is more reliable.
    """
    out = ""
    guard = 0
    # ~3–4k chars per call is safer than asking for the full remaining 40k
    chunk_chars = 3500
    while len(out) < int(target_chars * 0.97) and guard < 20:
        remaining = target_chars - len(out)
        ask = min(remaining, chunk_chars)
        cont = (f"Continue seamlessly from where this leaves off, without repeating it:\n"
                f"...{(tail + out)[-600:]}\n\n") if (tail or out) else ""
        prompt = (
            f"{ctx}\n"
            f"Write the '{name}' part of the narration, IN {language.upper()} — "
            f"every sentence must be {language}, regardless of the language of "
            f"any excerpt above.\n{extra}\n"
            f"{cont}"
            f"Write approximately {ask} more characters (spaces included) — "
            f"not more. "
            f"Output ONLY the narration prose for this part — no headings, labels, "
            f"quotes around it, or notes. Continuous prose only."
        )
        log(f"  {name}: chunk {guard + 1} (~{ask} chars, have {len(out)}/{target_chars})")
        # 8k tokens is enough for ~3.5k chars of prose with headroom
        chunk = llm.chat(prompt, model, max_tokens=8000, timeout=180)
        if not chunk:
            break
        out += ("" if not out else " ") + chunk.strip()
        guard += 1
    return out.strip()


def write_script(inputs: Dict[str, Any], profile: Dict[str, Any],
                 outline: str, hook_text: str, model: str,
                 log=lambda *_: None) -> str:
    """Produce the full narration: disclosure + hook + outlined sections."""
    disclosure = _disclosure(profile)
    hook_text = hook_text.strip()

    # Hooks are approved in English; for other languages the chosen hook must
    # be rendered into the target language FIRST — the sections continue from
    # its tail, so an English opening drags the whole script into English.
    language = inputs["language"]
    if hook_text and not language.lower().startswith("english"):
        log(f"rendering the chosen hook in {language}…")
        hook_text = llm.chat(
            f"{_persona_context(inputs, profile)}\n"
            f"Render this approved video hook in {language}, preserving its "
            f"voice, impact, and promises (adapt idioms naturally, follow the "
            f"language conventions in the rules). Output ONLY the {language} "
            f"prose:\n\n{hook_text}",
            model, max_tokens=2000).strip() or hook_text
    body_target = max(600, int(inputs["length_chars"]) - len(disclosure) - len(hook_text))

    ctas = profile.get("cta_lines", {})
    ebook_line = _ebook_line(inputs, profile)
    spotify_line = _spotify_line(inputs, profile)
    ctx = (
        f"{_persona_context(inputs, profile)}\n"
        f"THE APPROVED OUTLINE for this video (follow it; the sources listed in "
        f"it are for your accuracy, never read them aloud as citations unless "
        f"real and verified):\n{outline}\n\n"
        + ("The script so far opened with the channel's fixed AI disclosure and "
           "the chosen hook. " if disclosure else
           "The script so far opened with the chosen hook. ")
        + "Continue in the same voice, one continuous spoken prose, addressed "
          "to ONE viewer.\n"
    )

    parts: List[str] = [disclosure, hook_text]
    tail = hook_text[-800:]
    spotify_placed = False
    for sec in profile.get("writing_sections", []):
        hint = sec["hint"]
        if "{EBOOK}" in hint:
            # a user-chosen character position moves the CTA out of this
            # section — it gets woven in post-write by insert_ebook_cta
            inline_ebook = (inputs.get("promote_ebook")
                            and not inputs.get("ebook_cta_at"))
            hint = hint.replace(
                "{EBOOK}", ebook_line if inline_ebook else "")
        if "{PATREON}" in hint:  # legacy token — the cut is now inserted
            hint = hint.replace("{PATREON}", "")  # surgically post-write
        if "{LIKE_SUBSCRIBE}" in hint:
            like = ctas.get("like_subscribe", "")
            if inputs.get("promote_community"):
                like += " " + ctas.get("community", "")
            hint = hint.replace("{LIKE_SUBSCRIBE}", like)
        if sec["name"] == "midpoint_ctas" and not inputs.get("comment_cta", True):
            hint = ("A short natural mid-video pause with a gentle re-hook tease "
                    "for what comes next. No comment prompt in this video.")
        # One natural Spotify mention when the on-screen popup is enabled
        if spotify_line and not spotify_placed and sec["name"] in (
                "midpoint_ctas", "close_block", "close"):
            hint = (hint + " " + spotify_line).strip()
            spotify_placed = True
        target = max(250, int(body_target * float(sec.get("weight", 0.1))))
        log(f"writing {sec['name']} (~{target} chars)")
        try:
            text = _generate_section(sec["name"], target, ctx, tail, hint,
                                     model, language=language, log=log)
        except Exception as e:  # noqa: BLE001
            # Surface which section died so the UI / resume is clear.
            # Transient 5xx is already retried inside llm.chat; if we still
            # land here the provider stayed down — re-raise with context.
            raise RuntimeError(
                f"script section '{sec['name']}' failed after retries: {e}"
            ) from e
        if not text.strip():
            raise RuntimeError(
                f"script section '{sec['name']}' came back empty — "
                f"provider may be overloaded; resume this job to retry")
        log(f"  {sec['name']}: done ({len(text)} chars)")
        parts.append(text)
        tail = text[-800:]
    return "\n\n".join(p for p in parts if p).strip()


# ─────────────────────── Beat planning ──────────────────────────────────
_SENT = re.compile(r'[^.!?…]+[.!?…]+["»]?\s*|\S+\s*$')


def _split_into_chunks(text: str, n: int) -> List[str]:
    """Split narration into ~n roughly-equal, sentence-aware chunks."""
    sentences = [s.strip() for s in _SENT.findall(text) if s.strip()]
    if not sentences:
        return [text]
    n = max(1, min(n, len(sentences)))
    target = len(text) / n
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > target * 1.15 and len(chunks) < n - 1:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur.strip())
    return _rebalance(chunks, target)


def _rebalance(chunks: List[str], target: float) -> List[str]:
    """No chunk may exceed ~1.6× target — an oversized beat means minutes of
    one looping visual. Oversized chunks are re-split at sentence boundaries."""
    out: List[str] = []
    for c in chunks:
        while len(c) > target * 1.6:
            sentences = [s for s in _SENT.findall(c) if s.strip()]
            if len(sentences) < 2:
                break
            acc = ""
            for s in sentences[:-1]:
                acc += s
                if len(acc) >= target:
                    break
            head, tail = acc.strip(), c[len(acc):].strip()
            if not head or not tail:
                break
            out.append(head)
            c = tail
        out.append(c)
    return out


# Every animation template the planner may use: schema + when to use it.
ANIM_SCHEMAS: Dict[str, str] = {
    "checklist": ("{\"title\": short title, \"items\": [3-5 short strings]} — "
                  "action steps or habit lists with checkmarks"),
    "stat": ("{\"number\": REAL numeric value > 0, \"suffix\": e.g. \"%\", "
             "\"label\": what the number means (short), \"note\": source} — "
             "one big count-up number; only for genuine numbers"),
    "compare": ("{\"title\": short title, \"left\": {\"title\": e.g. \"Do this\", "
                "\"items\": [2-4 short]}, \"right\": {\"title\": e.g. \"Avoid this\", "
                "\"items\": [2-4 short]}} — do/avoid advice"),
    "quote": ("{\"text\": the quote (max ~140 chars), \"attribution\": who/what "
              "said it} — a spoken saying, proverb, or research finding worth "
              "seeing verbatim"),
    "keyword": ("{\"word\": ONE key term (1-3 words), \"sub\": short supporting "
                "line} — punch a single important word the narration lands on"),
    "barchart": ("{\"title\": short title, \"unit\": e.g. \"g sugar\", \"bars\": "
                 "[{\"label\": str, \"value\": number}] 2-5 bars, REAL numbers} — "
                 "comparisons across items (the on-screen evidence moment)"),
    "list": ("{\"title\": short title, \"items\": [2-5 short strings]} — "
             "numbered countdown/steps revealed one by one"),
    "warning": ("{\"title\": e.g. \"Check first\", \"text\": the caveat in one "
                "sentence, \"items\": optional [medication names]} — medication/"
                "safety caveats; use when the narration speaks a safety line"),
    "timeline": ("{\"title\": short title, \"points\": [{\"label\": e.g. \"Morning\", "
                 "\"text\": short}] 3-5 points} — sequences, routines, history"),
    "gauge": ("{\"percent\": number 1-100, \"label\": what it measures} — a "
              "single percentage as a filling gauge"),
    "didyouknow": ("{\"fact\": one surprising checkable sentence} — a 'did you "
                   "know' moment the narration lands on"),
    "mythfact": ("{\"myth\": the false belief (short), \"fact\": the truth "
                 "(short)} — when the narration corrects a misconception"),
    "definition": ("{\"word\": the term, \"pronunciation\": optional phonetic, "
                   "\"word_class\": optional part-of-speech label in the "
                   "video's language (e.g. \"Substantiv\"), \"definition\": "
                   "plain-words meaning} — dictionary card for key terms"),
    "trio": ("{\"title\": short title, \"items\": [{\"value\": short number/"
             "word, \"label\": short}] exactly 2-3} — two or three small "
             "facts side by side"),
    "photocard": ("{\"caption\": short italic caption, \"query\": stock photo "
                  "search per the query rules} — a framed real photograph "
                  "with a handwritten-style caption; warm scrapbook feel"),
    "chapter": ("{\"kicker\": e.g. \"Part Two\", \"title\": the section name} "
                "— a section divider when the video changes topic"),
}


def _mix_for(profile: Dict[str, Any], broll_modes: List[str]) -> Dict[str, float]:
    base = profile.get("video", {}).get(
        "broll_mix", {"stock": 0.5, "ai_video": 0.3, "animation": 0.2})
    mix = {k: float(base.get(k, 0)) for k in broll_modes if base.get(k, 0) > 0}
    if not mix:
        mix = {k: 1.0 for k in (broll_modes or ["stock"])}
    total = sum(mix.values())
    return {k: v / total for k, v in mix.items()}


QUERY_RULES = (
    "a CONCRETE, literally filmable 2-4 word English 'query': visible subject "
    "+ action or setting (\"senior woman stretching\", \"pouring herbal tea\", "
    "\"doctor patient talking\", \"clove spice closeup\"). NEVER abstract ideas, "
    "emotions, metaphors, or medical jargon — only things a stock camera sees. "
    "The query must depict what the segment as a WHOLE is about — read the "
    "full excerpt, not just its first words."
)


def plan_beats(narration: str, profile: Dict[str, Any], broll_modes: List[str],
               model: str, anim_templates: Optional[List[str]] = None,
               language: str = "English",
               log=lambda *_: None) -> Tuple[List[Dict[str, Any]], str]:
    """Type every beat as b-roll. Returns (beats, music_prompt).

    Every beat gets a b-roll visual — host placement is decided separately by
    mark_host_segments(), and split-screen beats still need their b-roll half.
    anim_templates restricts which motion-graphic templates may be used.
    """
    v = profile.get("video", {})
    beat_seconds = float(v.get("beat_seconds", 15))
    chars_per_beat = max(120, int(1000 * beat_seconds / 60))
    n = max(3, round(len(narration) / chars_per_beat))
    chunks = _split_into_chunks(narration, n)

    allowed_anims = [t for t in (anim_templates if anim_templates is not None
                                 else list(ANIM_SCHEMAS))
                     if t in ANIM_SCHEMAS and t in animation.TEMPLATES]
    modes = list(broll_modes)
    if "animation" in modes and not allowed_anims:
        modes.remove("animation")
    mix = _mix_for(profile, modes)
    style = profile.get("style", {})
    log(f"split into {len(chunks)} beats; assigning b-roll "
        f"({len(allowed_anims)} templates allowed)")

    schema_txt = "\n".join(f"    {t}: {ANIM_SCHEMAS[t]}" for t in allowed_anims)
    mix_txt = ", ".join(f"~{round(p * 100)}% {k}" for k, p in mix.items())

    def _batch_prompt(idx_chunks: List[Tuple[int, str]]) -> str:
        n_batch = len(idx_chunks)
        numbered = "\n".join(f"[{i}] {c[:400]}" for i, c in idx_chunks)
        return (
        f"Below are {n_batch} consecutive segments of a long-form video "
        f"narration. Assign each segment ONE "
        f"b-roll visual, split roughly {mix_txt}:\n"
        + (f"- \"stock\": real footage; give {QUERY_RULES}\n"
           if "stock" in mix else "")
        + (f"- \"stock_image\": a still photo with a slow zoom; same query rules "
           f"as stock. Prefer it when a static subject fits (foods, herbs, "
           f"objects, close-ups).\n" if "stock_image" in mix else "")
        + ("- \"ai_video\": generated footage; give a vivid one-sentence English "
           f"'prompt' in this style: {style.get('broll_style', 'warm documentary')}. "
           "No on-screen text, no readable faces. Use SPARINGLY, only when no "
           "real footage could show it.\n" if "ai_video" in mix else "")
        + ("- \"ai_image\": a generated STILL image shown for the whole beat "
           "with a slow zoom; give a vivid one-sentence English 'prompt' in "
           f"this style: {style.get('broll_style', 'cinematic')}. Best for "
           "timeless, symbolic, atmospheric moments no stock camera has "
           "filmed.\n" if "ai_image" in mix else "")
        + ((lambda n_anim:
            f"- \"animation\": motion-graphics card; give 'template' + 'params'. "
            f"Assign type 'animation' to AT LEAST {n_anim} of the {n_batch} "
            f"segments — this count is a requirement, not a suggestion. Spread "
            f"many DIFFERENT templates across the video. Templates:\n"
            f"{schema_txt}\n")(max(1, round(n_batch * mix.get("animation", 0))))
           if "animation" in mix else "")
        + f"\nEvery beat also gets a 'query' (backup stock search, same rules). "
        f"NEVER reuse the same query twice — vary subjects, actions, and "
        f"settings across the whole video so the footage never repeats. "
        f"Write every 'query', 'prompt', and the 'music_prompt' in ENGLISH; "
        f"write all animation params (on-screen display text) in {language}, "
        f"the language of the video. Also write one 'music_prompt' describing: "
        f"{style.get('music_style', 'gentle acoustic')}.\n\n"
        f"Return ONLY JSON: {{\"music_prompt\":\"...\",\"beats\":[{{\"i\":{idx_chunks[0][0]},"
        f"\"type\":\"stock\",\"query\":\"...\"}}, ...]}} with exactly {n_batch} "
        f"beats using the segment numbers shown.\n\nSEGMENTS:\n{numbered}"
        )

    def _fallback_type(i: int) -> str:
        order = sorted(mix, key=mix.get, reverse=True)
        return order[i % len(order)]

    def _clean(b: Dict[str, Any], i: int, text: str) -> Dict[str, Any]:
        t = b.get("type")
        if t == "animation" and b.get("template") not in allowed_anims:
            t = "stock"
        if t == "animation" and b.get("template") == "stat":
            # a stat card with no real number renders a big "0" — demote
            try:
                if not float((b.get("params") or {}).get("number", 0)) > 0:
                    t = "stock"
            except (TypeError, ValueError):
                t = "stock"
        if t not in mix:
            t = _fallback_type(i)
        if t == "animation" and b.get("template") not in allowed_anims:
            # never invent an animation: a fallback-typed beat has no
            # template/params — use the heaviest non-animation type instead
            t = next((k for k in sorted(mix, key=mix.get, reverse=True)
                      if k != "animation"), "stock")
        out = {"text": text, "type": t,
               "query": b.get("query") or " ".join(text.split()[:3])}
        if t in ("ai_video", "ai_image"):
            out["prompt"] = (b.get("prompt")
                             or f"{text[:140]}, {style.get('broll_style', '')}")
        elif t == "animation":
            out["template"] = b["template"]
            out["params"] = b.get("params") or {}
        return out

    # Long videos (3h ≈ 300+ beats) are planned in batches to stay inside
    # sane prompt/response sizes.
    BATCH = 100
    beats: List[Dict[str, Any]] = []
    music_prompt = style.get("music_style", "")
    by_i: Dict[int, Dict[str, Any]] = {}
    idx_all = list(enumerate(chunks))
    for start in range(0, len(idx_all), BATCH):
        batch = idx_all[start:start + BATCH]
        if len(idx_all) > BATCH:
            log(f"planning beats {start + 1}-{start + len(batch)} of {len(idx_all)}")
        try:
            data = llm.chat_json(_batch_prompt(batch), model, max_tokens=16000)
            if start == 0:
                music_prompt = data.get("music_prompt", music_prompt)
            for b in data.get("beats", []):
                if "i" in b:
                    by_i[int(b["i"])] = b
        except Exception as e:  # noqa: BLE001
            log(f"beat planning fallback for batch at {start} ({e})")
    for i, text in enumerate(chunks):
        beats.append(_clean(by_i.get(i) or {"type": _fallback_type(i)}, i, text))

    # Templates get repetitive fast — cap per-template uses, demote the rest.
    cap = int(v.get("max_template_repeats", 2))
    used: Dict[str, int] = {}
    demote_to = "stock_image" if "stock_image" in mix else "stock"
    for b in beats:
        if b["type"] != "animation":
            continue
        tpl = b["template"]
        used[tpl] = used.get(tpl, 0) + 1
        if used[tpl] > cap:
            b["type"] = demote_to
            b.pop("template", None)
            b.pop("params", None)
    return beats, music_prompt


PATREON_MARKER = "[—— HIER ABSCHNEIDEN · WEITER AUF PATREON ——]"


def insert_patreon_cut(narration: str, position: int, language: str,
                       model: str, log=lambda *_: None) -> str:
    """Surgically insert the Patreon cut at ~`position` characters.

    The passage around the chosen sentence boundary is rewritten so the free
    half ends on a strong cliffhanger, the marker sits alone on its own line,
    and the paid half reopens naturally — placement is exact, unlike asking
    the long-form writer to count characters.
    """
    position = max(500, min(position, len(narration) - 500))
    boundary = position
    match = re.compile(r'[.!?…]["»]?\s').search(narration, position)
    if match:
        boundary = match.end()
    start = max(0, boundary - 900)
    end = min(len(narration), boundary + 900)
    before, after = narration[start:boundary], narration[boundary:end]

    prompt = (
        f"You are editing a continuous spoken narration in {language}. "
        f"A membership cut must fall between excerpt A and excerpt B.\n"
        f"Rewrite them so that: A ends on a STRONG cliffhanger (tease what is "
        f"about to be revealed without revealing it); then this exact marker "
        f"stands alone on its own line:\n{PATREON_MARKER}\n"
        + ("Then B reopens for the members who continued, opening with "
           "»Sie sind also weitergegangen…« "
           if language.lower().startswith(("german", "deutsch")) else
           f"Then B reopens for the members who continued, opening with a "
           f"natural {language} line acknowledging they kept going (the "
           f"equivalent of 'So you decided to keep going…' — NOT in German) ")
        + f"— and flows back into the original content. Keep the "
        f"same voice, keep all information, keep roughly the same total "
        f"length. Return ONLY JSON: {{\"replacement\": \"...\"}} — the full "
        f"text replacing A+B including the marker line.\n\n"
        f"EXCERPT A:\n{before}\n\nEXCERPT B:\n{after}"
    )
    data = llm.chat_json(prompt, model, max_tokens=4000)
    replacement = str(data.get("replacement", "")).strip()
    if "ABSCHNEIDEN" not in replacement:
        raise RuntimeError("cut rewrite did not include the marker")
    log(f"Patreon cut inserted at ~{boundary} chars")
    return narration[:start] + replacement + narration[end:]


def insert_patreon_cut_fallback(narration: str, position: int,
                                language: str = "English",
                                log=lambda *_: None) -> str:
    """Insert the Patreon marker at a sentence boundary (no LLM rewrite).

    Used when the cliffhanger rewrite fails (provider/JSON errors). The free
    half still ends cleanly enough for a hard cut + end CTA clip.
    """
    position = max(500, min(int(position), max(len(narration) - 500, 500)))
    boundary = position
    match = re.compile(r'[.!?…]["»]?\s').search(narration, position)
    if match:
        boundary = match.end()
    if language.lower().startswith(("german", "deutsch")):
        reopen = "Sie sind also weitergegangen. "
    else:
        reopen = "So you decided to keep going. "
    insert = (
        f"\n\n{PATREON_MARKER}\n\n{reopen}"
    )
    log(f"Patreon cut (fallback marker) at ~{boundary} chars")
    return narration[:boundary].rstrip() + insert + narration[boundary:].lstrip()


def insert_ebook_cta(narration: str, inputs: Dict[str, Any],
                     profile: Dict[str, Any], position: int,
                     model: str, log=lambda *_: None) -> str:
    """Surgically weave the e-book CTA at ~`position` characters.

    Mirrors insert_patreon_cut: the passage around the nearest sentence
    boundary is rewritten as the profile's CTA sandwich (value → CTA →
    immediate re-hook) — placement is exact, unlike asking the long-form
    writer to count characters.
    """
    language = inputs.get("language", "English")
    ebook_line = _ebook_line(inputs, profile)
    position = max(500, min(int(position), len(narration) - 500))
    boundary = position
    match = re.compile(r'[.!?…]["»]?\s').search(narration, position)
    if match:
        boundary = match.end()
    start = max(0, boundary - 800)
    end = min(len(narration), boundary + 800)
    before, after = narration[start:boundary], narration[boundary:end]

    prompt = (
        f"{_persona_context(inputs, profile)}\n"
        f"You are editing a continuous spoken narration in {language}. "
        f"An e-book CTA must be woven in between excerpt A and excerpt B, "
        f"following these channel instructions exactly:\n{ebook_line}\n\n"
        f"Rewrite the passage so A flows naturally into the CTA and then "
        f"IMMEDIATELY re-hooks into B — the viewer must feel the value "
        f"continues right away. Keep the same voice, keep ALL information "
        f"from A and B, write everything IN {language}. Return ONLY JSON: "
        f"{{\"replacement\": \"...\"}} — the full text replacing A+B "
        f"including the CTA.\n\n"
        f"EXCERPT A:\n{before}\n\nEXCERPT B:\n{after}"
    )
    data = llm.chat_json(prompt, model, max_tokens=4000)
    replacement = str(data.get("replacement", "")).strip()
    if len(replacement) < (len(before) + len(after)) * 0.6:
        raise RuntimeError("CTA weave came back suspiciously short")
    log(f"e-book CTA woven in at ~{boundary} chars")
    return narration[:start] + replacement + narration[end:]


def plan_mindmap(inputs: Dict[str, Any], outline: str,
                 beats: List[Dict[str, Any]], model: str,
                 log=lambda *_: None) -> Optional[Dict[str, Any]]:
    """Build the video's recurring mind-map slide and place its appearances.

    Returns {"title", "nodes": [labels], "placements": {beat_index: node_idx}}
    — one appearance per node, at the beat where that point is central.
    Host/split beats and the first/last beats are never used.
    """
    language = inputs.get("language", "English")
    eligible = [i for i, b in enumerate(beats)
                if b.get("type") not in ("host",) and not b.get("split")
                and 0 < i < len(beats) - 1]
    if len(eligible) < 3:
        return None
    numbered = "\n".join(f"[{i}] {beats[i]['text'][:150]}" for i in eligible)
    prompt = (
        f"A long-form video narration is summarized below as numbered "
        f"segments. Build ONE mind map for the whole video, like a "
        f"presentation slide the narrator keeps returning to:\n"
        f"- \"title\": the central topic, 2-5 words, in {language}\n"
        f"- \"nodes\": 4-7 branch labels (2-4 words each, in {language}) — "
        f"the video's main points, in the order they are discussed\n"
        f"- \"placements\": for EACH node, the segment number where that "
        f"point is most centrally discussed (one segment per node, all "
        f"different, chosen only from the numbers shown, spread across the "
        f"video)\n\n"
        f"Return ONLY JSON: {{\"title\": \"...\", \"nodes\": [\"...\"], "
        f"\"placements\": [{{\"node\": 0, \"segment\": 12}}, ...]}}\n\n"
        f"SEGMENTS:\n{numbered}"
    )
    try:
        data = llm.chat_json(prompt, model, max_tokens=3000)
        nodes = [str(n) for n in data.get("nodes", [])][:8]
        placements: Dict[int, int] = {}
        for p in data.get("placements", []):
            seg, node = int(p.get("segment", -1)), int(p.get("node", -1))
            if seg in eligible and 0 <= node < len(nodes) and seg not in placements:
                placements[seg] = node
        if not nodes or not placements:
            return None
        log(f"mind map: “{data.get('title', '')}” with {len(nodes)} nodes, "
            f"{len(placements)} appearances")
        return {"title": str(data.get("title", "")), "nodes": nodes,
                "placements": placements}
    except Exception as e:  # noqa: BLE001
        log(f"mind map planning failed ({e}) — continuing without")
        return None


def _est_secs(text: str) -> float:
    return len(text) / CHARS_PER_SEC


def mark_host_segments(beats: List[Dict[str, Any]], profile: Dict[str, Any],
                       log=lambda *_: None) -> List[Dict[str, Any]]:
    """Decide where the HeyGen host appears, inside the host-seconds budget.

    - The leading beats merge into ONE full-screen "host" intro of
      host_intro_min..max seconds (the disclosure + hook land on camera).
    - Later beats become split-screen (host + their b-roll) roughly every
      split_every_seconds, each capped at split_max_seconds, until the
      budget runs out. Everything else stays pure b-roll.
    """
    v = profile.get("video", {})
    budget = float(v.get("host_budget_seconds", 180))
    if budget <= 0 or (profile.get("host", {}) or {}).get("enabled") is False:
        log("no on-camera host for this profile — pure voice-over b-roll")
        return beats
    intro_min = float(v.get("host_intro_min_seconds", 20))
    intro_max = float(v.get("host_intro_max_seconds", 50))
    split_every = float(v.get("split_every_seconds", 240))
    split_max = float(v.get("split_max_seconds", 35))

    # 1. Merge the opening beats into one full-screen host intro.
    intro_text, intro_secs, rest_at = "", 0.0, 0
    for i, b in enumerate(beats):
        s = _est_secs(b["text"])
        if intro_secs >= intro_min or (intro_text and intro_secs + s > intro_max):
            break
        intro_text = f"{intro_text} {b['text']}".strip()
        intro_secs += s
        rest_at = i + 1
    marked: List[Dict[str, Any]] = [{"text": intro_text, "type": "host"}]
    budget -= intro_secs

    # 2. Sprinkle split-screen host moments through the rest.
    since_host = 0.0
    n_splits = 0
    for b in beats[rest_at:]:
        s = _est_secs(b["text"])
        if since_host >= split_every and s <= split_max and budget >= s:
            b["split"] = True
            budget -= s
            since_host = 0.0
            n_splits += 1
        else:
            since_host += s
        marked.append(b)
    log(f"host on camera: {intro_secs:.0f}s intro + {n_splits} split-screen "
        f"moments (~{float(v.get('host_budget_seconds', 180)) - budget:.0f}s "
        f"of {v.get('host_budget_seconds', 180)}s budget)")
    return marked
