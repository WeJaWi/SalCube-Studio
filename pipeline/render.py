"""FFmpeg composite render engine (host video, split-screens, b-roll, subs).

Each beat becomes a clip. Beat kinds:
  host  — full-screen HeyGen host video; played once, last frame held
  split — b-roll on the left half, the host on the right half
  anim  — locally rendered animation (already exactly the right duration)
  stock / video — real or AI b-roll, looped to cover the narration
  image — still with Ken Burns (fallback)
  blank — solid card (last-resort fallback)
Every beat gets its narration audio muxed in and its subtitles burned.
Beats are concatenated, then the music bed is mixed low underneath.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg/ffprobe not found on PATH (install ffmpeg).")


def _run(cmd: List[str]) -> None:
    # -nostats: progress lines would otherwise drown real errors in stderr
    cmd = [cmd[0], "-hide_banner", "-nostats", *cmd[1:]]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + " ".join(cmd) + "\n\n" + p.stderr[-1600:])


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _esc(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _fonts_dir() -> str:
    font = config.find_font()
    return str(Path(font).parent) if font else "/System/Library/Fonts/Supplemental"


def _sub_style(margin_r: int = 25) -> str:
    # Values are in ASS script space (PlayRes ~384x288), NOT pixels, so they
    # scale with resolution. FontName is required or libass renders nothing.
    # Slightly larger than usual — senior audience. BorderStyle=4 (libass
    # whole-line box), NOT 3: per-run boxes draw a shadow-copy of the text
    # that pokes out under the box. margin_r clears a figure overlay if any.
    return ("FontName=Arial,Fontsize=17,PrimaryColour=&H00FFFFFF,BorderStyle=4,"
            "Outline=1,Shadow=0,BackColour=&H90000000,Alignment=2,"
            f"MarginV=20,MarginL=25,MarginR={margin_r}")


def _pane(idx: int, kind: str, w: int, h: int, fps: int, frames: int,
          ken_burns: bool, label: str) -> str:
    """Filter chain turning input `idx` into a w×h pane labeled [label]."""
    if kind in ("stock", "video"):
        return (f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},fps={fps},setsar=1[{label}]")
    if kind in ("host", "anim"):
        # play once; hold the last frame if the clip runs a hair short
        return (f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},fps={fps},"
                f"tpad=stop_mode=clone:stop_duration=10,setsar=1[{label}]")
    if kind == "image":
        big_w, big_h = int(w * 1.5) // 2 * 2, int(h * 1.5) // 2 * 2
        f = (f"[{idx}:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
             f"crop={big_w}:{big_h},")
        f += (f"zoompan=z='min(zoom+0.0006,1.3)':d={frames}:x='iw/2-(iw/zoom/2)':"
              f"y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}" if ken_burns
              else f"scale={w}:{h}")
        return f + f",setsar=1[{label}]"
    return f"[{idx}:v]setsar=1[{label}]"  # blank lavfi color


def _visual_input(media: Optional[Path], kind: str, w: int, h: int,
                  fps: int) -> List[str]:
    """ffmpeg -i args for one visual source of the given kind."""
    if kind in ("stock", "video") and media:
        return ["-stream_loop", "-1", "-i", str(media)]
    if kind in ("host", "anim") and media:
        return ["-i", str(media)]
    if kind == "image" and media:
        return ["-loop", "1", "-i", str(media)]
    return ["-f", "lavfi", "-i", f"color=c=0xFAF6EF:s={w}x{h}:r={fps}"]


OVERLAY_KINDS = ("stock", "video", "image")


def render_beat_classic(b: Dict[str, Any], out: Path, width: int, height: int,
                        fps: int, ken_burns: bool,
                        figure: Optional[Path] = None,
                        figure_frac: float = 0.92, figure_margin: int = 40,
                        bg_blur: int = 8,
                        preset: Optional[str] = "veryfast",
                        badge: Optional[Path] = None, badge_width: int = 170,
                        badge_margin: int = 28) -> None:
    """The 'classic' layout: blurred b-roll background, the philosopher
    cutout on the right, an animated voice waveform on the left with the
    subtitles beneath it. Built for very long (up to 3h) cheap videos —
    no transitions, fast encode preset."""
    duration = float(b.get("duration", 5))
    frames = max(1, int(duration * fps))
    kind = b.get("kind", "image")
    media = b.get("media")
    if media is None:
        kind = "blank"

    inputs: List[str] = []
    chain: List[str] = []

    inputs += _visual_input(media, kind, width, height, fps)
    chain.append(_pane(0, kind if media else "blank", width, height, fps,
                       frames, ken_burns, "raw"))
    chain.append(f"[raw]boxblur={bg_blur}:1[bg]")
    last = "bg"
    n_in = 1

    if figure and figure.exists():
        inputs += ["-i", str(figure)]
        fh = int(height * figure_frac)
        chain.append(f"[{n_in}:v]scale=-1:{fh}[fig]")
        chain.append(f"[{last}][fig]overlay=W-w-{figure_margin}:H-h[bgfig]")
        last = "bgfig"
        n_in += 1

    if badge and badge.exists():
        inputs += ["-i", str(badge)]
        chain.append(f"[{n_in}:v]scale={badge_width}:-1[bdg]")
        chain.append(f"[{last}][bdg]overlay={badge_margin}:{badge_margin}[bgb]")
        last = "bgb"
        n_in += 1

    audio = b.get("audio")
    have_audio = bool(audio and Path(audio).exists())
    if have_audio:
        inputs += ["-i", str(audio)]
        a_idx = n_in
        n_in += 1
        # voice waveform, mid-left
        ww = int(width * 0.42) // 2 * 2
        wh = int(height * 0.20) // 2 * 2
        wx, wy = int(width * 0.05), int(height * 0.36)
        chain.append(f"[{a_idx}:a]asplit=2[amix][awave]")
        chain.append(f"[awave]aformat=channel_layouts=mono,"
                     f"showwaves=s={ww}x{wh}:mode=cline:rate={fps}:"
                     f"colors=0xFAF6EF@0.9[wv]")
        chain.append(f"[{last}][wv]overlay={wx}:{wy}:eof_action=repeat[bgw]")
        last = "bgw"

    srt = b.get("srt")
    if srt and Path(srt).exists():
        # bottom-left, under the waveform, clear of the figure on the right
        style = ("FontName=Arial,Fontsize=15,PrimaryColour=&H00FFFFFF,"
                 "BorderStyle=4,Outline=1,Shadow=0,BackColour=&H90000000,"
                 "Alignment=1,MarginV=42,MarginL=25,MarginR=185")
        chain.append(f"[{last}]subtitles='{_esc(str(srt))}':"
                     f"fontsdir='{_esc(_fonts_dir())}':force_style='{style}'[v]")
    else:
        chain.append(f"[{last}]null[v]")

    if have_audio:
        chain.append(f"[amix]aresample=44100,apad,atrim=0:{duration},"
                     f"asetpts=N/SR/TB[a]")
    else:
        inputs += ["-f", "lavfi", "-i",
                   "anullsrc=channel_layout=stereo:sample_rate=44100"]
        chain.append(f"[{n_in}:a]atrim=0:{duration}[a]")

    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-preset", preset or "veryfast",
          "-pix_fmt", "yuv420p", "-r", str(fps),
          "-c:a", "aac", "-ar", "44100", "-ac", "2",
          "-t", f"{duration}", str(out)])


def render_beat(b: Dict[str, Any], out: Path, width: int, height: int,
                fps: int, ken_burns: bool,
                overlay: Optional[Path] = None, overlay_opacity: float = 0.35,
                pad_head: float = 0.0, pad_tail: float = 0.0,
                figure: Optional[Path] = None, figure_frac: float = 0.92,
                figure_margin: int = 40, sub_margin_r: int = 25,
                badge: Optional[Path] = None, badge_width: int = 170,
                badge_margin: int = 28) -> None:
    """Render one composited beat clip from a beat dict.

    Beat keys: kind, media, host_media, host_kind, srt, audio, duration.
    For kind "split", `media`(+kind fallback handling) is the left b-roll pane
    and host_media/host_kind the right pane; otherwise media fills the frame.
    `overlay` (dust/twinkle, white-on-black) is screen-blended over b-roll
    kinds. pad_head/pad_tail extend the clip with frozen frames + silence so
    crossfade transitions never eat narration. `badge` (e.g. a QR card) is
    pinned top-left on every kind, including host and split beats.
    """
    duration = float(b.get("duration", 5))
    frames = max(1, int(duration * fps))
    kind = b.get("kind", "image")
    media = b.get("media")
    if media is None and kind != "split":
        kind = "blank"

    inputs: List[str] = []
    chain: List[str] = []

    if kind == "split":
        half = width // 2
        left_kind = b.get("media_kind") or ("blank" if media is None else "stock")
        host_kind = b.get("host_kind", "host")
        inputs += _visual_input(media, left_kind, half, height, fps)
        inputs += _visual_input(b.get("host_media"), host_kind, half, height, fps)
        chain.append(_pane(0, left_kind, half, height, fps, frames, ken_burns, "L"))
        chain.append(_pane(1, host_kind, half, height, fps, frames, ken_burns, "R"))
        chain.append("[L][R]hstack=inputs=2,setsar=1[bg]")
        n_in = 2
    else:
        inputs += _visual_input(media, kind, width, height, fps)
        chain.append(_pane(0, kind if media else "blank", width, height, fps,
                           frames, ken_burns, "bg"))
        n_in = 1

    last = "bg"
    if overlay and overlay.exists() and kind in OVERLAY_KINDS:
        inputs += ["-stream_loop", "-1", "-i", str(overlay)]
        chain.append(f"[{last}]format=gbrp[obase]")
        chain.append(f"[{n_in}:v]scale={width}:{height},fps={fps},format=gbrp[opart]")
        chain.append(f"[obase][opart]blend=all_mode=screen:"
                     f"all_opacity={overlay_opacity},format=yuv420p[bgo]")
        last = "bgo"
        n_in += 1

    # optional figure cutout pinned bottom-right (the philosopher channel's
    # constant on-screen character — a still image, not a talking avatar)
    if figure and figure.exists() and kind != "split":
        inputs += ["-i", str(figure)]
        fh = int(height * figure_frac)
        chain.append(f"[{n_in}:v]scale=-1:{fh}[fig]")
        chain.append(f"[{last}][fig]overlay=W-w-{figure_margin}:H-h[bgfig]")
        last = "bgfig"
        n_in += 1

    if badge and badge.exists():
        inputs += ["-i", str(badge)]
        chain.append(f"[{n_in}:v]scale={badge_width}:-1[bdg]")
        chain.append(f"[{last}][bdg]overlay={badge_margin}:{badge_margin}[bgb]")
        last = "bgb"
        n_in += 1

    srt = b.get("srt")
    if srt and Path(srt).exists():
        chain.append(f"[{last}]subtitles='{_esc(str(srt))}':fontsdir='{_esc(_fonts_dir())}':"
                     f"force_style='{_sub_style(sub_margin_r)}'[vc]")
    else:
        chain.append(f"[{last}]null[vc]")

    # freeze-frame padding for transitions (after subtitles, so cue timing
    # stays relative to the narration)
    total = pad_head + duration + pad_tail
    if pad_head > 0 or pad_tail > 0:
        chain.append(f"[vc]tpad=start_mode=clone:start_duration={pad_head}:"
                     f"stop_mode=clone:stop_duration={pad_tail + 5}[v]")
    else:
        chain.append("[vc]null[v]")

    audio = b.get("audio")
    if audio and Path(audio).exists():
        inputs += ["-i", str(audio)]
        a = f"[{n_in}:a]aresample=44100"
        if pad_head > 0:
            ms = int(pad_head * 1000)
            a += f",adelay={ms}|{ms}"
        a += f",apad,atrim=0:{total},asetpts=N/SR/TB[a]"
        chain.append(a)
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        chain.append(f"[{n_in}:a]atrim=0:{total}[a]")

    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
          "-c:a", "aac", "-ar", "44100", "-ac", "2",
          "-t", f"{total}", str(out)])


def concat_with_music(clips: List[Path], music: Optional[Path], out: Path,
                      workdir: Path, music_volume: float = 0.15) -> None:
    lst = workdir / "concat.txt"
    lst.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips))
    if music and music.exists():
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-stream_loop", "-1", "-i", str(music),
              "-filter_complex",
              f"[1:a]volume={music_volume},aresample=44100[m];"
              "[0:a][m]amix=inputs=2:duration=first:normalize=0:"
              "dropout_transition=2[a]",
              "-map", "0:v", "-map", "[a]",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
              str(out)])
    else:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
              str(out)])


# One ffmpeg run decodes all its inputs concurrently — too many 1080p inputs
# in a single xfade graph exhausts memory. Long videos are assembled in
# bounded groups, then the group intermediates are crossfaded together (the
# junction padding survives at group boundaries, so the math is identical).
MAX_XFADE_INPUTS = 10


def _xfade_chain(clips: List[Path], music: Optional[Path], out: Path,
                 trans_seconds: float, trans_types: List[str],
                 music_volume: float = 0.15, crf: Optional[int] = None) -> None:
    durs = [probe_duration(c) for c in clips]
    inputs: List[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    n = len(clips)

    chain: List[str] = []
    vprev, aprev = "0:v", "0:a"
    offset = 0.0
    for i in range(1, n):
        t = trans_types[(i - 1) % len(trans_types)]
        offset += durs[i - 1] - trans_seconds
        vout = f"v{i}" if i < n - 1 else "vv"
        aout = f"a{i}" if i < n - 1 else "aa"
        chain.append(f"[{vprev}][{i}:v]xfade=transition={t}:"
                     f"duration={trans_seconds}:offset={offset:.3f}[{vout}]")
        chain.append(f"[{aprev}][{i}:a]acrossfade=d={trans_seconds}[{aout}]")
        vprev, aprev = vout, aout

    maps = ["-map", f"[{vprev}]"]
    if music and music.exists():
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        chain.append(f"[{n}:a]volume={music_volume},aresample=44100[m]")
        chain.append(f"[{aprev}][m]amix=inputs=2:duration=first:"
                     f"normalize=0:dropout_transition=2[afinal]")
        maps += ["-map", "[afinal]"]
    else:
        maps += ["-map", f"[{aprev}]"]

    quality = ["-crf", str(crf)] if crf is not None else []
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
          *maps, "-c:v", "libx264", *quality, "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", str(out)])


def crossfade_concat(clips: List[Path], music: Optional[Path], out: Path,
                     trans_seconds: float, trans_types: List[str],
                     music_volume: float = 0.15, workdir: Optional[Path] = None,
                     log=lambda *_: None) -> None:
    """Concatenate with xfade video transitions + audio crossfades.

    Beats are rendered with freeze-frame/silence padding at the junctions, so
    the crossfade consumes padding — never narration. Long videos assemble
    hierarchically in groups of MAX_XFADE_INPUTS.
    """
    if len(clips) <= MAX_XFADE_INPUTS:
        _xfade_chain(clips, music, out, trans_seconds, trans_types, music_volume)
        return

    workdir = workdir or out.parent
    groups = [clips[i:i + MAX_XFADE_INPUTS]
              for i in range(0, len(clips), MAX_XFADE_INPUTS)]
    # a trailing single clip can't crossfade with itself — fold it back
    if len(groups[-1]) == 1:
        groups[-2].extend(groups.pop())

    inters: List[Path] = []
    ti = 0
    for gi, g in enumerate(groups):
        log(f"  transition group {gi + 1}/{len(groups)} ({len(g)} clips)")
        inter = workdir / f"xseg_{gi:02d}.mp4"
        rotated = trans_types[ti % len(trans_types):] + trans_types[:ti % len(trans_types)]
        # near-lossless intermediates: they get re-encoded once more below
        _xfade_chain(g, None, inter, trans_seconds, rotated, crf=18)
        ti += len(g) - 1
        inters.append(inter)

    log(f"  final assembly of {len(inters)} segments + music bed")
    rotated = trans_types[ti % len(trans_types):] + trans_types[:ti % len(trans_types)]
    _xfade_chain(inters, music, out, trans_seconds, rotated, music_volume)
    for p in inters:
        p.unlink(missing_ok=True)


def render_video(beats: List[Dict[str, Any]], profile: Dict[str, Any],
                 music: Optional[Path], workdir: Path, out_path: Path,
                 overlay: Optional[Path] = None,
                 badge: Optional[Path] = None, log=print) -> Path:
    ensure_ffmpeg()
    v = profile.get("video", {})
    width, height = int(v.get("width", 1920)), int(v.get("height", 1080))
    fps = int(v.get("fps", 30))
    ken_burns = bool(v.get("ken_burns", True))
    ov_cfg = v.get("overlay", {}) or {}
    ov_opacity = float(ov_cfg.get("opacity", 0.35))
    tr_cfg = v.get("transitions", {}) or {}
    tr_on = bool(tr_cfg.get("enabled", True)) and len(beats) > 1
    tr_sec = float(tr_cfg.get("seconds", 0.5))
    tr_types = tr_cfg.get("types") or ["fade", "fade", "smoothleft", "fade",
                                       "circleopen", "fade", "smoothright"]
    fig_cfg = v.get("figure", {}) or {}
    figure = None
    if fig_cfg.get("image"):
        p = Path(fig_cfg["image"])
        figure = p if p.is_absolute() else config.ROOT / p
    fig_frac = float(fig_cfg.get("height_frac", 0.92))
    fig_margin = int(fig_cfg.get("margin", 40))
    sub_margin_r = int(fig_cfg.get("sub_margin_r", 120)) if figure else 25

    badge_width = int(v.get("corner_badge_width", 170))
    badge_margin = int(v.get("corner_badge_margin", 28))

    layout = v.get("layout", "standard")
    clips: List[Path] = []
    n = len(beats)
    img_i = 0
    for i, b in enumerate(beats):
        clip = workdir / f"beat_{i:03d}.mp4"
        log(f"  beat {i+1}/{n} ({b.get('kind')}, {b.get('duration', 0):.1f}s)")
        # dust/star overlay on ~half the still images (alternating) — footage
        # keeps the profile's blanket behavior
        beat_overlay = overlay
        if b.get("kind") == "image":
            beat_overlay = overlay if img_i % 2 == 0 else None
            img_i += 1
        if layout == "classic":
            render_beat_classic(b, clip, width, height, fps, ken_burns,
                                figure=figure, figure_frac=fig_frac,
                                figure_margin=fig_margin,
                                bg_blur=int(v.get("bg_blur", 8)),
                                preset=v.get("encode_preset", "veryfast"),
                                badge=badge, badge_width=badge_width,
                                badge_margin=badge_margin)
        else:
            render_beat(b, clip, width, height, fps, ken_burns,
                        overlay=beat_overlay, overlay_opacity=ov_opacity,
                        pad_head=tr_sec if (tr_on and i > 0) else 0.0,
                        pad_tail=tr_sec if (tr_on and i < n - 1) else 0.0,
                        figure=figure, figure_frac=fig_frac,
                        figure_margin=fig_margin, sub_margin_r=sub_margin_r,
                        badge=badge, badge_width=badge_width,
                        badge_margin=badge_margin)
        clips.append(clip)

    if tr_on:
        log("  assembling with crossfade transitions + music bed")
        crossfade_concat(clips, music, out_path, tr_sec, tr_types,
                         workdir=workdir, log=log)
    else:
        log("  concatenating beats + music bed")
        concat_with_music(clips, music, out_path, workdir)
    return out_path
