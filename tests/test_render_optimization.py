"""Render regressions with isolated temporary media and no application import.

Real FFmpeg smoke tests are opt-in:
  DMIQWI_RENDER_FFMPEG_TESTS=1 .venv/bin/python -m unittest discover \
      -s tests -p test_render_optimization.py -v
"""
import json
import os
import shutil
import subprocess
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import render


def metadata(duration="4.000"):
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "profile": "High", "pix_fmt": "yuv420p", "width": 640,
             "height": 360, "r_frame_rate": "30/1",
             "avg_frame_rate": "30/1", "nb_frames": "120",
             "start_time": "0.000",
             "duration": duration},
            {"index": 1, "codec_type": "audio", "codec_name": "aac",
             "profile": "LC", "sample_rate": "44100", "channels": 2,
             "start_time": "0.000", "duration": duration},
        ],
        "format": {"nb_streams": 2, "duration": duration},
    }


def response(data=None, returncode=0):
    return SimpleNamespace(stdout=json.dumps(data if data is not None else metadata()),
                           stderr="", returncode=returncode)


class MetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "media.mp4"
        self.path.write_bytes(b"x" * 20_000)
        cache = patch.object(render, "_PROBE_CACHE", OrderedDict())
        cache.start()
        self.addCleanup(cache.stop)

    def test_all_common_probes_share_one_subprocess(self):
        with patch.object(render.subprocess, "run", return_value=response()) as probe:
            self.assertEqual(render.probe_duration(self.path), 4.0)
            self.assertEqual(render.probe_stream_duration(self.path, "video"), 4.0)
            self.assertEqual(render.probe_stream_duration(self.path, "audio"), 4.0)
            self.assertEqual(render.probe_video_size(self.path), (640, 360, 30.0))
            self.assertTrue(render.is_valid_audio(self.path))
            self.assertTrue(render.is_valid_video(self.path))
            self.assertEqual(render._validate_spotify_export(self.path, 30)[
                "video"]["codec_name"], "h264")
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.kwargs["timeout"], 30)
        self.assertIn("json", probe.call_args.args[0])

    def test_rewritten_file_invalidates_duration(self):
        with patch.object(render.subprocess, "run", side_effect=[
                response(), response(metadata("8.000"))]) as probe:
            self.assertEqual(render.probe_duration(self.path), 4.0)
            self.path.write_bytes(b"y" * 20_100)
            self.assertEqual(render.probe_duration(self.path), 8.0)
        self.assertEqual(probe.call_count, 2)

    def test_atomic_replacement_with_same_size_and_mtime_invalidates(self):
        with patch.object(render.subprocess, "run", side_effect=[
                response(), response(metadata("9.000"))]) as probe:
            self.assertEqual(render.probe_duration(self.path), 4.0)
            original = self.path.stat()
            replacement = self.root / "replacement.mp4"
            replacement.write_bytes(b"z" * 20_000)
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(replacement, self.path)
            self.assertEqual(render.probe_duration(self.path), 9.0)
        self.assertEqual(probe.call_count, 2)

    def test_failed_probe_is_not_cached(self):
        with patch.object(render.subprocess, "run", side_effect=[
                response(returncode=1), response()]) as probe:
            self.assertEqual(render.probe_duration(self.path), 0.0)
            self.assertEqual(render.probe_duration(self.path), 4.0)
        self.assertEqual(probe.call_count, 2)

    def test_timeout_is_bounded_and_retried(self):
        with patch.object(render.subprocess, "run", side_effect=[
                subprocess.TimeoutExpired("ffprobe", 30), response()]) as probe:
            self.assertEqual(render.probe_duration(self.path), 0.0)
            self.assertEqual(render.probe_duration(self.path), 4.0)
        self.assertEqual(probe.call_count, 2)

    def test_file_changed_during_probe_is_not_published(self):
        def probe_while_writing(*_args, **_kwargs):
            self.path.write_bytes(b"y" * 20_100)
            return response()

        with patch.object(render.subprocess, "run", side_effect=probe_while_writing):
            self.assertEqual(render.probe_duration(self.path), 0.0)
        with patch.object(render.subprocess, "run", return_value=response(
                metadata("8.000"))) as probe:
            self.assertEqual(render.probe_duration(self.path), 8.0)
            self.assertEqual(probe.call_count, 1)

    def test_missing_file_never_launches_probe_or_uses_old_cache(self):
        with patch.object(render.subprocess, "run", return_value=response()) as probe:
            self.assertEqual(render.probe_duration(self.path), 4.0)
            self.path.unlink()
            self.assertEqual(render.probe_duration(self.path), 0.0)
        self.assertEqual(probe.call_count, 1)

    def test_audio_validation_does_not_substitute_container_duration(self):
        data = metadata()
        data["streams"] = data["streams"][:1]
        with patch.object(render.subprocess, "run", return_value=response(data)):
            self.assertFalse(render.is_valid_audio(self.path))
            self.assertEqual(render.probe_stream_duration(self.path, "audio"), 0)
            self.assertEqual(render.probe_duration(self.path), 4.0)

    def test_frame_count_gives_exact_video_duration(self):
        data = metadata("4.125")
        with patch.object(render.subprocess, "run", return_value=response(data)):
            self.assertEqual(render.probe_video_duration_exact(self.path), 4.0)

    def test_nonfinite_durations_fail_closed(self):
        for invalid in ("NaN", "inf", "-1", "N/A", None):
            with self.subTest(duration=invalid), \
                    patch.object(render, "_PROBE_CACHE", OrderedDict()), \
                    patch.object(render.subprocess, "run", return_value=response(
                        metadata(invalid))):
                self.assertEqual(render.probe_duration(self.path), 0)
                self.assertFalse(render.is_valid_audio(self.path))

    def test_malformed_probe_structure_is_not_cached(self):
        for invalid in ([], {"format": []}, {"streams": [None]},
                        {"streams": "video"}):
            with self.subTest(data=invalid), \
                    patch.object(render.subprocess, "run", side_effect=[
                        response(invalid), response()]) as probe:
                render._PROBE_CACHE.clear()
                self.assertEqual(render.probe_duration(self.path), 0)
                self.assertEqual(render.probe_duration(self.path), 4)
                self.assertEqual(probe.call_count, 2)

    def test_cache_is_bounded_and_recently_used_entries_survive(self):
        paths = [self.root / f"{i}.mp4" for i in range(3)]
        for path in paths:
            path.write_bytes(b"x" * 20_000)
        with patch.object(render, "_PROBE_CACHE_LIMIT", 2), \
                patch.object(render.subprocess, "run", return_value=response()) as probe:
            for i in (0, 1, 0, 2, 0):
                render.probe_duration(paths[i])
            self.assertEqual(probe.call_count, 3)
            render.probe_duration(paths[1])
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(len(render._PROBE_CACHE), 2)

    def test_arbitrary_stream_selectors_keep_ffprobe_behavior(self):
        with patch.object(render.subprocess, "run", return_value=SimpleNamespace(
                stdout="12.5\n")) as probe:
            self.assertEqual(render.probe_stream_duration(self.path, "a:1"), 12.5)
            self.assertIn("a:1", probe.call_args.args[0])


