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

import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cancel, captions, config, waveform

# Hard ceiling for a single ffmpeg call. A normal beat composite uses well
# under 1 GB; this only exists to catch filtergraph pathologies (a fast
# infinite -stream_loop source racing far ahead of a slow filter branch has
# been observed to grow RSS by ~1 GB/s, unbounded) before they can exhaust
# system memory. Real work never approaches this; a runaway hits it in
# seconds and gets killed with a clear error instead of taking the OS down.
_RSS_CEILING_MB = 3072
_WALLCLOCK_CEILING_S = 1800


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg/ffprobe not found on PATH (install ffmpeg).")


def _rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip() or 0) / 1024.0
    except Exception:  # noqa: BLE001
        return 0.0


def _run(cmd: List[str], *, wallclock_s: Optional[float] = None) -> None:
    # -nostats: progress lines would otherwise drown real errors in stderr
    cmd = [cmd[0], "-hide_banner", "-nostats", *cmd[1:]]
    # Cancellation rides a thread-local rather than a parameter, so all ~14
    # call sites stay untouched. No token (tests, CLI use) == no-op.
    tok = cancel.current()
    if tok is not None:
        tok.check()  # already stopped — don't even start another encode
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if tok is not None:
        tok.register(proc)
    watchdog_tripped: List[str] = []
    wall_limit = float(wallclock_s if wallclock_s is not None
                       else _WALLCLOCK_CEILING_S)

    def _watch() -> None:
        start = time.monotonic()
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            rss = _rss_mb(proc.pid)
            if rss > _RSS_CEILING_MB:
                watchdog_tripped.append(
                    f"memory runaway: {rss:.0f} MB > {_RSS_CEILING_MB} MB "
                    f"ceiling (elapsed {elapsed:.0f}s) — killed to protect "
                    f"the system")
                proc.kill()
                return
            if elapsed > wall_limit:
                watchdog_tripped.append(
                    f"wall-clock runaway: {elapsed:.0f}s > "
                    f"{wall_limit:.0f}s ceiling — killed")
                proc.kill()
                return
            time.sleep(1)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    try:
        _, stderr = proc.communicate()
    finally:
        if tok is not None:
            tok.unregister(proc)
    t.join(timeout=5)

    # A cancelled job's ffmpeg was killed by us, so it "fails" with signal 9.
    # Report that as cancellation rather than letting it masquerade as a
    # render error the retry logic would then pointlessly retry.
    if tok is not None and tok.cancelled:
        raise cancel.JobCancelled(f"job {tok.job_id} was stopped")

    if watchdog_tripped:
        raise RuntimeError(f"ffmpeg {watchdog_tripped[0]}:\n" + " ".join(cmd) +
                           "\n\n" + (stderr or "")[-1600:])
    if proc.returncode != 0:
        # negative returncode == killed by signal -N (SIGKILL=-9 etc), not an
        # ffmpeg-reported error — stderr will look "cut off" with no message
        tag = (f"signal {-proc.returncode}" if proc.returncode < 0
               else f"exit code {proc.returncode}")
        raise RuntimeError(f"ffmpeg failed ({tag}):\n" + " ".join(cmd) +
                           "\n\n" + (stderr or "")[-1600:])


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def probe_video_size(path: Path) -> Tuple[int, int, float]:
    """Return (width, height, fps) for the first video stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30)
        # e.g. "1920x1080x30/1" or "1920x1080x30000/1001"
        parts = (out.stdout or "").strip().split("x")
        w = int(parts[0]) if parts else 1920
        h = int(parts[1]) if len(parts) > 1 else 1080
        fps = 30.0
        if len(parts) > 2 and parts[2]:
            fr = parts[2]
            if "/" in fr:
                a, b = fr.split("/", 1)
                fps = float(a) / max(float(b), 1e-6)
            else:
                fps = float(fr)
        return max(w, 2), max(h, 2), max(fps, 1.0)
    except Exception:  # noqa: BLE001
        return 1920, 1080, 30.0


def default_patreon_end_clip() -> Optional[Path]:
    """Bundled CTA clip appended to free-half end when Patreon split is on."""
    for rel in ("branding/patreon-end.mp4", "branding/patreon video.mp4"):
        p = config.ROOT / rel
        if p.exists() and p.stat().st_size > 1000:
            return p
    return None


def append_end_clip(main: Path, ender: Path, out: Path, workdir: Path,
                    log=lambda *_: None, gpu: bool = False) -> Path:
    """Hard-cut ``ender`` onto the end of ``main`` (no crossfade).

    Normalizes the ender to match ``main``'s resolution/fps so concat can
    stream-copy the long free half (re-encode only the short ender).
    """
    if not main.exists() or not ender.exists():
        raise FileNotFoundError(f"append_end_clip missing input: {main} / {ender}")
    w, h, fps = probe_video_size(main)
    # even dimensions for yuv420p
    w -= w % 2
    h -= h % 2
    fps_i = max(1, int(round(fps)))
    norm = workdir / f"{ender.stem}_norm_{w}x{h}_{fps_i}.mp4"
    if not (norm.exists() and norm.stat().st_size > 1000):
        log(f"  normalizing end clip → {w}x{h} @{fps_i}fps")
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps_i},format=yuv420p")
        # Prefer the ender's own audio; if missing, synthesize silence.
        has_audio = False
        try:
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                 str(ender)],
                capture_output=True, text=True, timeout=15)
            has_audio = "audio" in (pr.stdout or "")
        except Exception:  # noqa: BLE001
            has_audio = False
        if has_audio:
            _run([
                "ffmpeg", "-y", "-i", str(ender),
                "-vf", vf,
                "-af", "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo",
                *_encode_args(quality="veryfast", crf=18, gpu=gpu),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(norm),
            ], wallclock_s=300)
        else:
            _run([
                "ffmpeg", "-y", "-i", str(ender),
                "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", vf,
                "-map", "0:v", "-map", "1:a",
                *_encode_args(quality="veryfast", crf=18, gpu=gpu),
                "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart", str(norm),
            ], wallclock_s=300)
    # If main has no audio, still concat carefully — our finals always have audio.
    lst = workdir / f"append_{out.stem}.txt"
    lst.write_text(
        f"file '{main.resolve().as_posix()}'\n"
        f"file '{norm.resolve().as_posix()}'\n")
    tmp = workdir / f"{out.stem}_with_end.mp4"
    # Re-encode audio junction safely; try video copy first (fast for long part1)
    try:
        log(f"  appending end clip to {main.name} ({probe_duration(ender):.1f}s)")
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(tmp),
        ], wallclock_s=max(600.0, probe_duration(main) * 0.2 + 120.0))
    except RuntimeError:
        log("  append stream-copy failed — re-encoding junction")
        _run([
            "ffmpeg", "-y", "-i", str(main), "-i", str(norm),
            "-filter_complex",
            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]",
            *_encode_args(quality="veryfast", crf=20, gpu=gpu),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(tmp),
        ], wallclock_s=max(1800.0, probe_duration(main) * 2 + 300.0))
    # Atomic replace into final path
    if out.resolve() == tmp.resolve():
        return out
    if out.exists() or out.is_symlink():
        out.unlink()
    shutil.move(str(tmp), str(out))
    return out


def _esc(path: str) -> str:
    """Escape a path for embedding inside an ffmpeg filtergraph argument.

    On Windows, normalize separators to forward slashes FIRST: `Path.parent`
    there yields `C:\\Windows\\Fonts`, and escaping those backslashes gives a
    doubled form the subtitles/drawtext filters parse badly. ffmpeg accepts
    forward slashes on Windows, so `C\\:/Windows/Fonts` is the reliable
    spelling. Done only on win32 — on POSIX a backslash is a legal literal
    filename character and must keep being escaped, not rewritten.
    """
    if sys.platform == "win32":
        path = path.replace("\\", "/")
    else:
        path = path.replace("\\", "\\\\")
    return path.replace(":", "\\:").replace("'", "\\'")


def _fonts_dir() -> str:
    font = config.find_font()
    if font:
        return str(Path(font).parent)
    # No font found — fall back to the platform's own font dir rather than a
    # macOS path that doesn't exist on a Windows/Linux render worker.
    if sys.platform == "win32":
        return "C:/Windows/Fonts"
    if sys.platform == "darwin":
        return "/System/Library/Fonts/Supplemental"
    return "/usr/share/fonts"


def _fonts_dir_for(family: Optional[str]) -> str:
    """fontsdir that actually contains `family` (e.g. a user-installed
    Nunito in ~/Library/Fonts); falls back to the system caption font dir."""
    if family and family.lower() not in ("arial", "helvetica"):
        # match on the base family word: "Nunito Bold" must still find
        # Nunito-Bold.ttf / Nunito-VariableFont_wght.ttf. The repo's own
        # fonts/ dir wins — it carries static instances (libass resolves
        # those reliably; variable-font named instances often fall back).
        needle = family.split()[0].lower()
        for d in (config.ROOT / "fonts",
                  Path.home() / "Library/Fonts", Path("/Library/Fonts"),
                  Path("/System/Library/Fonts/Supplemental"),
                  Path("/usr/share/fonts/truetype")):
            if not d.is_dir():
                continue
            for f in d.glob("*.[to]t[fc]"):
                if needle in f.name.lower().replace(" ", "").replace("-", ""):
                    return str(d)
    return _fonts_dir()


# crf(x264, lower=better) → q:v(VideoToolbox, higher=better), calibrated so
# the visual "tier" a caller asks for looks about the same either path.
_CRF_TO_VT_QV = {14: 85, 16: 78, 18: 70, 20: 65, 22: 58, 23: 55, 24: 52}


def _encode_args(quality: Optional[str] = None, crf: Optional[int] = None,
                 gpu: bool = False) -> List[str]:
    """-c:v ... args for one ffmpeg call.

    `quality` is an x264 preset name (veryfast/ultrafast/...), omitted
    entirely (libx264's own default) when None; ignored when gpu=True
    (VideoToolbox has no preset concept — its speed is inherent to the
    hardware path). `crf` (x264, lower=better; omitted -> x264's own
    default ~23) maps to VideoToolbox's `-q:v` (1-100, higher=better) via a
    fixed table when gpu=True, so a caller-supplied quality tier behaves
    consistently either way. Every call site keeps passing the exact
    quality/crf it always has — this only branches the codec, never
    changes CPU-path behavior.
    """
    if gpu:
        # Which hardware encoder exists is per-machine: VideoToolbox on a
        # Mac, NVENC/QuickSync/AMF on a Windows or Linux worker. All are
        # H.264 — only the quality-control flag differs, since each vendor
        # names it differently and none accept x264's -crf/-preset.
        enc = config.gpu_encoder()
        if enc == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4",
                    "-rc", "vbr", "-cq", str(crf if crf is not None else 23),
                    "-b:v", "0"]
        if enc == "h264_qsv":
            return ["-c:v", "h264_qsv",
                    "-global_quality", str(crf if crf is not None else 23)]
        if enc == "h264_amf":
            q = str(crf if crf is not None else 23)
            return ["-c:v", "h264_amf", "-quality", "balanced",
                    "-rc", "cqp", "-qp_i", q, "-qp_p", q]
        if enc == "h264_vaapi":
            return ["-c:v", "h264_vaapi",
                    "-qp", str(crf if crf is not None else 23)]
        if enc == "h264_videotoolbox":
            return ["-c:v", "h264_videotoolbox",
                    "-q:v", str(_CRF_TO_VT_QV.get(crf, 65)),
                    "-allow_sw", "1"]  # falls back to software h264 if VT unavailable
        # gpu was requested but this machine has no hardware H.264 encoder —
        # fall through to x264 rather than emitting an invalid -c:v.
    args = ["-c:v", "libx264"]
    if quality:
        args += ["-preset", quality]
    if crf is not None:
        args += ["-crf", str(crf)]
    return args


def _sub_style(margin_r: int = 25) -> str:
    # Legacy force_style string (fallback only).
    # The preferred path now uses captions.build_ass() to generate a real .ass file.
    # This gives much better control (blur, colors, per-profile styling, etc.).
    return ("FontName=Arial,Fontsize=18,PrimaryColour=&H00FFFFFF,BorderStyle=4,"
            "Outline=1,Shadow=0,BackColour=&H90000000,Alignment=2,"
            f"MarginV=22,MarginL=25,MarginR={margin_r}")


def _generate_beat_ass(
    srt_path: Path,
    workdir: Path,
    profile_video: Dict[str, Any],
    sub_margin_r: int = 25,
    alignment: int = 2,
    margin_v: int = 22,
    font_size: int = 18,
) -> Optional[Path]:
    """Parse SRT and generate a styled .ass file using the new captions module.

    All sizes/margins (including the profile's video.subtitle overrides) are
    in the SAME 384x288 script space the legacy force_style path used, so the
    numbers keep their meaning; libass scales them to the output resolution.

    Returns path to the generated ASS, or None on failure (caller should fall back).
    """
    if not srt_path or not srt_path.exists():
        return None

    try:
        text = srt_path.read_text(encoding="utf-8", errors="ignore")
        segments = captions.parse_srt(text)
        if not segments:
            return None

        sub_cfg: Dict[str, Any] = profile_video.get("subtitle", {}) or {}

        # FontName must be a font FAMILY; file paths belong in fontsdir
        # (already passed to the subtitles filter), not here.
        font = sub_cfg.get("font") or "Arial"

        ass_path = workdir / (srt_path.stem + ".ass")

        captions.build_ass(
            segments,
            ass_path,
            play_res=(384, 288),
            font_name=font,
            font_size=int(sub_cfg.get("size", font_size)),
            primary_colour=sub_cfg.get("color", "&H00FFFFFF"),
            outline=int(sub_cfg.get("outline", 1)),
            shadow=int(sub_cfg.get("shadow", 0)),
            alignment=int(sub_cfg.get("alignment", alignment)),
            margin_v=int(sub_cfg.get("margin_v", margin_v)),
            margin_l=int(sub_cfg.get("margin_l", 25)),
            margin_r=sub_margin_r,
            back_colour=sub_cfg.get("back", "&H90000000"),
            blur=int(sub_cfg.get("blur", 1)),  # soft shadow halo looks modern
            # whole-line box; 3 draws a shadow-copy artifact (see README)
            border_style=4,
            bold=bool(sub_cfg.get("bold", False)),
        )
        return ass_path
    except Exception as exc:  # noqa: BLE001
        print(f"[captions] Could not generate ASS for {srt_path}: {exc}")
        return None


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
                  fps: int, duration: Optional[float] = None,
                  ken_burns: bool = False) -> List[str]:
    """ffmpeg -i args for one visual source of the given kind."""
    if kind in ("stock", "video") and media:
        # -t as an INPUT option (before -i) caps the DEMUXER itself, giving
        # -stream_loop -1 a genuine EOF. Without it the source is truly
        # infinite; when composited via overlay against a slower branch
        # (observed: an audio-driven showwaves wave), ffmpeg's filtergraph
        # scheduler has no signal to stop over-producing from the cheap,
        # infinite side and buffers unboundedly (confirmed: >1 GB/s RSS
        # growth). An output-level -t or a `trim` filter downstream do NOT
        # fix this — they only discard frames after decode, they don't stop
        # the demuxer from producing them. Always pass duration for looped
        # sources that feed into any overlay/composite chain.
        if duration:
            return ["-stream_loop", "-1", "-t", f"{duration:.3f}",
                    "-i", str(media)]
        return ["-stream_loop", "-1", "-i", str(media)]
    if kind in ("host", "anim") and media:
        return ["-i", str(media)]
    if kind == "image" and media:
        # Ken Burns: zoompan generates ALL output frames from a single input
        # frame (d=frames), so the still must NOT be looped — looping it
        # multiplies every looped frame by zoompan's d (~1200 x ~1500 frames)
        # and reproduces the unbounded-buffering leak when composited against
        # the audio-driven wave branch. Confirmed by isolation tests:
        # loop+zoompan+wave leaks; single-frame+zoompan+wave is flat.
        if ken_burns:
            return ["-i", str(media)]
        # plain scale path shows the still for the whole beat — here the loop
        # IS needed, capped at the demuxer like the video sources
        if duration:
            return ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(media)]
        return ["-loop", "1", "-i", str(media)]
    return ["-f", "lavfi", "-i", f"color=c=0xFAF6EF:s={w}x{h}:r={fps}"]


OVERLAY_KINDS = ("stock", "video", "image")


def _precompute_bg_clip(media: Optional[Path], kind: str, duration: float,
                        width: int, height: int, fps: int, ken_burns: bool,
                        bg_blur: int, dest: Path, gpu: bool = False) -> Path:
    """Render the blurred background ALONE into a normal, already-finite
    clip before it ever enters the composite that also carries the live
    showwaves wave.

    Root cause of a recurring memory-runaway bug (fixed 3 times at 3
    different triggers — a looped stock video, an -loop 1 Ken Burns still,
    then a 60fps stock video that outpaced the fixed duration cap): ANY
    infinite/looped source (-stream_loop -1, -loop 1) composited via
    `overlay:eof_action=repeat` against the audio-driven wave branch lets
    ffmpeg's filtergraph scheduler over-produce from the cheap/fast side
    while waiting on the wave, buffering unboundedly (observed >1 GB/s).
    Capping the loop's duration at the demuxer (-t before -i) narrowed the
    window but didn't close it for high-throughput sources. Precomputing
    the background into a flat, single-pass file removes the infinite
    source from the composite ENTIRELY — the class of bug cannot recur here
    regardless of the source's fps, kind, or duration."""
    frames = max(1, int(duration * fps))
    inputs = _visual_input(media, kind, width, height, fps, duration=duration,
                           ken_burns=ken_burns)
    chain = [_pane(0, kind if media else "blank", width, height, fps,
                   frames, ken_burns, "raw"),
             f"[raw]boxblur={bg_blur}:1[bg]"]
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
          "-map", "[bg]", "-an", *_encode_args(quality="veryfast", gpu=gpu),
          "-pix_fmt", "yuv420p", "-r", str(fps), "-t", f"{duration:.3f}",
          str(dest)])
    return dest


