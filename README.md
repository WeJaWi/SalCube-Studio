# 🩺 Senior Health Studio — Grandma Hannah & Matthias Richter

Three channels, one tool — pick the profile in the UI:
- **north-philosopher** — English/German long-form philosopher narrations in
  the cited author's own voice (default Joseph Murphy — change per video via
  "cited thinker(s)"). NO on-camera avatar: pure voice-over b-roll with the
  Murphy cutout pinned bottom-right (`video.figure` in the profile; captions
  auto-clear it). Replaces the old ~/video-studio tool with the full modern
  pipeline (transitions, particles, templates, no-repeat stock).
- **grandma-hannah** — English senior-health, warm disclosed-AI grandmother.
- **matthias-richter** — GERMAN philosophy/manifestation, first-person
  character narrator (63, studying the great thinkers since 2004). Per-video
  "cited thinker(s)" input drives whose authentic concepts carry the teaching
  (Murphy, Jung, …). 5 hooks, Sie + guillemets house style, fresh
  self-introduction after the hook, verbatim e-book blocks, honest
  buried-secret framing (no fabricated studies, no conspiracies). His HeyGen
  character and ai33 voice ("Matthias") are wired into the profile.

A local web app that turns a title into a complete Grandma Hannah
senior-health video: Sonnet 5 writes an outline + 4 hooks, **you approve at
the gate**, the full script is written per the channel profile (AI disclosure
first, medical-safety caveats in Hannah's voice), and the video is assembled
with **Hannah on camera via your HeyGen avatar** — full-screen for the
20-50s intro, then split-screen moments spread through the video, all inside
a hard HeyGen-credit budget. B-roll mixes Pixabay stock, cheap AI video
(Grok Imagine), and free locally-rendered animation templates.

Sibling project of `~/video-studio` (same kie/ai33/Pixabay keys).

## Providers

| Stage | Provider | Model / endpoint | Key (.env) |
|-------|----------|------------------|------------|
| Outline, hooks, script, beat plan | kie.ai (Claude Sonnet 5) | `POST /claude/v1/messages` | `LLM_API_KEY` |
| Host on camera | HeyGen (your avatar) | `POST /v2/video/generate`, audio-driven | `HEYGEN_API_KEY` + `HEYGEN_AVATAR_ID` |
| Voice + subtitles | ai33.pro | `POST /v3/text-to-speech` (`with_transcript`) | `AI33_API_KEY` |
| AI b-roll | kie.ai Grok Imagine (cheapest; swappable) | `grok-imagine/text-to-video`, exact 6-30s | `KIE_VIDEO_API_KEY` |
| Stock photos | Pixabay images API | relevance-scored, Ken Burns | `PIXABAY_IMAGE_API_KEY` |
| Fallback AI images | kie.ai Z-Image | `z-image` | `KIE_API_KEY` |
| Music | ai33.pro Suno | `POST /v1s/task/music-generation` | `AI33_API_KEY` |
| Stock b-roll | Pixabay | videos API | `PIXABAY_API_KEY` |
| Animations | **local** (headless Chromium + FFmpeg) | `anim_templates/*.html` | — |

The **entire narration is voiced by ai33** (one consistent voice). HeyGen
only lip-syncs the on-camera segments from that same audio — so a 30-minute
video needs only ~3 minutes of HeyGen credits, not 30.

## Run

```bash
cd ~/health-studio
./run.sh                 # venv + deps + server → http://127.0.0.1:5062
```

Port note: 5062 on purpose — Chrome refuses to connect to 5060/5061 (SIP).

## The workflow (per the Grandma Hannah script profile)

1. Enter a **title**, length, flags (e-book CTA only on flagged videos), and
   which b-roll sources to allow. Click Generate.
2. Sonnet 5 delivers a **detailed outline + exactly 4 hooks** (bold
   statement, question, story, comment). The job pauses at the **approval
   gate**: pick ONE hook, edit the outline if you like, approve.
   (Or pick an "Auto" hook mode to skip the gate for hands-off runs.)
3. The full script is written: chosen hook → promise → stakes → teaching
   (honesty tiers) → Hannah's world → midpoint comment prompt (+ e-book if
   flagged) → practical how-to (safety lines inline) → evidence recap →
   close → closing comment prompt → soft like/subscribe → end-screen funnel.
   NOTE: there is no spoken AI disclosure (removed July 2026) — the channel
   discloses via a fixed line in every video TITLE + DESCRIPTION plus
   YouTube's altered-content flag. Those are load-bearing for compliance:
   never upload without them. (Set `ai_disclosure` in the profile to bring
   the spoken version back.)
