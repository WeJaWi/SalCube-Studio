"""Waveform extraction and overlay generation for Health Studio.

Adapted / simplified from Wexilo's sound_waves + waveformr_bridge (Qt-free).

Provides:
- Audio sample extraction (pure ffmpeg)
- Simple waveform data for animation templates
- Ability to generate a static or animated waveform overlay video

Health Studio already has a powerful HTML+Playwright animation system.
The recommended way to use waveforms is either:
  1. Use the built-in ffmpeg `showwaves` filter (fast, no extra code)
  2. Or feed waveform data into a new anim_template
"""

from __future__ import annotations

import array
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import config


@dataclass
class WaveformData:
    samples: List[float]          # normalized 0.0 - 1.0
    duration: float
    sample_rate: int = 22050


class WaveformError(RuntimeError):
    pass


def which_ffmpeg() -> Optional[str]:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return shutil.which(exe)


def which_ffprobe() -> Optional[str]:
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    return shutil.which(exe)


def _ffmpeg() -> str:
    path = which_ffmpeg()
    if not path:
        raise WaveformError("FFmpeg is required for waveform generation.")
    return path


def extract_waveform_data(
    audio_or_video: str | Path,
    *,
    samples: int = 200,
    sample_rate: int = 22050,
    scale: str = "lin",          # "lin" | "sqrt" | "log"
    smooth_radius: int = 1,
) -> WaveformData:
    """Extract normalized waveform amplitude data using ffmpeg."""
    ffm = _ffmpeg()
    path = str(audio_or_video)

    # Get duration
    dur_cmd = [which_ffprobe() or "ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
    dur_out = subprocess.run(dur_cmd, capture_output=True, text=True)
    try:
        duration = float(dur_out.stdout.strip())
    except Exception:
        duration = 0.0

    # Decode to float32 mono
    cmd = [
        ffm,
        "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise WaveformError((proc.stderr.decode("utf-8", "ignore") or "Failed to decode audio").strip())

    floats = array.array("f")
    floats.frombytes(proc.stdout)
    if not floats:
        raise WaveformError("No audio samples decoded.")

    # Block averaging
    block_size = max(1, len(floats) // max(1, samples))
    values: List[float] = []
    for i in range(samples):
        start = min(len(floats), i * block_size)
        end = min(len(floats), start + block_size)
        if start >= end:
            values.append(0.0)
            continue
        total = sum(abs(f) for f in floats[start:end])
        values.append(total / (end - start))

    # Normalize + shape
    peak = max(values) if values else 0.0
    if peak > 0:
        values = [v / peak for v in values]
    else:
        values = [0.0] * len(values)

    if scale == "sqrt":
        values = [math.sqrt(v) for v in values]
    elif scale == "log":
        values = [math.log1p(v * 9.0) / math.log(10.0) for v in values]

    # Simple moving average smoothing
    if smooth_radius > 0 and len(values) > 2:
        smoothed: List[float] = []
        total = 0.0
        q: List[float] = []
        r = smooth_radius
        for v in values:
            q.append(v)
            total += v
            if len(q) > r * 2 + 1:
                total -= q.pop(0)
            smoothed.append(total / len(q))
        values = smoothed

    return WaveformData(samples=values, duration=duration, sample_rate=sample_rate)


def generate_waveform_overlay_video(
    audio_or_video: str | Path,
    output_path: str | Path,
    *,
    width: int = 1920,
    height: int = 200,
    waveform_type: str = "bars",   # "bars" | "mirror" | "steps"
    samples: int = 200,
    color: str = "#0a9396",
    bg_color: str = "#001219",
    fps: int = 30,
    duration: Optional[float] = None,
) -> Path:
    """
    Generate a simple animated waveform overlay video using ffmpeg's showwaves filter.
    This is the fastest, dependency-light path and works great with Health Studio.
    """
    ffm = _ffmpeg()
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    dur = duration or extract_waveform_data(audio_or_video, samples=1).duration or 10.0

    # Use showwaves for real-time animated waveform (very reliable).
    # The audio is input 1 (input 0 is the lavfi background color, video-only);
    # showwaves outputs RGBA, so overlay it on the solid background.
    # showwaves has no "bars"/"mirror"/"steps" — map friendly names to
    # its real modes (point, line, p2p, cline).
    mode = {"bars": "cline", "mirror": "p2p", "steps": "point",
            "line": "line"}.get(waveform_type, "cline")
    filter_complex = (
        f"[1:a]showwaves=s={width}x{height}:mode={mode}:"
        f"rate={fps}:colors={color}[w];"
        f"[0:v][w]overlay=format=auto,format=yuv420p[wave]"
    )

    cmd = [
        ffm, "-y",
        "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
        "-i", str(audio_or_video),
        "-filter_complex", filter_complex,
        "-map", "[wave]",
        "-map", "1:a?",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(out),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WaveformError("ffmpeg showwaves failed:\n" + (proc.stderr[-800:] or proc.stdout[-800:]))

    return out


def render_waveform_pngs(
    audio: str | Path,
    out_base: str | Path,
    *,
    width: int,
    height: int,
    base_color: str = "0xFAF6EF@0.32",
    played_color: str = "0xFAF6EF@0.95",
) -> Tuple[Path, Path]:
    """Render two static full-clip waveform PNGs (RGBA, transparent bg):
    a dim 'base' layer and a bright 'played' layer.

    The classic layout overlays the dim layer whole and reveals the bright
    layer left-to-right as the narration plays — a modern podcast-style
    progress waveform, all precomputed so the main render stays one pass.
    """
    ffm = _ffmpeg()
    out_base = Path(out_base)
    dim = out_base.with_name(out_base.name + "_wave_dim.png")
    lit = out_base.with_name(out_base.name + "_wave_lit.png")
    for path, colors in ((dim, base_color), (lit, played_color)):
        cmd = [
            ffm, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio),
            "-filter_complex",
            f"aformat=channel_layouts=mono,"
            f"showwavespic=s={width}x{height}:colors={colors}:"
            f"draw=full:filter=peak",
            "-frames:v", "1", str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not path.exists():
            raise WaveformError("showwavespic failed:\n" + proc.stderr[-400:])
    return dim, lit


def generate_waveform_data_json(audio_or_video: str | Path, samples: int = 300) -> dict:
    """Return JSON-serializable waveform data (useful for HTML animation templates)."""
    data = extract_waveform_data(audio_or_video, samples=samples)
    return {
        "samples": data.samples,
        "duration": data.duration,
        "sample_count": len(data.samples),
    }


# --- Optional: copy of vendor for advanced use (if Deno is installed) ---

def has_waveformr_vendor() -> bool:
    vendor = config.ROOT / "vendor" / "waveformr" / "apps" / "api" / "lib" / "svg_path.ts"
    return vendor.exists()


def waveformr_vendor_path() -> Path:
    return config.ROOT / "vendor" / "waveformr"