def _precompute_wave_clip(
    audio: Path,
    duration: float,
    ww: int,
    wh: int,
    fps: int,
    color: str,
    rate: int,
    scale: str,
    n: int,
    dest: Path,
    gpu: bool = False,
) -> Path:
    """Render the voice waveform ALONE into a finite H.264 clip (black bg).

    Live `showwaves` inside the main classic composite (alongside figure
    overlay + subtitles + audio split) has been observed to hit the RSS
    ceiling in seconds on some beats (rate=30 + dynaudnorm was especially
    bad). Precomputing the wave the same way we precompute the blurred
    background removes the audio-driven filter from the composite graph
    entirely — main pass only overlays ordinary video inputs.
    """
    dest = Path(dest)
    # Cap rate: every-frame redraw is pretty but costly; 15 is smooth enough
    # and stays well under the memory ceiling in soak tests.
    rate = max(1, min(int(rate), 15))
    if scale not in ("lin", "log", "sqrt", "cbrt"):
        scale = "sqrt"
    n = max(1, int(n))
    filt = (
        f"[0:a]aformat=channel_layouts=mono,"
        f"showwaves=s={ww}x{wh}:mode=cline:colors={color}:"
        f"rate={rate}:draw=full:scale={scale}:n={n},"
        f"fps={fps},format=yuv420p[w]"
    )
    _run([
        "ffmpeg", "-y", "-i", str(audio),
        "-filter_complex", filt,
        "-map", "[w]", "-an",
        *_encode_args(quality="ultrafast", gpu=gpu), "-pix_fmt", "yuv420p",
        "-r", str(fps), "-t", f"{duration:.3f}",
        str(dest),
    ])
    return dest


