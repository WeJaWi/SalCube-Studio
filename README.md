# Dmiqwi — video studio

Formerly *Senior Health Studio*. Channel profiles still include Grandma Hannah & Matthias Richter.

Profile-driven channel formats, one tool — pick the profile in the UI:
- **classic** — English/German long-form philosopher narrations in
  the cited author's own voice (default Joseph Murphy — change per video via
  "cited thinker(s)"). Classic cheap format: NO on-camera avatar, pure voice-over
  b-roll with the Murphy cutout pinned bottom-right (`video.figure` in the
  profile; captions auto-clear it). Very stock-heavy and inexpensive. (See
  also **north-pro** for the modern full-pipeline version with animations,
  transitions, and particles.)
- **grandma-hannah** — English senior-health, warm disclosed-AI grandmother.
- **matthias-richter** — GERMAN philosophy/manifestation, first-person
  character narrator (63, studying the great thinkers since 2004). Per-video
  "cited thinker(s)" input drives whose authentic concepts carry the teaching
  (Murphy, Jung, …). 5 hooks, Sie + guillemets house style, fresh
  self-introduction after the hook, verbatim e-book blocks, honest
  buried-secret framing (no fabricated studies, no conspiracies). His HeyGen
  character and ai33 voice ("Matthias") are wired into the profile.
- **reddit** — ENGLISH, character-free original Reddit-style story. Enter the
  final title you want; it is locked exactly as submitted and production starts
  immediately with no web search, research, or title-selection gate. Stories
  remain 40k–70k characters.
- **astrology** — ENGLISH, character-free symbolic astrology. Enter the final
  title you want; it is locked exactly as submitted and production starts with
  no research/title-selection gate.
- **astrology-search** — the researched variant. Enter a theme; Terra prepares
  a bounded research brief and exactly three titles. Unverified transit/date
  claims are never invented; without verified dates the treatment remains
  evergreen, reflective, and non-deterministic.
- **cars** — researched general automotive engineering, history, comparisons,
  ownership, design, and driving culture.
- **carparts** — researched components, system operation, maintenance,
  diagnostic reasoning, replacement choices, and repair safety.
- **cars-modern** — researched model-year-2000+ vehicles, including combustion,
  hybrid and EV powertrains, software, ADAS, connectivity, and ownership tradeoffs.
- **cars-old** — researched pre-2000 classic/vintage vehicles, period engineering,
  model history, preservation, restoration, and original-versus-modified details.

The four car profiles accept the exact English title to publish and send it
straight into the normal Small (32k–47k character) production queue, while
retaining their profile-specific automotive accuracy and safety rules.

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
| Operations observer, chat, reports | OpenRouter | `qwen/qwen3.7-flash` (`/chat/completions`, reasoning enabled) | `OPENROUTER_API_KEY` |
| Thumbnail words | existing GPT provider | GPT-5.6 Terra (`/codex/v1/responses`) | `LLM_GPT_API_KEY` |
| Host on camera | HeyGen (your avatar) | `POST /v2/video/generate`, audio-driven | `HEYGEN_API_KEY` + `HEYGEN_AVATAR_ID` |
| Voice + subtitles | ai33.pro | `POST /v3/text-to-speech` (`with_transcript`) | `AI33_API_KEY` + optional `AI33_API_KEY_BACKUP` |
| AI b-roll | kie.ai Grok Imagine (cheapest; swappable) | `grok-imagine/text-to-video`, exact 6-30s | `KIE_VIDEO_API_KEY` |
| Stock photos | Pixabay images API | relevance-scored, Ken Burns | `PIXABAY_IMAGE_API_KEY` |
| Fallback AI images | kie.ai Z-Image | `z-image` | `KIE_API_KEY` |
| Music | ai33.pro Suno | `POST /v1s/task/music-generation` | `AI33_API_KEY` + optional `AI33_API_KEY_BACKUP` |
| Stock b-roll | Pixabay | videos API | `PIXABAY_API_KEY` |
| Animations | **local** (headless Chromium + FFmpeg) | `anim_templates/*.html` | — |

The **entire narration is voiced by ai33** (one consistent voice). HeyGen
only lip-syncs the on-camera segments from that same audio — so a 30-minute
video needs only ~3 minutes of HeyGen credits, not 30.

Set `AI33_API_KEY_BACKUP` to a second ai33 key to keep voice and music jobs
running when the primary explicitly reports exhausted credits, balance, or
quota. Authentication errors, network failures, and ordinary rate limits do
not trigger key switching. Each submitted task is polled with the same key
that created it. After a credit failover, new tasks stay on the backup until
the app restarts; restart after topping up the primary to probe it again.

## Run

```bash
cd ~/health-studio
./run.sh                 # venv + deps + server → http://127.0.0.1:5066
```

On Windows, double-click `run.bat` (or `powershell -ExecutionPolicy Bypass -File run.ps1`).
It does the same thing: venv, deps, Chromium, then starts the server.

