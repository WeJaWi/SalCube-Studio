# Dmiqwi: next improvement plan

Prepared 5 September 2026 from the local code; implementation status updated below.

## Implementation status — 5 September 2026

Implemented locally: strict narration-export validation; per-attempt timing and
cache counters; merged final-pass popup compositing (legacy switch retained);
bounded beat-search prefetch; saved B-roll source history, curator caching and
review/replace controls; input-sensitive narration/B-roll/render manifests;
shared encode/memory/final-disk budgets; protocol-3 attempts, persisted worker
reports and cache-aware dispatch; AI33 task persistence and per-machine usage
limits; distinct render/delivery status with confirmed upload progress and
restart recovery. New split renders require both verified final parts.

Also fixed the reported Word-thumbnail loss: existing DOCX contents are retained
while new titles are appended. Non-document package parts are byte-identical,
duplicate titles do not rewrite the file, a downloaded recovery copy is kept,
and detected concurrent edits cause a retry instead of an overwrite.

Validation includes isolated provider/worker failure simulations, short real
FFmpeg popup/audio comparisons, and a visually inspected Word-image fixture.
These checks do not establish full-length speedups or Windows production
behavior. Still pending: representative long-video Mac/Windows comparisons,
manual B-roll relevance scoring, history-based adaptive routing, disconnected
worker reconciliation across controller restarts before reassignment, preview
lookahead beyond candidate search, and invariant end-clip cache benchmarking.
Resource limits cover shared FFmpeg work and final-export disk reservations,
not a complete admission model for every preparation asset. AI33 limits are
per machine; fleet-wide billing aggregation is not implemented. The existing
persisted job queue resumes delivery, with one transfer at a time, rather than
a separate delivery service. Old workers need an update/restart for new logic.

## Objective

Reduce time from queued job to verified deliverable, improve stock-footage relevance, and make worker activity and spending easier to control. Preserve the current VidForge final export settings that were adopted for Spotify compatibility.

The previous round already added FFprobe caching, safer transition caching, concurrent searches across stock providers, bounded stock caches, atomic downloads, and cancellation/retry fixes. This plan builds on those changes. Drive already supports resumable uploads and remote verification.

## Release 1: reliable exports and faster rendering

### 1. Prevent silent narration loss — first priority

Evidence: [final audio extraction](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/render.py:626>) catches a failure and generates a silent audio master. Stream validation can then accept that output even though narration is missing.

- Treat failed narration extraction as a recoverable export error. Rebuild from validated narration assets where possible; otherwise retain intermediates and report the problem.
- Permit silent output only for an explicitly silent project.
- Check narration coverage and suspicious long silent sections, accounting for intentional pauses. Make these checks distinct from codec/container validation.

Acceptance: injecting an audio-extraction failure cannot produce a completed, silent narrated video or trigger its upload. Valid narrated and deliberately silent fixtures retain their intended behavior.

### 2. Measure stages, then eliminate the separate popup encode

Evidence: [render statistics](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/stats.py>) stores aggregate render times. [Popup processing](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/render.py:1683>) applies the popup after stitching, and `encode_final_export` subsequently encodes the full video again.

- Record timings for preparation, narration, stock search, curation, download, beat rendering, transitions, final encoding, and upload. Separate queue time and cached resumes from fresh work.
- Record output duration, profile, worker, encoder, peak memory, and cache hits so comparisons reflect the same workload.
- Add popup compositing to the final export pass. Preserve the final video/audio settings and the existing popup placement and timing, including both Patreon halves and the end clip.
- Evaluate reusable normalized end clips and invariant overlay assets after measuring their cost.

Acceptance: representative popup videos use one fewer full-length video encode, pass the existing export checks, and match approved frames at popup boundaries and split/end-clip boundaries. Report total elapsed time and temporary disk usage against the baseline; do not extrapolate the earlier short-process benchmark to whole videos.

## Release 2: faster and more relevant B-roll

### 3. Prefetch stock for multiple beats

Evidence: the providers for a single query run concurrently, but the [stock/animation loop](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/orchestrator.py:917>) still handles beats sequentially.

- Prefetch candidate lists and previews for a small window of upcoming beats; tune the window using measured network and memory use.
- Keep final selection and asset reservation in beat order so concurrency does not introduce duplicate clips or unpredictable choices.
- Bound downloads and curator requests separately, respect provider backoff, and propagate Stop to all pending work.
- Keep expensive animation work from blocking unrelated stock lookups.

Acceptance: a fixed stock-heavy test video prepares faster with equivalent selection rules, no additional paid requests, and no repeat-limit violations. Stop leaves no pending work able to submit paid fallback requests.

### 4. Improve matching and preserve selection history

Evidence: the [curator](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/curator.py>) already judges metadata or thumbnails, but uses a separate request/retry path. `used_media` starts empty after assets are restored, so resumed clips do not contribute their provider IDs to repeat prevention.

- Persist each selected asset's provider ID, source, query, relevant metadata, and beat assignment; restore repeat counts on resume.
- Add profile-specific matching rules: distinguish car parts and vehicle eras, and distinguish literal versus abstract imagery for astrology and psychology.
- Try bounded alternative queries before paid image fallback. Avoid depicting an unverified specific component as an exact match.
- Cache candidate previews and contextual curator results using narration, candidate IDs, model, and selection-rules version. Reapply current repeat limits before reuse.
- Route curation through the shared cancellation, retry, and usage controls. Use richer frame inspection only where thumbnail selection demonstrably fails and its cost is justified.
- Add an optional contact-sheet preview with replace/retry controls for weak matches.