class WatchdogTests(unittest.TestCase):
    def test_finished_process_wakes_watchdog_without_fixed_sleep(self):
        # Synchronize the fake process with the watchdog, so this cannot pass
        # merely because FFmpeg exited before the thread started checking it.
        reached_watchdog = threading.Event()

        class FakeProcess:
            pid = 99999
            returncode = None

            def poll(self):
                return self.returncode

            def communicate(self):
                if not reached_watchdog.wait(2):
                    raise AssertionError("watchdog did not start")
                self.returncode = 0
                return "", ""

        def rss(_pid):
            reached_watchdog.set()
            return 1

        with patch.object(render.subprocess, "Popen", return_value=FakeProcess()), \
                patch.object(render, "_rss_mb", side_effect=rss), \
                patch.object(render.time, "sleep") as fixed_sleep:
            render._run(["ffmpeg", "-version"])
        fixed_sleep.assert_not_called()

    def test_memory_watchdog_still_kills_runaway(self):
        killed = threading.Event()

        class FakeProcess:
            pid = 99999
            returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9
                killed.set()

            def communicate(self):
                if not killed.wait(2):
                    raise AssertionError("runaway was not killed")
                return "", ""

        with patch.object(render.subprocess, "Popen", return_value=FakeProcess()), \
                patch.object(render, "_rss_mb", return_value=4096):
            with self.assertRaisesRegex(RuntimeError, "memory runaway"):
                render._run(["ffmpeg", "-version"])


class ClassicWaveformOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / "voice.mp3"
        self.audio.write_bytes(b"synthetic audio")
        self.bg = self.root / "background.mp4"
        self.profile = {"waveform": {
            "rate": 15, "color": "0xFAF6EF@0.9", "width_frac": 0.46,
            "height_frac": 0.3, "n": 5, "scale": "sqrt",
        }}

    def render_classic(self, *, live=True, single=False):
        commands = []
        with patch.object(render, "is_valid_audio", return_value=True), \
                patch.object(render, "_precompute_bg_clip",
                             return_value=self.bg) as background, \
                patch.object(render, "_precompute_wave_clip",
                             return_value=self.root / "wave.mp4") as wave, \
                patch.object(render, "_run",
                             side_effect=lambda command: commands.append(command)):
            render.render_beat_classic(
                {"duration": 10, "kind": "blank", "audio": self.audio},
                self.root / "beat.mp4", 1920, 1080, 30, False,
                workdir=self.root, profile_video=self.profile,
                live_waveform=live,
                single_pass=single,
            )
        return commands, wave, background

    def test_live_waveform_removes_intermediate_encode(self):
        commands, wave, _ = self.render_classic(live=True)
        wave.assert_not_called()
        self.assertEqual(len(commands), 1)
        graph = commands[0][commands[0].index("-filter_complex") + 1]
        self.assertIn("asplit=2[wavea][amix]", graph)
        self.assertIn("showwaves=", graph)
        self.assertEqual(commands[0].count(str(self.audio)), 1)

    def test_safe_mode_keeps_precomputed_waveform_path(self):
        commands, wave, _ = self.render_classic(live=False)
        wave.assert_called_once()
        graph = commands[0][commands[0].index("-filter_complex") + 1]
        self.assertNotIn("showwaves=", graph)
        self.assertIn("[2:a]anull[amix]", graph)

    def test_single_pass_builds_background_in_the_main_graph(self):
        commands, wave, background = self.render_classic(
            live=True, single=True)
        background.assert_not_called()
        wave.assert_not_called()
        command = commands[0]
        self.assertIn("-filter_buffered_frames", command)
        self.assertEqual(
            command[command.index("-filter_buffered_frames") + 1], "300")
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("boxblur=8:1[bg]", graph)
        self.assertIn("showwaves=", graph)

    def test_precomputed_fallback_does_not_encode_duplicate_fps(self):
        commands = []
        with patch.object(render, "_run",
                          side_effect=lambda command: commands.append(command)):
            render._precompute_wave_clip(
                self.audio, 10, 882, 324, 30, "white", 15, "sqrt", 5,
                self.root / "wave.mp4",
            )
        command = commands[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertNotIn("fps=30", graph)
        self.assertNotIn("-r", command)


class FilterBufferCapabilityTests(unittest.TestCase):
    def test_capability_is_detected_once_and_cached(self):
        reply = SimpleNamespace(
            stdout="-filter_buffered_frames maximum buffered frames",
            stderr="", returncode=0)
        with patch.object(render, "_FILTER_BUFFER_CAP_SUPPORTED", None), \
                patch.object(render.subprocess, "run",
                             return_value=reply) as probe:
            self.assertTrue(render._supports_filter_buffer_cap())
            self.assertTrue(render._supports_filter_buffer_cap())
        probe.assert_called_once()

    def test_old_ffmpeg_fails_closed(self):
        reply = SimpleNamespace(stdout="generic ffmpeg help", stderr="",
                                returncode=0)
        with patch.object(render, "_FILTER_BUFFER_CAP_SUPPORTED", None), \
                patch.object(render.subprocess, "run", return_value=reply):
            self.assertFalse(render._supports_filter_buffer_cap())


class ClassicFallbackTests(unittest.TestCase):
    def test_failure_steps_down_from_single_to_live_to_precomputed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "voice.mp3"
            audio.write_bytes(b"audio")
            calls = []

            def composite(_beat, out, *_args, **kwargs):
                calls.append((kwargs["single_pass"], kwargs["live_waveform"]))
                if len(calls) == 1:
                    raise RuntimeError("single-pass filter rejected")
                if len(calls) == 2:
                    raise RuntimeError("ffmpeg memory runaway: test ceiling")
                out.write_bytes(b"valid composite")

            def stitch(_clips, _music, dest, _workdir):
                dest.write_bytes(b"stitched")

            profile = {"video": {
                "layout": "classic", "width": 320, "height": 180,
                "fps": 30, "transitions": {"enabled": False},
            }}
            with patch.object(render, "ensure_ffmpeg"), \
                    patch.object(render, "_supports_filter_buffer_cap",
                                 return_value=True), \
                    patch.object(render, "render_beat_classic",
                                 side_effect=composite), \
                    patch.object(render, "concat_with_music",
                                 side_effect=stitch), \
                    patch.object(render, "encode_final_export"), \
                    patch.object(render.artifacts, "matches",
                                 return_value=False), \
                    patch.object(render.artifacts, "publish"), \
                    patch.object(render.config, "get",
                                 side_effect=lambda _key, default=None: default):
                render.render_video(
                    [{"duration": 1, "kind": "blank", "audio": audio}],
                    profile, None, root, root / "final.mp4",
                )

            self.assertEqual(calls, [
                (True, True), (False, True), (False, False),
            ])


class SingleClipAssemblyTests(unittest.TestCase):
    def test_single_clip_copies_exact_file_without_launching_ffmpeg(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip, out = root / "beat_000.mp4", root / "final.mp4"
            clip.write_bytes(b"unaltered video and audio")
            with patch.object(render, "_run") as encode:
                render.crossfade_concat([clip], None, out, 0.5, ["fade"])
            encode.assert_not_called()
            self.assertEqual(clip.read_bytes(), out.read_bytes())

    def test_single_clip_at_output_path_is_not_overwritten(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "final.mp4"
            clip.write_bytes(b"original")
            render.crossfade_concat([clip], None, clip, 0.5, ["fade"])
            self.assertEqual(clip.read_bytes(), b"original")

    def test_single_clip_keeps_music_mix_without_video_reencode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip, out, music = (root / name for name in
                                ("beat_000.mp4", "final.mp4", "music.wav"))
            music.write_bytes(b"fake music")
            with patch.object(render, "_run") as encode, \
                    patch.object(render, "probe_duration", return_value=4):
                render.crossfade_concat([clip], music, out, 0.5, ["fade"],
                                        music_volume=0.08)
            command = encode.call_args.args[0]
            self.assertEqual(command[command.index("-c:v") + 1], "copy")
            self.assertIn("volume=0.08", command[command.index("-filter_complex") + 1])
            self.assertIn("-c:a", command)


class FinalExportFastPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / "final.mp4"
        self.video.write_bytes(b"compatible stitched video")
        self.contract = {
            "video": {"width": 1920, "height": 1080,
                      "pix_fmt": "yuv420p", "profile": "High"},
            "audio": {"profile": "LC"},
        }

    @staticmethod
    def _fake_ffmpeg(commands):
        def run(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"encoded")
        return run

    def test_balanced_compatible_export_copies_video_stream(self):
        commands = []
        with patch.object(render, "probe_video_duration_exact", return_value=10.0), \
                patch.object(render, "_can_copy_final_video", return_value=True), \
                patch.object(render, "_run", side_effect=self._fake_ffmpeg(commands)), \
                patch.object(render, "_validate_narration_master"), \
                patch.object(render, "_validate_spotify_export",
                             return_value=self.contract):
            render.encode_final_export(self.video, self.root)

        self.assertEqual(len(commands), 2)
        final = commands[1]
        self.assertEqual(final[final.index("-c:v") + 1], "copy")
        self.assertNotIn("-vf", final)

    def test_rejected_stream_copy_falls_back_to_full_normalization(self):
        commands = []
        with patch.object(render, "probe_video_duration_exact", return_value=10.0), \
                patch.object(render, "_can_copy_final_video", return_value=True), \
                patch.object(render, "_run", side_effect=self._fake_ffmpeg(commands)), \
                patch.object(render, "_validate_narration_master"), \
                patch.object(render, "_validate_spotify_export", side_effect=[
                    RuntimeError("copy contract mismatch"), self.contract]):
            render.encode_final_export(self.video, self.root)

        self.assertEqual(len(commands), 3)
        fallback = commands[2]
        self.assertIn("-vf", fallback)
        self.assertIn("fps=fps=30", fallback[fallback.index("-vf") + 1])
        self.assertNotEqual(fallback[fallback.index("-c:v") + 1], "copy")


class TransitionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clips = [self.root / f"beat_{i:03d}.mp4" for i in range(12)]
        for clip in self.clips:
            clip.write_bytes(b"source media")
        self.out = self.root / "final_part1.mp4"

    def run_assembly(self, *, clips=None, seconds=0.5, types=None, gpu=False,
                     fail_final=False):
        calls = []

        def encode(group, _music, out, *_args, **_kwargs):
            calls.append(out.name)
            self.assertLessEqual(len(group), render.MAX_XFADE_INPUTS)
            if fail_final and out == self.out:
                raise RuntimeError("simulated interrupted final assembly")
            out.write_bytes(b"encoded media" * 1000)

        with patch.object(render, "_xfade_chain", side_effect=encode), \
                patch.object(render, "is_valid_video", return_value=True):
            if fail_final:
                with self.assertRaisesRegex(RuntimeError, "interrupted final"):
                    render.crossfade_concat(clips or self.clips, None, self.out,
                                            seconds, types or ["fade"],
                                            gpu=gpu, workdir=self.root)
            else:
                render.crossfade_concat(clips or self.clips, None, self.out,
                                        seconds, types or ["fade"], gpu=gpu,
                                        workdir=self.root)
        return calls

    def test_matching_retry_reuses_completed_groups_and_cleans_manifests(self):
        self.assertEqual(self.run_assembly(fail_final=True),
                         ["xseg_00.mp4", "xseg_01.mp4", self.out.name])
        self.assertTrue((self.root / "xseg_00.cache.json").exists())
        self.assertEqual(self.run_assembly(), [self.out.name])
        self.assertFalse(list(self.root.glob("xseg*")))

    def test_different_patreon_half_cannot_reuse_previous_half_groups(self):
        self.run_assembly(fail_final=True)
        second_half = [self.root / f"beat_{i:03d}.mp4" for i in range(12, 24)]
        for clip in second_half:
            clip.write_bytes(b"different narration and video")
        self.out = self.root / "final_part2.mp4"
        self.assertEqual(self.run_assembly(clips=second_half),
                         ["xseg_00.mp4", "xseg_01.mp4", self.out.name])

    def test_source_change_rebuilds_only_affected_group(self):
        self.run_assembly(fail_final=True)
        self.clips[0].write_bytes(b"replacement media")
        self.assertEqual(self.run_assembly(), ["xseg_00.mp4", self.out.name])

    def test_transition_duration_change_invalidates_groups(self):
        self.run_assembly(fail_final=True)
        self.assertEqual(self.run_assembly(seconds=0.25),
                         ["xseg_00.mp4", "xseg_01.mp4", self.out.name])

    def test_transition_type_change_invalidates_groups(self):
        self.run_assembly(fail_final=True)
        self.assertEqual(self.run_assembly(types=["smoothleft"]),
                         ["xseg_00.mp4", "xseg_01.mp4", self.out.name])

    def test_encoding_mode_change_invalidates_groups(self):
        self.run_assembly(fail_final=True)
        self.assertEqual(self.run_assembly(gpu=True),
                         ["xseg_00.mp4", "xseg_01.mp4", self.out.name])

    def test_missing_legacy_manifest_rebuilds_group(self):
        self.run_assembly(fail_final=True)
        (self.root / "xseg_00.cache.json").unlink()
        self.assertEqual(self.run_assembly(), ["xseg_00.mp4", self.out.name])

    def test_corrupt_manifest_rebuilds_group(self):
        self.run_assembly(fail_final=True)
        (self.root / "xseg_00.cache.json").write_text("not json")
        self.assertEqual(self.run_assembly(), ["xseg_00.mp4", self.out.name])

    def test_replaced_cached_output_is_not_trusted(self):
        self.run_assembly(fail_final=True)
        (self.root / "xseg_00.mp4").write_bytes(b"partial")
        self.assertEqual(self.run_assembly(), ["xseg_00.mp4", self.out.name])

    def test_matching_manifest_still_requires_probe_validation(self):
        self.run_assembly(fail_final=True)
        cached = self.root / "xseg_00.mp4"
        contract = json.loads(cached.with_suffix(".cache.json").read_text())["contract"]
        with patch.object(render, "is_valid_video", return_value=False) as validate:
            self.assertFalse(render._reusable_xfade(cached, contract))
            validate.assert_called_once_with(cached)

    def test_large_hierarchy_remains_bounded_and_resumable(self):
        clips = [self.root / f"large_{i:03d}.mp4" for i in range(156)]
        for clip in clips:
            clip.write_bytes(b"synthetic source")
        calls = self.run_assembly(clips=clips, fail_final=True)
        self.assertEqual(len(calls), 19)
        self.assertIn("xseg_l1_00.mp4", calls)
        self.assertEqual(self.run_assembly(clips=clips), [self.out.name])

    def test_source_change_during_encoding_does_not_publish_manifest(self):
        def encode(group, _music, out, *_args, **_kwargs):
            if out == self.out:
                raise RuntimeError("simulated interrupted final assembly")
            out.write_bytes(b"encoded" * 3000)
            if out.name == "xseg_00.mp4":
                group[0].write_bytes(b"modified during encoding")

        with patch.object(render, "_xfade_chain", side_effect=encode):
            with self.assertRaisesRegex(RuntimeError, "interrupted final"):
                render.crossfade_concat(self.clips, None, self.out, 0.5,
                                        ["fade"], workdir=self.root)
        self.assertFalse((self.root / "xseg_00.cache.json").exists())
        self.assertTrue((self.root / "xseg_01.cache.json").exists())


@unittest.skipUnless(os.environ.get("DMIQWI_RENDER_FFMPEG_TESTS") == "1" and
                     shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "opt-in local FFmpeg smoke test")
class FFmpegSmokeTests(unittest.TestCase):
    @staticmethod
    def _video_hash(path):
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v",
            "-c:v", "copy", "-f", "hash", "-hash", "sha256", "-",
        ], capture_output=True, text=True, check=True, timeout=30)
        return result.stdout.strip()

    def test_compliant_balanced_export_preserves_video_bitstream(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "final.mp4"
            render._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=30:duration=1",
                "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=44100:duration=1",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-bf", "0",
                "-video_track_timescale", "15360", "-c:a", "aac", "-ac", "2",
                str(out),
            ])
            before = self._video_hash(out)
            messages = []
            render.encode_final_export(out, root, log=messages.append)
            self.assertEqual(self._video_hash(out), before)
            self.assertTrue(any("fast final pass" in line for line in messages))
            render._validate_spotify_export(out, 30)

    def test_single_clip_preserves_video_and_final_spotify_contract(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip, out = root / "beat_000.mp4", root / "final.mp4"
            render._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-bf", "0", "-c:a", "aac", "-ac", "2", str(clip),
            ])
            render.crossfade_concat([clip], None, out, 0.5, ["fade"])
            self.assertAlmostEqual(render.probe_stream_duration(out, "video"), 1, places=2)

            self.assertEqual(self._video_hash(clip), self._video_hash(out))
            render.encode_final_export(out, root, fps=30, quality="balanced", gpu=False)
            contract = render._validate_spotify_export(out, 30)
            self.assertEqual(contract["video"]["profile"], "High")
            self.assertEqual(contract["audio"]["profile"], "LC")
            self.assertLessEqual(abs(float(contract["audio"]["duration"]) -
                                     float(contract["video"]["duration"])), 0.05)
            self.assertFalse((root / "final_voice_all.wav").exists())

    def test_classic_single_pass_has_exact_tracks_and_no_intermediates(self):
        if not render._supports_filter_buffer_cap():
            self.skipTest("FFmpeg lacks -filter_buffered_frames")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, out = root / "voice.wav", root / "beat.mp4"
            render._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=44100:duration=1",
                "-c:a", "pcm_s16le", str(audio),
            ])
            render.render_beat_classic(
                {"duration": 1, "kind": "blank", "audio": audio},
                out, 320, 180, 30, False, workdir=root, gpu=False,
                single_pass=True,
            )
            self.assertTrue(render.is_valid_video(out, 0.9))
            self.assertAlmostEqual(
                render.probe_stream_duration(out, "video"), 1, places=2)
            self.assertAlmostEqual(
                render.probe_stream_duration(out, "audio"), 1, places=2)
            self.assertFalse((root / "beat_bgprecomp.mp4").exists())
            self.assertFalse((root / "beat_waveprecomp.mp4").exists())


if __name__ == "__main__":
    unittest.main()
