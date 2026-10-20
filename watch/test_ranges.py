"""No network or paid model calls: range lifecycle plus a small real FFmpeg fixture."""
import array
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from watch.engine import WatchService, MODEL, atomic_json
from watch.ranges import requested_range, resolve_range, cut_segment, clean_child_environment
from watch.test_watch import FakeService, complete


class RangeService(FakeService):
    def _download(self, bvid, part, directory, started, selection=None):
        result = super()._download(bvid, part, directory, started)
        if selection is not None:
            selection = resolve_range(selection, 600)
            result["evidence"].update(duration_seconds=selection[1] - selection[0],
                source_duration_seconds=600, source_time_range_seconds=selection)
        return result

    def _perceive(self, resource, question, started=None):
        self.last_question = question
        return super()._perceive(resource, question, started)


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="watch-range-test-")
        self.state = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_range_validation_and_legacy_cache_key_compatibility(self):
        service = RangeService(self.state, api_key="test-key-value")
        legacy = hashlib.sha256(json.dumps([1, MODEL, "BV0000000000", 1, ""], ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(service._key("BV0000000000", "", 1), legacy)
        for start, end in ((None, 20), (0, None), (-1, 2), (2, 2), (0, 181), (float("nan"), 2), (False, 2)):
            self.assertFalse(service.start("BV0000000000", start_seconds=start, end_seconds=end)["ok"])
        self.assertEqual(requested_range(0, 180), [0.0, 180.0])
        with self.assertRaisesRegex(ValueError, "VIDEO_DURATION_LIMIT_SPECIFY_TIME_RANGE"):
            resolve_range(None, 600)
        with self.assertRaisesRegex(ValueError, "VIDEO_TIME_RANGE_OUTSIDE_SOURCE"):
            resolve_range([599, 601], 600)
        self.assertNotEqual(service._key("BV0000000000", "", 1, [0.0, 180.0]), legacy)

    def test_segment_cache_upload_reuse_coverage_and_cleanup(self):
        service = RangeService(self.state, api_key="test-key-value")
        first = complete(service, service.start("BV0000000000", start_seconds=0, end_seconds=180))["result"]
        self.assertFalse(first["full_selected_part_covered"])
        self.assertEqual(first["source_duration_seconds"], 600)
        self.assertEqual(first["analysis_range_seconds"], [0, 180])
        self.assertEqual(first["uncovered_source_ranges_seconds"], [[180, 600]])
        self.assertEqual(first["completed_coverage_for_selected_part"]["next_uncovered_range_seconds"], [180, 360])
        second = complete(service, service.start("BV0000000000", start_seconds=180, end_seconds=360))["result"]
        self.assertEqual((service.downloads, service.uploads, service.perceptions), (2, 2, 2))
        self.assertEqual(second["analysis_range_seconds"], [180, 360])
        self.assertEqual(second["uncovered_source_ranges_seconds"], [[0, 180], [360, 600]])
        self.assertEqual(second["completed_coverage_for_selected_part"]["covered_ranges_seconds"], [[0, 360]])
        self.assertEqual(second["completed_coverage_for_selected_part"]["uncovered_ranges_seconds"], [[360, 600]])
        self.assertIn("文件内时间加180.0秒", service.last_question)
        cached = service.start("BV0000000000", start_seconds=180.0, end_seconds=360.0)
        self.assertTrue(cached["cache_hit"])
        followup = complete(service, service.start("BV0000000000", "动作？", start_seconds=180, end_seconds=360))
        self.assertTrue(followup["upload_reused"])
        self.assertEqual((service.downloads, service.uploads, service.perceptions), (2, 2, 3))
        denied = complete(service, service.start("BV0000000000", start_seconds=360, end_seconds=540))
        self.assertEqual(denied["error"], "WATCH_DAILY_API_CALL_LIMIT")
        self.assertEqual(list((service.root / "tmp").iterdir()), [])

    def test_old_completed_whole_record_survives_and_is_never_used_for_segment(self):
        service = RangeService(self.state, api_key="test-key-value")
        path = service.root / "analyses" / (service._key("BV0000000000", "", 1) + ".json")
        atomic_json(path, {"ok": True, "bvid": "BV0000000000", "part": 1, "model": MODEL,
            "duration_seconds": 20, "provider_request_id": "synthetic-original-receipt-id", "usage": {}})
        before = path.read_bytes()
        whole = service.start("BV0000000000")
        self.assertTrue(whole["cache_hit"])
        self.assertTrue(whole["full_selected_part_covered"])
        self.assertEqual(path.read_bytes(), before)
        segment = complete(service, service.start("BV0000000000", start_seconds=0, end_seconds=10))["result"]
        self.assertNotEqual(segment["provider_request_id"], "synthetic-original-receipt-id")
        self.assertFalse(segment["full_selected_part_covered"])
        self.assertEqual(path.read_bytes(), before)

    def test_private_remote_input_and_cookie_never_enter_command_arguments(self):
        directory = self.state / "clip"
        directory.mkdir()
        observed = {}
        def run(command, **kwargs):
            observed["command"] = command
            observed["env"] = kwargs["env"]
            self.assertNotIn("secret-signed-url", " ".join(command))
            self.assertNotIn("secret-cookie", " ".join(command))
            self.assertIn("http://127.0.0.1:", " ".join(command))
            (directory / "media.mp4").write_bytes(b"synthetic")
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "not-for-ffmpeg"}), patch("watch.ranges.subprocess.run", run):
            cut_segment("ffmpeg", [{"url": "https://example.invalid/secret-signed-url",
                "headers": {"Cookie": "secret-cookie"}}], directory, [20, 30],
                timeout=10, max_bytes=10000, deadline=lambda: None)
        self.assertNotIn("DASHSCOPE_API_KEY", observed["env"])
        self.assertEqual(list(directory.glob("*.headers")), [])
        self.assertEqual(list(directory.glob("*.url")), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg fixture requires installed tools")
    def test_real_ffmpeg_exact_continuous_video_and_audio_cut(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        source = self.state / "source.mp4"
        command = [ffmpeg, "-v", "error", "-y", "-filter_complex_threads", "1"]
        for color, frequency in (("red", 440), ("blue", 880), ("green", 1320)):
            command.extend(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=10:d=2",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=16000:duration=2"])
        command.extend(["-filter_complex", "[0:v][1:a][2:v][3:a][4:v][5:a]concat=n=3:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-threads:v", "1", "-g", "50",
            "-c:a", "aac", "-threads:a", "1", "-movflags", "+faststart", str(source)])
        flags = {"env": clean_child_environment(), "capture_output": True, "timeout": 30,
            "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
        generated = subprocess.run(command, **flags)
        self.assertEqual(generated.returncode, 0, generated.stderr.decode(errors="replace"))
        directory = self.state / "segment"
        directory.mkdir()
        actual_run = subprocess.run
        def inspect_fixture(command, **kwargs):
            result = actual_run(command, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            return result
        with patch("watch.ranges.subprocess.run", inspect_fixture):
            clip = cut_segment(ffmpeg, [{"url": str(source)}], directory, [2.2, 3.4],
                timeout=20, max_bytes=1024 * 1024, deadline=lambda: None)
        probed = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(clip)], **flags)
        data = json.loads(probed.stdout)
        self.assertAlmostEqual(float(data["format"]["duration"]), 1.2, delta=.1)
        self.assertEqual({stream["codec_type"] for stream in data["streams"]}, {"video", "audio"})
        frame = subprocess.run([ffmpeg, "-v", "error", "-i", str(clip), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], **flags).stdout
        self.assertTrue(frame)
        red, green, blue = frame[(45 * 160 + 80) * 3:(45 * 160 + 80) * 3 + 3]
        self.assertGreater(blue, 200)
        self.assertLess(red + green, 40)
        audio = subprocess.run([ffmpeg, "-v", "error", "-i", str(clip), "-vn", "-ac", "1",
            "-ar", "16000", "-f", "s16le", "pipe:1"], **flags).stdout
        samples = array.array("h", audio)[:16000]
        def energy(frequency):
            real = sum(value * math.cos(2 * math.pi * frequency * index / 16000) for index, value in enumerate(samples))
            imag = sum(value * math.sin(2 * math.pi * frequency * index / 16000) for index, value in enumerate(samples))
            return real * real + imag * imag
        self.assertGreater(energy(880), 50 * max(energy(440), energy(1320)))
        self.assertEqual(list(directory.glob("*.url")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