Port note: 5066 is used for the stable controller address; Chrome refuses to
connect to 5060/5061 (SIP).

### Windows setup (render worker)

The pipeline runs on Windows as well as macOS — useful for putting a second
machine's CPU/GPU to work. One-time setup:

1. **Python 3.11+** from python.org — tick *"Add python.exe to PATH"* during install.
2. **ffmpeg on PATH**: `winget install Gyan.FFmpeg`, then open a **new** terminal
   so the updated PATH is picked up. `run.ps1` warns if it can't find ffmpeg.
3. `git clone` the repo — keep it on the **same commit** as your main machine, since
   profiles reference repo assets (`figures/`, `branding/`, `fonts/`, `anim_templates/`).
4. Run `run.bat`. It creates the venv, installs dependencies, and downloads Chromium
   (needed for the animation templates).
5. Copy `.env` across, or give the machine its own provider keys.
6. Enable long-path support if the repo lives in a deeply-nested folder
   (`LongPathsEnabled` in the registry, or via Group Policy).

Hardware encoding is detected automatically and is still H.264 either way —
VideoToolbox on a Mac, NVENC / QuickSync / AMF on Windows, falling back to
libx264 when no hardware encoder is present. On Apple Silicon, VideoToolbox
uses measured speed-priority/power-efficient hints, and `run.sh` keeps the Mac
awake during multi-hour renders without forcing the display to stay on.

## The workflow (per the Grandma Hannah script profile)

1. Enter a **title**, choose **Small** (a one-time random target from
   32,000–47,000 characters) or **Big** (180,000–210,000), set any optional
   flags, and click Add to queue. Each video gets a collision-checked
   20-digit ID. That ID remains its private local job folder; Drive and browser
   downloads use the publication title as the filename.
2. The writer delivers a **detailed outline + hooks**. The studio defaults
   to an auto-selected bold-statement hook so a title can run unattended.
   Switch Hook approval back to the manual gate when you want to pick the
   hook and edit the outline yourself.
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
   generates in parallel, and FFmpeg composites with burned subtitles and the
   music bed. The stitched file then goes through VidForge's complete final
   export pass: one continuous PCM audio master, a fresh constant-frame-rate
   H.264 High/yuv420p encode, AAC-LC stereo at 192 kbps, zero-based track
   timestamps, duration matching, and MP4 `faststart`. The delivered file is
   validated against that contract before it can be marked complete, which
   keeps it compatible with Spotify for Creators.
5. For thumbnail-enabled profiles, GPT-5.6 Terra proposes thumbnail words in
   parallel. Rendering does not
   wait for thumbnail approval. It resolves the script language from the UI
   override or profile, rewrites/translates the publication title into that
   language, and proposes different (non-repeating) thumbnail text using the
   truthful life-change trigger rules. Approve/edit the title and words first;
   only then is the image generated. Approve that image separately. The
   thumbnail gets its own 20-digit ID and remains visible/downloadable from
   the video job.
   Astrology Search and all four car profiles add an earlier gate: Terra first
   researches the theme and presents exactly three titles. The video enters the
   render queue only after one title is selected. Thumbnail generation is fully
   disabled for `astrology`, `astrology-search`, `reddit`, `cars`, `carparts`,
   `cars-modern`, and `cars-old`; those jobs show no thumbnail approval card and
   make no thumbnail-provider calls.
6. When the Spotify CTA is enabled, choose either the standard localized
   card or the dedicated **GEISTMEISTER** card. The standard card uses timed
   windows; GEISTMEISTER stays visible for the entire video and also names
   the artist in the script cue.

Outline, hooks, and the final script are saved in the job folder
(`output/<job>/outline.txt`, `hooks.json`, `script.txt`).

## AI operations center

The right side of the studio contains a Qwen 3.8 27B observer via
OpenRouter, with GPT-5.6 Terra as its explicit fallback. Neither provider can
fall through to the local Claude CLI. Once per
minute it receives a sanitized snapshot of queue states, recent job events,
worker heartbeats/capacity, Drive readiness, encoder information, and free
disk space. It writes a quiet update to `output/operations_log.jsonl`.

Watcher requests enable OpenRouter reasoning with a small private reasoning
budget, exclude reasoning details from the response/log, and use non-streaming
responses because each health tick must produce one complete JSON object.
Ticks and owner questions are independent one-turn requests, so there is no
cross-turn reasoning state to persist or replay.

The model has no mutation tools or secret/environment access. The host grants
the operations center one deterministic exception: when a sanitized job error
explicitly reports AI33 credit/quota exhaustion, it may persistently prefer the
configured backup AI33 account for new tasks. It never receives either key,
and task polling stays pinned to the account that created each task. It cannot
stop/retry jobs, edit any other setting/file, restart workers, or send messages.
Broad/uncertain failures cause a local Markdown report under `output/reports/`;
reports are downloadable but never sent.

