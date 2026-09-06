"""Background music via ai33.pro Suno (/v1s/task/music-generation).

Suno tracks are only ~2-4 min, so for long videos we generate several
distinct tracks, stitch them into one bed, and let the render loop that
whole set across the video (variety instead of one song on repeat).

Jobs are submitted early and collected later so Suno generates in
parallel with the TTS/image work. Music is optional/graceful.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

from . import ai33_tasks, cancel, clients, config, render, telemetry


def submit_music(prompt: str, n_tracks: int = 5, *, workdir: Optional[Path] = None) -> List[str]:
    """Kick off `n_tracks` Suno instrumental generations; return task_ids."""
    if (not (prompt or "").strip()
            or not (config.AI33_API_KEY or config.AI33_API_KEY_BACKUP)):
        return []
    task_ids: List[str] = []
    for i in range(max(1, n_tracks)):
        cancel.check()
        try:
            task_ids.append(clients.ai33_submit_json("/v1s/task/music-generation", {
                "create_mode": "simple",
                "gpt_description_prompt": prompt[:500],
                "make_instrumental": True,
            }, ledger_path=(workdir / f"music_{i:02d}.task.json") if workdir else None))
        except (cancel.JobCancelled, ai33_tasks.SubmissionUncertain, ai33_tasks.UsageLimitReached):
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[music] submit error: {e}")
    return task_ids


def _audio_url(meta: dict) -> Optional[str]:
    if meta.get("audio_url"):
        return meta["audio_url"]
    clips = (meta.get("suno_result") or {}).get("clips") or []
    if clips and clips[0].get("audio_url"):
        return clips[0]["audio_url"]
    urls = meta.get("all_audio_urls") or []
    return urls[0] if urls else None


@telemetry.timed("music_collection")
def collect_music(task_ids: List[str], dest: Path, workdir: Path,
                  timeout_s: int = 900) -> Optional[Path]:
    """Poll each Suno task, download the tracks, stitch into one bed.

    Returns `dest` (the stitched bed) or None if nothing succeeded.
    """
    if not task_ids:
        return None
    tracks: List[Path] = []
    for i, tid in enumerate(task_ids):
        try:
            cancel.check()
            task = clients.ai33_poll(tid, timeout_s=timeout_s)
            url = _audio_url(task.get("metadata", {}) or {})
            if url:
                tracks.append(clients.download(url, workdir / f"music_{i:02d}.mp3"))
        except cancel.JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[music] track {i} failed: {e}")

    if not tracks:
        return None
    if len(tracks) == 1:
        tracks[0].replace(dest)
        return dest

    # Concatenate the distinct tracks into one bed (re-encode for uniformity).
    lst = workdir / "music_list.txt"
    lst.write_text("".join(f"file '{t.resolve().as_posix()}'\n" for t in tracks))
    try:
        render._run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:a", "libmp3lame", "-b:a", "192k", str(dest)])
        return dest
    except cancel.JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[music] stitch failed ({e}); using first track")
        tracks[0].replace(dest)
        return dest