4. Beats are typed (stock / AI video / animation), the host is placed
   (intro + split-screens inside `host_budget_seconds`), everything
   generates in parallel, FFmpeg composites with burned subtitles and the
   music bed.

Outline, hooks, and the final script are saved in the job folder
(`output/<job>/outline.txt`, `hooks.json`, `script.txt`).

## HeyGen setup (configured)

Hannah is the "Smiling woman in a cozy kitchen" look (talking photo
`cf78253c…`) on the connected HeyGen account — note the avatar group is
named "Linda" inside HeyGen, but she is Hannah, the channel's character;
her clips are audio-driven from the ai33 narration. Her still lives
at `hosts/grandma-hannah.png` (used as the fallback if a HeyGen clip fails
and as the UI preview). To switch looks, list them via
`GET /v2/avatar_group.list` → `GET /v2/avatar_group/{id}/avatars` and update
`HEYGEN_AVATAR_ID` (+ `HEYGEN_AVATAR_TYPE=talking_photo|avatar`).

Budget knobs in the profile (`video.…`): `host_intro_min/max_seconds`
(20/50), **`host_budget_seconds` (240 — hard cap: max 4 minutes of Hannah
on camera per video)**, `split_every_seconds` (240), `split_max_seconds` (35).

## Costs

- **HeyGen credits** are the budget the planner protects — only the intro +
  split moments are generated.
- AI b-roll uses Grok Imagine (cheapest on kie; exact durations, no looping).
  Swap via `KIE_VIDEO_MODEL` in `.env` — bytedance/wan/kling model IDs are
  auto-adapted. NOTE: the kie key must be authorized for the chosen model
  (kie dashboard → API Keys), or those beats fall back to stock.
- Animations are free (rendered locally).
- Test with the `test-short` profile first.

## Animation templates (`anim_templates/`)

Sixteen templates: `checklist`, `stat`, `compare`, `quote`, `keyword`,
`barchart`, `list`, `warning`, `timeline`, `gauge`, `didyouknow`,
`mythfact`, `definition`, `trio`, `photocard` (frames a real stock photo,
polaroid-style), `chapter` — standalone HTML with `setup(params, duration)`
+ `seek(t)`, rendered frame-by-frame so they always match the narration
length. No template appears more than `video.max_template_repeats` times
per video (default 2; extras demote to stock photos). The UI has a
per-video picker (with Select all) controlling which templates the planner
may use. Add one: create the HTML, add its name to `TEMPLATES` in
`pipeline/animation.py`, describe its params in `ANIM_SCHEMAS` in
`pipeline/script.py`.

## Mind-map presentation element

A separate toggle (not one of the animation templates): the planner builds
ONE mind map for the whole video — central topic + its main points — and a
handful of times, at the beat where each point is centrally discussed, the
slide appears: a visible mouse cursor glides to that node, clicks, and the
camera zooms into it. Same slide every time (first checked color scheme)
for a "presenter returning to their deck" feel. Costs nothing: rendered
locally, planned with one small LLM call, and it suppresses the paid AI
b-roll of the beats it replaces.

## Production polish

- **Transitions** — beats are joined with crossfades (fade / smooth /
  circle, cycling). Each beat is padded with freeze-frames + silence at the
  junctions so transitions never eat narration. Knobs:
  `video.transitions.{enabled, seconds, types}`.
- **Dust & twinkle overlay** — a locally rendered particle layer
  (`anim_templates/particles.html`, cached in `overlays/` per resolution)
  is screen-blended over stock/AI/image beats. Knobs:
  `video.overlay.{enabled, opacity}`.
- **Stock relevance** — searches pull 25 candidates and score tag coverage
  of the query; weak matches are rejected and fall through video → photo →
  AI image → blank. The planner is forced to write concrete, filmable
  queries. Stock photos get a slow Ken Burns zoom.
- **Beat length cap** — no beat can exceed ~1.6× the target beat length,
  so a single clip never lingers for minutes.

## Notes / known limits

- The script engine encodes the Grandma Hannah profile (see
  `profiles/grandma-hannah.json`, distilled from the PDF in `profiles/`):
  disclosure-first, one-to-one address, defensible longevity framing,
  no conspiracies, no scarcity devices, caveats in-voice. Red-line topics
  should be refused at the outline stage — still, read the outline at the
  gate; that's what it's for.
- Remember to set YouTube's altered/synthetic-content flag on every upload.
- Hannah's ai33 voice id is set per profile (`voices.English`) — replace the
  placeholder with the voice you use for her.
- Suno can stall; music is submitted early and skipped gracefully.
- Subtitle style lives in `render._sub_style` (BorderStyle=4 on purpose —
  3 draws a shadow-copy artifact).