Acceptance: compare old and new selection on the same approved narration sample. Track relevant matches, repeated shots, blank cards, preparation time, and paid fallback calls. Stop/resume preserves repeat limits. Asset captions and metadata are treated as source data, not instructions.

### 5. Make every resume cache depend on its inputs

Evidence: [TTS reuse](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/voiceover.py:132>) checks playable indexed files, while [beat reuse](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/render.py:1600>) checks media validity and duration. Neither check fully identifies the script, voice, visual source, or render settings that produced the file.

- Add versioned manifests for narration groups, narration slices, selected B-roll, beat composites, and final exports.
- Include text, voice/model/settings, group membership, source signatures, captions, layout, dimensions, frame rate, and relevant overlays at the appropriate stage.
- Invalidate only dependent outputs. For example, a caption-style change should rerender affected visual stages while retaining valid narration and downloads.
- Treat old files without manifests conservatively and explain what needs rebuilding before incurring new provider costs.

Acceptance: unchanged resumes reuse valid work; changed voice, text, captions, and source assets cannot reuse stale dependent outputs. Simulate interrupted manifest writes and file replacement.

## Release 3: worker throughput and operating costs

### 6. Schedule against each worker's real resources

Evidence: [concurrency configuration](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/config.py:203>) independently sizes jobs and beat workers from fixed heuristics. [Fleet dispatch](</Users/kornelborecky/Documents/FOLDER APP IDL/app.py:758>) sends overflow after local slots fill. The heartbeat reports hardware and capacity, but no application revision or job-attempt identity.

- Introduce one resource budget per machine shared by all jobs: active encodes, available memory, and reserved temporary disk space.
- Separate preparation, encoding, and upload capacity. Measure whether a worker can safely prepare two jobs while encoding one or more beats.
- Route jobs using available resources, compatible encoder/features, profile history, and existing cached assets; keep manual worker assignment.
- Explain queue decisions in the UI, such as “waiting for encoder capacity” or “second job preparing.”
- Include application revision and per-run attempt IDs in dispatches and reports; persist acknowledgements so stale completions cannot overwrite a newer run. Reconcile disconnected workers before reassignment to avoid duplicate execution.
- Retain existing pause, drain, and capacity controls and deploy worker changes with protocol compatibility.

Acceptance: test two simultaneous Windows jobs, memory pressure, delayed/repeated heartbeats, controller/worker restarts, stale completion reports, and worker disconnects. Capacity remains bounded and each attempt has one authoritative outcome.

### 7. Persist AI33 tasks and record real usage

Evidence: [music generation](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/music.py>) returns submitted task IDs in memory, and the orchestrator only reuses music once `music.mp3` exists. TTS submits and polls before saving the audio. [Cost statistics](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/stats.py>) labels its cost table as an estimate.

- Extend the existing persistent paid-task pattern to AI33 narration and music. Save submission intent, task IDs, provider/account alias, state, and resulting files.
- On restart, resume known tasks. Surface uncertain submissions for reconciliation rather than blindly submitting again.
- Track submitted characters, confirmed provider usage where available, model, job, and profile. Clearly distinguish actual usage from estimated costs.
- Add daily/per-job limits and report primary/backup account usage using aliases, never secret values.

Acceptance: crash/restart tests around submission and download do not duplicate known accepted requests. Unknown acceptance remains explicit. Daily/profile totals reconcile with captured usage records, and no key is written to reports.

### 8. Separate render completion from delivery status

Evidence: [job completion](</Users/kornelborecky/Documents/FOLDER APP IDL/app.py:1250>) enters upload after rendering and later sets job status to done while delivery has its own state. [Drive storage](</Users/kornelborecky/Documents/FOLDER APP IDL/pipeline/storage.py>) already resumes chunks and verifies the remote file.

- Display “render ready,” “uploading,” “upload failed,” and “delivered” distinctly, including separate status for free and Patreon files.
- Report confirmed bytes, throughput, retry/backoff state, and upload ETA; distinguish permission/quota issues from slow transfer.
- Use bounded upload workers and a persisted delivery queue so slow uploads do not monopolize preparation/render capacity or require a rerender.
- Coordinate retained output size with the machine's disk budget and existing verified-upload cleanup behavior.

Acceptance: interrupted delivery resumes correctly, failed uploads retain usable local outputs, and a completed render can release compute resources while delivery continues. A job is shown as delivered only when every expected file has been verified remotely.

## Validation and rollout

1. Capture baseline timings using reusable approved assets: a stock-heavy car video, a classic narration video, and a Patreon split with popups. Include a long timeline to expose memory and disk behavior.
2. Land the audio failure fix and timing instrumentation first. Then implement the popup-pass merge, stock prefetch, matching/history, and cache manifests.
3. Validate worker scheduling and task persistence using simulated failures before a small real Mac/Windows batch. Publish application revisions so both machines' behavior is visible.
4. Compare fresh runs and resumed runs separately. Measure total time, stage time, peak memory, disk usage, B-roll acceptance, and provider calls.
5. Keep the current rendering path available during the optimized-path comparison. Promote changes only after export, visual, audio, and cancellation checks pass.

The best immediate speed candidates are the popup-pass merge and stock prefetch. Their end-to-end benefit is not yet measured. Adaptive scheduling and changing worker lifecycle are larger follow-on work and should build on the timing data.
