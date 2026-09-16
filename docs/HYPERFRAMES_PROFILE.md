# HyperFrames-only video profile

The `hyperframes` profile creates every visible frame from HTML, CSS, vector
shapes, typography, and deterministic GSAP timelines. It does not call stock,
AI-image, AI-video, or avatar providers. Voice generation remains the only
media-generation step; the visual authoring model returns constrained JSON,
never executable code.

## Requirements

- Node.js 22 or newer
- `npx`, FFmpeg, and FFprobe on `PATH`
- HyperFrames CLI 0.8.41 (invoked through the pinned `npx` package version)
- Local Georgia and Arial fonts

The app checks the local runtime before accepting a HyperFrames job. The job is
rejected before paid authoring work if the toolchain or profile safety contract
is invalid.

## Pipeline

1. The normal script workflow produces the approved narration.
2. Voiceover is generated first and merged into one authoritative audio master.
3. Semantic narration spans become exact-duration scenes.
4. A conservative Kie authoring reservation is calculated, persisted, and
   compared with the profile ceiling before any authoring request is submitted.
5. The authoring model proposes a constrained scene blueprint. A deterministic
   fallback covers missing or rejected fields.
6. The local compiler escapes all text and creates the executable HTML/GSAP
   composition. It rejects remote I/O, iframes, dynamic code, wall-clock time,
   unseeded randomness, and infinite repeats.
7. HyperFrames checks runtime behavior, layout, transition frames, captions,
   contrast, and snapshots before rendering.
8. The rendered MP4 passes through the existing VidForge final export contract.

## Per-job artifacts

The job's `hyperframes/` directory retains:

- `DESIGN.md` — human-readable visual identity
- `authoring-budget.json` — pre-submission cost reservation and ceiling
- `video-blueprint.json` — deterministic scene and animation data
- `timeline.json` — resolved scene timings in seconds
- `index.html` — executable composition source
- `hyperframes.json` and `package.json` — pinned project/runtime configuration
- `validation-report.json` and `snapshots/` — render QA evidence
- versioned input/output manifests used for safe resume

The final video remains at the normal job path, `final.mp4`.

## Cost behavior

Rendering the HTML composition does not consume Kie image or video credits.
Kie usage is limited to text-model authoring when a Kie-backed script model is
selected. The default profile reserves a conservative ceiling of 1,000 credits
and refuses to submit requests that would exceed it. Actual planning for a
typical eight-minute video should be far below that ceiling; each job records
the reservation in `authoring-budget.json` before submission.

## Recovery and reproducibility

The blueprint seed is derived from the narration, visual identity, and engine
version. Artifact manifests hash their authoritative inputs. A resumed job
reuses a matching blueprint and regenerates only invalid downstream output.
CLI processes have explicit timeouts and are registered with the existing job
cancellation system.
