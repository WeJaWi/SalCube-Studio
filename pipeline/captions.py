"""Pure caption / subtitle utilities for Health Studio.

Adapted from Wexilo's tools/captions.py (Qt-free parts only).

Provides:
- Segment dataclass
- parse_srt / parse_vtt
- build_ass (ASS subtitle file generator)
- Various ffmpeg probe helpers
- seconds_to_ass
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import config


@dataclass
class Segment:
    start: float
    end: float
    text: str


def which_ffmpeg() -> Optional[str]:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return shutil.which(exe)


def which_ffprobe() -> Optional[str]:
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    return shutil.which(exe)


def _run(cmd: List[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def get_video_duration(path: str | Path) -> Optional[float]:
    probe = which_ffprobe()
    if not probe:
        return None
    code, out, _ = _run([probe, "-v", "error", "-show_entries", "format/duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    if code != 0:
        return None
    try:
        return float(out.strip())
    except Exception:
        return None


def get_video_size(path: str | Path) -> Optional[Tuple[int, int]]:
    probe = which_ffprobe()
    if not probe:
        return None
    code, out, _ = _run([probe, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height",
                         "-of", "csv=p=0:s=x", str(path)])
    if code != 0:
        return None
    out = out.strip()
    if "x" in out:
        try:
            w, h = out.split("x")
            return int(w), int(h)
        except Exception:
            return None
    return None


def has_audio_stream(path: str | Path) -> bool:
    probe = which_ffprobe()
    if not probe:
        return False
    code, out, _ = _run([probe, "-v", "error", "-select_streams", "a:0",
                         "-show_entries", "stream=codec_type",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return code == 0 and "audio" in out.strip().lower()


# --- Parsing -----------------------------------------------------------------

SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")
VTT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})")


def _time_to_seconds(m: re.Match) -> float:
    return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0


def parse_srt(text: str) -> List[Segment]:
    segs: List[Segment] = []
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    for b in blocks:
        lines = [ln.strip("\ufeff") for ln in b.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        if "-->" in lines[0]:
            times = lines[0]
            words = lines[1:]
        else:
            if len(lines) < 2:
                continue
            times = lines[1]
            words = lines[2:]
        tparts = times.split("-->")
        if len(tparts) != 2:
            continue
        m1 = SRT_TIME.search(tparts[0])
        m2 = SRT_TIME.search(tparts[1])
        if not (m1 and m2):
            continue
        start = _time_to_seconds(m1)
        end = _time_to_seconds(m2)
        txt = " ".join(words).strip()
        txt = re.sub(r"<.*?>", "", txt)
        segs.append(Segment(start, end, txt))
    return segs


def parse_vtt(text: str) -> List[Segment]:
    segs: List[Segment] = []
    text = re.sub(r"^WEBVTT.*?\n", "", text, flags=re.IGNORECASE | re.DOTALL)
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    for b in blocks:
        lines = [ln.strip("\ufeff") for ln in b.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        if "-->" not in lines[0]:
            if len(lines) < 2:
                continue
            times = lines[1]
            words = lines[2:]
        else:
            times = lines[0]
            words = lines[1:]
        tparts = times.split("-->")
        if len(tparts) != 2:
            continue
        m1 = VTT_TIME.search(tparts[0])
        m2 = VTT_TIME.search(tparts[1])
        if not (m1 and m2):
            continue
        start = _time_to_seconds(m1)
        end = _time_to_seconds(m2)
        txt = " ".join(words).strip()
        txt = re.sub(r"<.*?>", "", txt)
        segs.append(Segment(start, end, txt))
    return segs


def seconds_to_srt(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:  # rounding can carry a full second
        ms -= 1000
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: List[Segment], dest: str | Path) -> None:
    """Write .srt cues, renumbered from 1 — the inverse of parse_srt().
    Used to slice a large multi-beat transcript's cues into a per-beat
    file with re-zeroed timestamps."""
    dest = Path(dest)
    if not segments:
        dest.write_text("", encoding="utf-8")
        return
    lines: List[str] = []
    for n, s in enumerate(segments, 1):
        lines += [str(n), f"{seconds_to_srt(s.start)} --> {seconds_to_srt(s.end)}",
                 s.text, ""]
    dest.write_text("\n".join(lines), encoding="utf-8")


def seconds_to_ass(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int((t - int(t)) * 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{").replace("}", "\\}")
    text = text.replace("\r\n", "\\N").replace("\n", "\\N").replace("\r", "\\N")
    return text


def build_ass(
    segments: List[Segment],
    out_path: str | Path,
    *,
    play_res: Tuple[int, int] = (1920, 1080),
    font_name: str = "Arial",
    font_size: int = 17,
    primary_colour: str = "&H00FFFFFF",
    outline: int = 1,
    shadow: int = 0,
    alignment: int = 2,
    margin_v: int = 20,
    margin_l: int = 25,
    margin_r: int = 25,
    back_colour: str = "&H90000000",
    blur: int = 0,
    border_style: int = 1,
    bold: bool = False,
) -> None:
    """Write a simple ASS file compatible with Health Studio render."""
    play_res_x, play_res_y = play_res
    event_prefix = f"{{\\blur{blur}}}" if blur else ""

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: HealthStudio,{font_name},{font_size},{primary_colour},&H000000FF,"
        f"&H00000000,{back_colour},{-1 if bold else 0},0,0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},{margin_v},0",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for s in segments:
        lines.append(
            f"Dialogue: 0,{seconds_to_ass(s.start)},{seconds_to_ass(s.end)},HealthStudio,,0,0,0,,"
            f"{event_prefix}{_ass_escape_text(s.text)}"
        )

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


# --- Small helpers for Health Studio integration ---

def extract_audio_for_transcription(video_path: str | Path, out_dir: Optional[Path] = None) -> Optional[Path]:
    """Extract mono 16kHz audio suitable for Whisper-style STT."""
    ffm = which_ffmpeg()
    if not ffm:
        return None
    out_dir = out_dir or (Path(video_path).parent / "audio_extract")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_wav = out_dir / (Path(video_path).stem + "_audio.wav")
    cmd = [
        ffm, "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(out_wav),
    ]
    code, _, _ = _run(cmd)
    return out_wav if code == 0 and out_wav.exists() else None