def render_beat_classic(b: Dict[str, Any], out: Path, width: int, height: int,
                        fps: int, ken_burns: bool,
                        figure: Optional[Path] = None,
                        figure_frac: float = 0.92, figure_margin: int = 40,
                        bg_blur: int = 8,
                        preset: Optional[str] = "veryfast",
                        badge: Optional[Path] = None, badge_width: int = 170,
                        badge_margin: int = 28,
                        popup: Optional[Path] = None,
                        popup_width: int = 420, popup_margin: int = 28,
                        workdir: Optional[Path] = None,
                        profile_video: Optional[Dict[str, Any]] = None,
                        gpu: bool = False) -> None:
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

    # Background is rendered to a flat file FIRST (see _precompute_bg_clip
    # docstring) — the main composite below never sees an infinite/looped
    # source, only ever this ordinary, already-finite clip.
    wdir = workdir or out.parent
    bg_clip = _precompute_bg_clip(media, kind if media else "blank", duration,
                                  width, height, fps, ken_burns, bg_blur,
                                  wdir / f"{out.stem}_bgprecomp.mp4", gpu=gpu)
    inputs += ["-i", str(bg_clip)]
    chain.append("[0:v]setsar=1[bg]")
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

    # Spotify (or other) popup card — bottom-right, full-video duration
    if popup and popup.exists():
        inputs += ["-i", str(popup)]
        chain.append(f"[{n_in}:v]scale={popup_width}:-1[pop]")
        chain.append(
            f"[{last}][pop]overlay=W-w-{popup_margin}:H-h-{popup_margin}[bgp]")
        last = "bgp"
        n_in += 1

    audio = b.get("audio")
    have_audio = bool(audio and Path(audio).exists())
    if have_audio:
        # Waveform is PRECOMPUTED into a finite clip (see _precompute_wave_clip)
        # then overlaid with colorkey so black bg is transparent. The narration
        # audio is a separate -i used only for the final mix — never asplit
        # into a live showwaves branch in this graph.
        wv_cfg = (profile_video or {}).get("waveform", {}) or {}
        ww = int(width * float(wv_cfg.get("width_frac", 0.46))) // 2 * 2
        wh = int(height * float(wv_cfg.get("height_frac", 0.30))) // 2 * 2
        wx, wy = int(width * 0.05), max(0, int(height * 0.46) - wh // 2)
        wv_color = wv_cfg.get("color", "0xFAF6EF@0.9")
        # rate capped inside _precompute_wave_clip (default 15, smooth + safe)
        wv_rate = int(wv_cfg.get("rate", 15))
        wv_n = max(1, int(wv_cfg.get("n", 5)))
        wv_scale = str(wv_cfg.get("scale", "sqrt"))
        wave_clip = _precompute_wave_clip(
            Path(audio), duration, ww, wh, fps, wv_color, wv_rate, wv_scale,
            wv_n, wdir / f"{out.stem}_waveprecomp.mp4", gpu=gpu,
        )
        inputs += ["-i", str(wave_clip)]
        # colorkey: showwaves draws on black; strip black so only the cream
        # wave sits over the b-roll (same look as live overlay).
        chain.append(
            f"[{n_in}:v]colorkey=0x000000:0.18:0.2[wvk]"
        )
        chain.append(
            f"[{last}][wvk]overlay={wx}:{wy}:eof_action=repeat[bgw]"
        )
        last = "bgw"
        n_in += 1
        inputs += ["-i", str(audio)]
        a_idx = n_in
        n_in += 1
        chain.append(f"[{a_idx}:a]anull[amix]")

    srt = b.get("srt")
    if srt and Path(srt).exists():
        srt_path = Path(srt)
        pv = profile_video or {}
        wdir = workdir or out.parent
        # Use new build_ass() for richer styling (left-aligned for classic layout)
        ass_path = _generate_beat_ass(srt_path, wdir, pv, sub_margin_r=185,
                                      alignment=1, margin_v=42, font_size=20)
        sub_file = ass_path or srt_path
        sub_fonts = _fonts_dir_for((pv.get("subtitle", {}) or {}).get("font"))
        if sub_file.suffix.lower() == ".ass":
            chain.append(f"[{last}]subtitles='{_esc(str(sub_file))}':fontsdir='{_esc(sub_fonts)}'[v]")
        else:
            # bottom-left fallback
            style = ("FontName=Arial,Fontsize=20,PrimaryColour=&H00FFFFFF,"
                     "BorderStyle=4,Outline=1,Shadow=0,BackColour=&H90000000,"
                     "Alignment=1,MarginV=42,MarginL=25,MarginR=185")
            chain.append(f"[{last}]subtitles='{_esc(str(sub_file))}':"
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
          *_encode_args(quality=preset or "veryfast", gpu=gpu),
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
                badge_margin: int = 28,
                popup: Optional[Path] = None,
                popup_width: int = 420, popup_margin: int = 28,
                workdir: Optional[Path] = None,
                profile_video: Optional[Dict[str, Any]] = None,
                gpu: bool = False) -> None:
    """Render one composited beat clip from a beat dict.

    Beat keys: kind, media, host_media, host_kind, srt, audio, duration.
    For kind "split", `media`(+kind fallback handling) is the left b-roll pane
    and host_media/host_kind the right pane; otherwise media fills the frame.
    `overlay` (dust/twinkle, white-on-black) is screen-blended over b-roll
    kinds. pad_head/pad_tail extend the clip with frozen frames + silence so
    crossfade transitions never eat narration. `badge` (e.g. a QR card) is
    pinned top-left on every kind, including host and split beats.
    `popup` (e.g. Spotify card) is pinned bottom-right.
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
        inputs += _visual_input(media, left_kind, half, height, fps,
                                duration=duration, ken_burns=ken_burns)
        inputs += _visual_input(b.get("host_media"), host_kind, half, height,
                                fps, duration=duration, ken_burns=ken_burns)
        chain.append(_pane(0, left_kind, half, height, fps, frames, ken_burns, "L"))
        chain.append(_pane(1, host_kind, half, height, fps, frames, ken_burns, "R"))
        chain.append("[L][R]hstack=inputs=2,setsar=1[bg]")
        n_in = 2
    else:
        inputs += _visual_input(media, kind, width, height, fps,
                                duration=duration, ken_burns=ken_burns)
        chain.append(_pane(0, kind if media else "blank", width, height, fps,
                           frames, ken_burns, "bg"))
        n_in = 1

    last = "bg"
    if overlay and overlay.exists() and kind in OVERLAY_KINDS:
        # same fix as _visual_input: input-level -t gives the demuxer a real
        # EOF instead of an infinite loop racing ahead in the filtergraph
        inputs += ["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", str(overlay)]
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

    if popup and popup.exists():
        inputs += ["-i", str(popup)]
        chain.append(f"[{n_in}:v]scale={popup_width}:-1[pop]")
        chain.append(
            f"[{last}][pop]overlay=W-w-{popup_margin}:H-h-{popup_margin}[bgp]")
        last = "bgp"
        n_in += 1

    srt = b.get("srt")
    if srt and Path(srt).exists():
        srt_path = Path(srt)
        pv = profile_video or {}
        wdir = workdir or out.parent
        # Prefer generating a proper .ass using the new captions.build_ass()
        ass_path = _generate_beat_ass(srt_path, wdir, pv, sub_margin_r, alignment=2)
        sub_file = ass_path or srt_path
        sub_fonts = _fonts_dir_for((pv.get("subtitle", {}) or {}).get("font"))
        if sub_file.suffix.lower() == ".ass":
            chain.append(f"[{last}]subtitles='{_esc(str(sub_file))}':fontsdir='{_esc(sub_fonts)}'[vc]")
        else:
            # Fallback to legacy SRT + force_style
            chain.append(f"[{last}]subtitles='{_esc(str(sub_file))}':fontsdir='{_esc(_fonts_dir())}':"
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
          *_encode_args(gpu=gpu), "-pix_fmt", "yuv420p", "-r", str(fps),
          "-c:a", "aac", "-ar", "44100", "-ac", "2",
          "-t", f"{total}", str(out)])


def burn_timed_popup(
    video: Path,
    popup: Path,
    out: Path,
    windows: List[Tuple[float, float]],
    *,
    popup_width: int = 420,
    popup_margin: int = 28,
    workdir: Optional[Path] = None,
    gpu: bool = False,
) -> Path:
    """Burn a PNG popup onto `video` only during the given time windows.

    windows: list of (start_sec, end_sec). Typical Spotify use:
      (0, 10) + one random 10s window in the first 10 minutes.
    """
    video, popup, out = Path(video), Path(popup), Path(out)
    if not video.exists() or not popup.exists() or not windows:
        if video != out and video.exists():
            import shutil
            shutil.copy2(video, out)
        return out
    # Build enable='between(t,s,e)+between(t,s2,e2)'
    parts = []
    for s, e in windows:
        s, e = max(0.0, float(s)), max(0.0, float(e))
        if e <= s:
            continue
        parts.append(f"between(t\\,{s:.3f}\\,{e:.3f})")
    if not parts:
        if video != out:
            import shutil
            shutil.copy2(video, out)
        return out
    enable = "+".join(parts)
    wdir = workdir or out.parent
    tmp = wdir / f"{out.stem}_popup_tmp.mp4"
    filt = (
        f"[1:v]scale={popup_width}:-1,format=rgba[pop];"
        f"[0:v][pop]overlay=W-w-{popup_margin}:H-h-{popup_margin}:"
        f"enable='{enable}'[v]"
    )
    # Video already encoded — only re-encode video for overlay; copy audio
    _run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(popup),
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        *_encode_args(quality="veryfast", gpu=gpu), "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(tmp),
    ], wallclock_s=max(_WALLCLOCK_CEILING_S,
                       probe_duration(video) * 0.5 + 600))
    tmp.replace(out)
    return out


def plan_spotify_windows(duration: float, *,
                         first_dur: float = 10.0,
                         second_dur: float = 10.0,
                         random_within: float = 600.0,
                         seed: Optional[int] = None) -> List[Tuple[float, float]]:
    """Return [(0, first_dur), (rand_start, rand_start+second_dur)] clipped to duration."""
    import random as _random
    duration = max(0.0, float(duration))
    windows: List[Tuple[float, float]] = []
    if duration <= 0:
        return windows
    # Opening popup
    w1_end = min(first_dur, duration)
    if w1_end > 0.05:
        windows.append((0.0, w1_end))
    # Second popup: random start inside first 10 minutes, after the first ends
    if duration > first_dur + 1.0:
        rng = _random.Random(seed)
        lo = first_dur + 5.0  # small gap after first popup
        hi = min(random_within, duration) - second_dur
        if hi > lo:
            start = rng.uniform(lo, hi)
            windows.append((start, start + second_dur))
        elif duration - first_dur > second_dur + 1:
            # video shorter than 10 min but still room after first popup
            start = rng.uniform(first_dur + 1.0, max(first_dur + 1.0,
                                                     duration - second_dur))
            windows.append((start, min(start + second_dur, duration)))
    return windows


def resolve_popup_windows(duration: float,
                          requested: Optional[List[Tuple[float, float]]],
                          *, full_duration: bool = False
                          ) -> List[Tuple[float, float]]:
    """Resolve timed vs persistent popup coverage for one exported file."""
    if full_duration:
        return [(0.0, max(1.0, float(duration) + 1.0))]
    return list(requested or [])


def concat_with_music(clips: List[Path], music: Optional[Path], out: Path,
                      workdir: Path, music_volume: float = 0.15) -> None:
    """Stitch beat clips + optional music bed.

    Video is **stream-copied** (`-c:v copy`) — every beat is already
    libx264/yuv420p/1080p from render_beat*. Re-encoding a 2–3h classic
    video here is what hit the 30‑minute wall-clock ceiling. Only the audio
    path is re-encoded so we can amix the music bed.
    """
    lst = workdir / "concat.txt"
    # Only final beat composites — not *_bgprecomp / *_waveprecomp
    beat_clips = [c for c in clips
                  if c.name.startswith("beat_")
                  and "_bgprecomp" not in c.name
                  and "_waveprecomp" not in c.name]
    if not beat_clips:
        beat_clips = list(clips)
    lst.write_text("".join(
        f"file '{c.resolve().as_posix()}'\n" for c in beat_clips))
    # Long jobs: allow ~2s per minute of video + 10 min floor (copy is fast)
    total_dur = sum(probe_duration(c) for c in beat_clips[:5])  # sample
    if len(beat_clips) > 5 and total_dur > 0:
        # extrapolate from first 5 if uniform-ish; else sum all (slower but ok)
        avg = total_dur / 5
        est = avg * len(beat_clips)
    else:
        est = sum(probe_duration(c) for c in beat_clips)
    wall = max(1200.0, min(14_400.0, est * 0.15 + 600.0))  # copy+amix

    if music and music.exists():
        # -c:v copy keeps video as-is; audio filter only mixes bed under VO
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-stream_loop", "-1", "-i", str(music),
              "-filter_complex",
              f"[1:a]volume={music_volume},aresample=44100[m];"
              "[0:a]aresample=44100[va];"
              "[va][m]amix=inputs=2:duration=first:normalize=0:"
              "dropout_transition=2[a]",
              "-map", "0:v", "-map", "[a]",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart",
              str(out)], wallclock_s=wall)
    else:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-c", "copy", "-movflags", "+faststart", str(out)],
             wallclock_s=wall)


# One ffmpeg run decodes all its inputs concurrently — too many 1080p inputs
# in a single xfade graph exhausts memory. Long videos are assembled in
# bounded groups, then the group intermediates are crossfaded together (the
# junction padding survives at group boundaries, so the math is identical).
MAX_XFADE_INPUTS = 10


def _xfade_chain(clips: List[Path], music: Optional[Path], out: Path,
                 trans_seconds: float, trans_types: List[str],
                 music_volume: float = 0.15, crf: Optional[int] = None,
                 gpu: bool = False) -> None:
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

    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
          *maps, *_encode_args(crf=crf, gpu=gpu), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", str(out)])


def crossfade_concat(clips: List[Path], music: Optional[Path], out: Path,
                     trans_seconds: float, trans_types: List[str],
                     music_volume: float = 0.15, workdir: Optional[Path] = None,
                     log=lambda *_: None, gpu: bool = False) -> None:
    """Concatenate with xfade video transitions + audio crossfades.

    Beats are rendered with freeze-frame/silence padding at the junctions, so
    the crossfade consumes padding — never narration. Long videos assemble
    hierarchically in groups of MAX_XFADE_INPUTS.
    """
    if len(clips) <= MAX_XFADE_INPUTS:
        _xfade_chain(clips, music, out, trans_seconds, trans_types, music_volume,
                    gpu=gpu)
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
        _xfade_chain(g, None, inter, trans_seconds, rotated, crf=18, gpu=gpu)
        ti += len(g) - 1
        inters.append(inter)

    log(f"  final assembly of {len(inters)} segments + music bed")
    rotated = trans_types[ti % len(trans_types):] + trans_types[:ti % len(trans_types)]
    _xfade_chain(inters, music, out, trans_seconds, rotated, music_volume, gpu=gpu)
    for p in inters:
        p.unlink(missing_ok=True)


def render_video(beats: List[Dict[str, Any]], profile: Dict[str, Any],
                 music: Optional[Path], workdir: Path, out_path: Path,
                 overlay: Optional[Path] = None,
                 badge: Optional[Path] = None,
                 popup: Optional[Path] = None,
                 popup_windows: Optional[List[Tuple[float, float]]] = None,
                 popup_full_duration: bool = False,
                 cut_after_beat: Optional[int] = None,
                 log=print,
                 gpu_encode: bool = False,
                 beat_workers: int = 1,
                 on_beat_progress: Optional[Callable[[int, int], None]] = None
                 ) -> Path:
    """Composite every beat, then stitch.

    If ``cut_after_beat`` is set (0-based index of the last free-half beat),
    stitch **part 1** first (beats 0..cut inclusive → ``final_part1.mp4``),
    then **part 2** (the rest → ``final_part2.mp4``). ``final.mp4`` is set
    to part 1 for the default player (full free video). The paid half is
    only in ``final_part2.mp4``.

    ``popup`` is burned after stitching. Standard Spotify cards use
    ``popup_windows`` (10s at start + 10s random in the first 10 minutes).
    Branded cards can set ``popup_full_duration`` to cover every exported
    frame, including both halves and the appended Patreon end clip.

    ``beat_workers`` &gt; 1 composites that many beats concurrently (each its
    own ffmpeg subprocess — CPU/hardware-encoder-bound, not memory-bound
    beyond the existing per-process RSS ceiling in ``_run()``).
    ``on_beat_progress(completed_count, total)`` fires after each beat
    finishes — report *count done*, not *which index just finished*, since
    with workers &gt; 1 completions arrive out of order.
    """
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
    popup_width = int(v.get("spotify_popup_width", 420))
    popup_margin = int(v.get("spotify_popup_margin", 28))

    layout = v.get("layout", "standard")
    n = len(beats)
    clips: List[Optional[Path]] = [None] * n

    # dust/star overlay on ~half the still images (alternating) — footage
    # keeps the profile's blanket behavior. Precomputed per-index BEFORE
    # dispatching workers: this alternation must stay deterministic, which
    # it can't if computed live inside a worker once beats finish out of
    # submission order under beat_workers > 1.
    beat_overlays: List[Optional[Path]] = [None] * n
    img_i = 0
    for i, b in enumerate(beats):
        if b.get("kind") == "image":
            beat_overlays[i] = overlay if img_i % 2 == 0 else None
            img_i += 1
        else:
            beat_overlays[i] = overlay

    progress_lock = threading.Lock()
    completed = 0

    def _tick() -> None:
        nonlocal completed
        with progress_lock:
            completed += 1
            done = completed
        if on_beat_progress:
            on_beat_progress(done, n)

    def _composite_one(i: int) -> Path:
        b = beats[i]
        clip = workdir / f"beat_{i:03d}.mp4"
        # Resume: skip beats already on disk from a prior partial render.
        # (If Spotify/QR toggles change mid-resume, delete beat_*.mp4 first.)
        if clip.exists() and clip.stat().st_size > 10_000:
            log(f"  beat {i+1}/{n}: reusing existing composite")
            _tick()
            return clip
        log(f"  beat {i+1}/{n} ({b.get('kind')}, {b.get('duration', 0):.1f}s)")
        # A beat composite can hit an intermittent ffmpeg filtergraph
        # scheduling issue (reproduced: identical inputs, identical code,
        # succeeded on one run and hit the memory-runaway watchdog on the
        # next) — not a deterministic function of the beat's data, so
        # restructuring the graph further won't reliably prevent it. Retry
        # the same beat a couple of times before giving up; the watchdog in
        # _run() already guarantees a failed attempt is killed cleanly.
        # Spotify popup is NOT burned per-beat (would be permanent); it is
        # applied after stitch via burn_timed_popup().
        for attempt in range(3):
            try:
                if layout == "classic":
                    render_beat_classic(b, clip, width, height, fps, ken_burns,
                                        figure=figure, figure_frac=fig_frac,
                                        figure_margin=fig_margin,
                                        bg_blur=int(v.get("bg_blur", 8)),
                                        preset=v.get("encode_preset", "veryfast"),
                                        badge=badge, badge_width=badge_width,
                                        badge_margin=badge_margin,
                                        popup=None,
                                        workdir=workdir, profile_video=v,
                                        gpu=gpu_encode)
                else:
                    render_beat(b, clip, width, height, fps, ken_burns,
                                overlay=beat_overlays[i], overlay_opacity=ov_opacity,
                                pad_head=tr_sec if (tr_on and i > 0) else 0.0,
                                pad_tail=tr_sec if (tr_on and i < n - 1) else 0.0,
                                figure=figure, figure_frac=fig_frac,
                                figure_margin=fig_margin, sub_margin_r=sub_margin_r,
                                badge=badge, badge_width=badge_width,
                                badge_margin=badge_margin,
                                popup=None,
                                workdir=workdir, profile_video=v,
                                gpu=gpu_encode)
                break
            except RuntimeError as e:
                clip.unlink(missing_ok=True)
                (workdir / f"{clip.stem}_bgprecomp.mp4").unlink(missing_ok=True)
                (workdir / f"{clip.stem}_waveprecomp.mp4").unlink(missing_ok=True)
                if attempt == 2:
                    raise
                log(f"  beat {i+1}/{n}: composite failed ({e}) — retrying "
                    f"({attempt + 2}/3)")
        _tick()
        return clip

    # Pool workers are separate threads and do NOT inherit the job thread's
    # cancel token, so hand it over explicitly — otherwise every beat's
    # ffmpeg would be untracked and would survive a Stop.
    _tok = cancel.current()
    with ThreadPoolExecutor(max_workers=max(1, beat_workers)) as pool:
        futures = {pool.submit(cancel.adopt(_tok, _composite_one), i): i
                   for i in range(n)}
        for fut, i in futures.items():
            clips[i] = fut.result()  # re-raises on failure, aborting the render

    def _burn_popup(dest: Path, label: str,
                    windows: Optional[List[Tuple[float, float]]] = None) -> None:
        if not popup or not popup.exists():
            return
        resolved = resolve_popup_windows(
            probe_duration(dest), windows or popup_windows,
            full_duration=popup_full_duration)
        if not resolved:
            return
        timing = "full duration" if popup_full_duration else f"{len(resolved)} windows"
        log(f"  {label}: burning Spotify popup ({timing})")
        burn_timed_popup(dest, popup, dest, resolved,
                         popup_width=popup_width,
                         popup_margin=popup_margin,
                         workdir=workdir, gpu=gpu_encode)

    def _stitch(clips_subset: List[Path], dest: Path, label: str,
                *, apply_popup: bool = True) -> None:
        if tr_on:
            log(f"  {label}: assembling with crossfades + music")
            crossfade_concat(clips_subset, music, dest, tr_sec, tr_types,
                             workdir=workdir, log=log, gpu=gpu_encode)
        else:
            log(f"  {label}: concatenating + music")
            concat_with_music(clips_subset, music, dest, workdir)
        if apply_popup:
            _burn_popup(dest, label)

    # ── stitch ──────────────────────────────────────────────────────────
    # Patreon split: free half first, paid half second (two files).
    cut = cut_after_beat
    if (cut is not None and isinstance(cut, int)
            and 0 <= cut < len(clips) - 1):
        part1 = out_path.parent / "final_part1.mp4"
        part2 = out_path.parent / "final_part2.mp4"
        n1, n2 = cut + 1, len(clips) - (cut + 1)
        log(f"  Patreon split: free half first "
            f"(beats 1–{n1} → {part1.name})")
        _stitch(clips[: cut + 1], part1, "free half",
                apply_popup=not popup_full_duration)
        # End of free half: hard-cut Patreon CTA clip (branding/patreon-end.mp4)
        end_clip = default_patreon_end_clip()
        if end_clip is not None:
            try:
                append_end_clip(part1, end_clip, part1, workdir, log=log,
                                gpu=gpu_encode)
                log(f"  free half: appended Patreon end clip "
                    f"({end_clip.name}, {probe_duration(end_clip):.1f}s)")
            except Exception as e:  # noqa: BLE001
                log(f"  free half: Patreon end clip failed ({e}) — "
                    f"continuing without it")
        else:
            log("  free half: no branding/patreon-end.mp4 — skip end CTA")
        if popup_full_duration:
            _burn_popup(part1, "free half including Patreon end")
        log(f"  Patreon split: paid half next "
            f"(beats {n1 + 1}–{len(clips)} → {part2.name})")
        # Paid half has its own timeline → own 10s open + random-in-10min
        if tr_on:
            crossfade_concat(clips[cut + 1 :], music, part2, tr_sec, tr_types,
                             workdir=workdir, log=log, gpu=gpu_encode)
        else:
            concat_with_music(clips[cut + 1 :], music, part2, workdir)
        if popup and popup.exists() and popup_full_duration:
            _burn_popup(part2, "paid half")
        elif popup and popup.exists():
            w2 = plan_spotify_windows(
                probe_duration(part2),
                seed=(hash(str(part2.name)) & 0xFFFFFFFF) or 1)
            log(f"  paid half: Spotify popup at {w2}")
            burn_timed_popup(part2, popup, part2, w2,
                             popup_width=popup_width,
                             popup_margin=popup_margin,
                             workdir=workdir, gpu=gpu_encode)
        # Default player / download uses the free half (with Patreon end clip).
        # os.link, NOT Path.hardlink_to: the latter only exists on Python 3.10+
        # and this runs on 3.9, where it raised AttributeError straight into
        # the except below — silently copying a full second 1-2 GB of video
        # for every Patreon-split job instead of linking it.
        try:
            if out_path.exists() or out_path.is_symlink():
                out_path.unlink()
            os.link(part1, out_path)
        except Exception:  # noqa: BLE001
            shutil.copy2(part1, out_path)
        log(f"  free half → {part1.name}, paid half → {part2.name} "
            f"({n1} + {n2} beats)")
        return out_path

    _stitch(clips, out_path, "full video")
    return out_path