Description generation is intentionally parked in `waiting_for_rules` until
the channel-specific description template, links, CTA, disclosure, language,
hashtags, and forbidden wording are supplied.

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
- Kie task IDs are persisted in each job's `plan.json` before collection, so
  restarts resume polling instead of submitting replacements. Actual provider
  credit reports are appended to `output/kie_usage.jsonl`. Last-resort paid AI
  stills are capped at three per video by default; change
  `KIE_FALLBACK_IMAGE_LIMIT` or a profile's `video.kie_fallback_image_limit`.
- Animations are free (rendered locally).
- Test with the `test-short` profile first.

## Google Drive deliverables

When `GDRIVE_CLIENT_SECRET_FILE` points to a Google **Desktop OAuth client**
JSON, finished videos upload below an app-owned `videos` folder in Drive. Each
profile has its own child folder, and each video is named from its title:

```text
videos/
  cars/
    How Modern Cars See the Road.mp4
    Titles and Thumbnails.docx
  reddit/
    The Family Secret I Found Too Late.mp4
    Titles and Thumbnails.docx
```

Despite its requested filename, `Titles and Thumbnails.docx` contains titles
only—one plain paragraph per unique finished video, with no images, dates, IDs,
or metadata. It is updated only after the corresponding video has uploaded and
verified successfully. A duplicate title in the same profile produces a naming
error before production; an existing Drive filename is never overwritten.

The first time, after restarting the app, use **Connect** in the Drive status
panel or open:

```
http://localhost:5066/api/drive/authorize
```

Approve the Google prompt. The refresh token is saved in the gitignored
`output/gdrive_token.json`; every later upload refreshes automatically. The
uploader streams 8 MB chunks, resumes a valid interrupted session, verifies
the remote size/checksum, and only then deletes the local deliverable. Drive
files are private—both free and Patreon parts are served through this app's
normal authenticated video route, never an "anyone with the link" share.

The panel shows the connected account and provides **Change** and **Disconnect**.
Changing accounts revokes and removes the old local token before opening a fresh
Google consent flow, so cached folder IDs from one account cannot leak into the
next account.

If a render completed before authorization or an upload temporarily fails,
the local video is retained. Retry it with:

```
curl -X POST http://localhost:5066/api/drive/retry/JOB_ID
```

## Render fleet (Mac main + Windows worker)

Install Tailscale on both machines and confirm the Mac can reach the Windows
machine through its `100.x.y.z` tailnet address. The worker only polls the
main; no inbound port needs opening on Windows and no video file crosses the
home internet connection—finished videos upload directly to Drive.

On the Mac, set `FLEET_ROLE=main`, a strong `FLEET_PAIRING_TOKEN`, and
`ACCESS_KEYS`; bind `HOST` to the Mac's Tailscale address. On Windows, use the
same token, set `FLEET_ROLE=worker`, and point
`FLEET_MAIN_URL` at `http://<mac-tailscale-ip>:5066`. Start it with `run.bat`.
The Mac keeps rendering while it has capacity; when it is full, a waiting job
is dispatched whole to the Windows worker. Hook approval still appears in the
Mac UI. The worker needs its own local `.env`, provider keys, and one-time
Drive authorization.

The **Worker control** panel on the main shows each machine's last heartbeat,
CPU/RAM/encoder, disk space, active and pending jobs, and effective slots. Use
**Active** to accept work, **Drain** to finish current work while refusing new
jobs, or **Pause** for maintenance. The per-worker slot selector is a live
controller cap and survives main-app restarts. Pending dispatches that have not
started can be safely released to the queue; after a worker acknowledges a job,
use **Stop** instead so the same render can never run on two machines. Job rows
also have a worker picker for explicit placement. Worker protocol v2 mirrors
the policy locally and prunes acknowledged terminal reports, while the main
still enforces lifecycle and capacity for older workers.

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

---

## Setup on a new machine (friend edition)

1. **Requirements**: macOS or Linux with Python 3.9+, `ffmpeg` on PATH
   (`brew install ffmpeg` on a Mac), and ~10 GB free disk.
2. Clone this repo, then:
   ```bash
   ./run.sh
   ```
   First run creates the venv, installs Python deps and the headless
   Chromium used for animation templates.
3. **Keys**: copy `.env.example` to `.env` and fill in the values —
   ask North for the current key set (NEVER commit `.env`; it's
   git-ignored). All generation (voice, avatar, stock, LLM) bills to
   these keys, so coordinate before big renders.
4. Open http://127.0.0.1:5066, pick the profile (matthias-richter for
   the German channel), enter a title, Generate. The gate pauses for
   your hook choice; the video lands in `output/<job>/final.mp4`
   (add `?download=1` to the video URL for a download).

Notes:
- If a job dies (disk, credits, crash), free the blocker and POST
  `{"resume_job": "<job id>"}` to `/api/render` — everything already
  generated is reused, nothing is re-billed.
- Videos are ~1 GB/hour of runtime; old job folders can be deleted
  once the final is saved elsewhere.
