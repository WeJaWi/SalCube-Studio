# Ports from Wexilo Desktop App

This folder contains adapted (Qt-free) code ported from `wexilo_desktop_v3` into Health Studio.

## Backup
Original code was backed up to: `FOLDER APP IDL.backup-2026-07-14_104826`

## What was ported

### 1. Job Management (`pipeline/jobs.py`)
- `JobManager` (singleton)
- `ManualJob` — for long-running orchestrator work (the main render pipeline)
- `BackgroundProcessJob` — for ffmpeg jobs with progress parsing (`-progress pipe:1`)
- `JSONLogWriter` — structured `.jsonl` logs under `logs/`
- `run_cancellable()` helper
- `WorkflowCancelled` exception

**Integration:**
- `app.py` now creates a manual job for every render and logs progress through it.
- You can access logs via the job manager.

### 2. Captions / Subtitles (`pipeline/captions.py`)
Pure functions only:
- `Segment`
- `parse_srt(text)` / `parse_vtt(text)`
- `build_ass(segments, out_path, ...)` — generates ASS files
- `seconds_to_ass()`
- `get_video_duration()`, `get_video_size()`, `has_audio_stream()`
- `extract_audio_for_transcription()`

The existing render pipeline still uses its own ASS styling, but you can now use these helpers for custom transcripts or post-processing.

### 3. Waveform (`pipeline/waveform.py`)
- `extract_waveform_data(audio_path, samples=200)` → normalized amplitude list + duration
- `generate_waveform_overlay_video(...)` — uses ffmpeg `showwaves` (fast, reliable)
- `generate_waveform_data_json(...)` — perfect for feeding into HTML animation templates
- `has_waveformr_vendor()` + path to the advanced Deno/TS renderer (copied to `vendor/waveformr`)

**Recommended usage in Health Studio:**
```python
from pipeline import waveform

# Get data for an animation template or planning
data = waveform.extract_waveform_data("output/xxx/assets/music.mp3", samples=180)

# Or generate a ready overlay video
overlay = waveform.generate_waveform_overlay_video(
    "output/xxx/assets/music.mp3",
    "output/xxx/assets/waveform.mp4",
    width=1920, height=300, waveform_type="bars"
)
```

### Vendor
`vendor/waveformr/` was copied. It contains a Deno-based waveform renderer if you ever want higher-fidelity SVG waveforms.

## How to use the new Job system

```python
from pipeline import jobs
jm = jobs.JobManager.instance()

jid = jm.start_manual_job({"tool": "my_step"}, tag="render")
jm.manual_log(jid, "working...")
jm.manual_progress(jid, 55)
jm.manual_finish(jid, 0)
```

The main video generation already uses it.

## Notes
- All Qt / PyQt5 / UI code was stripped.
- No new hard dependencies were added.
- The ports preserve the useful logging + progress + cancellation patterns.
