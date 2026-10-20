"""Focused guard/transport/lifecycle checks. No external calls, media or charges."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from watch.engine import WatchService, WatchError, MODEL, process_alive


class FakeService(WatchService):
    downloads = 0
    uploads = 0
    perceptions = 0
    fail_perception = False

    def _download(self, bvid, part, directory, started):
        self.downloads += 1
        path = directory / "media.mp4"
        path.write_bytes(b"synthetic-video-not-for-provider")
        return {"path": path, "evidence": {"title": "synthetic", "duration_seconds": 20,
            "video_codec": "h264", "audio_codec": "aac", "part": part}}

    def _upload(self, media, started=None):
        self.uploads += 1
        assert media.exists()
        return "oss://private-test-resource/video.mp4"

    def _perceive(self, resource, question, started=None):
        self.perceptions += 1
        if self.fail_perception:
            raise WatchError("SYNTHETIC_PROVIDER_FAILURE")
        return {"text": "synthetic observations", "model": MODEL,
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            "request_id": "synthetic-no-paid-call"}


def complete(service, result):
    if not result.get("job_id"):
        return result
    for _ in range(200):
        result = service.status(result["job_id"])
        if result.get("status") != "running" and result.get("temporary_files_cleaned"):
            return result
        time.sleep(.01)
    raise AssertionError("Synthetic job did not finish")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="watch-test-")
        self.state = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_endpoint_and_input_are_bounded(self):
        with self.assertRaises(ValueError):
            WatchService(self.state, api_key="synthetic", config={"base_url": "https://example.org/compatible-mode/v1"})
        with self.assertRaises(ValueError):
            WatchService(self.state, api_key="synthetic", config={"max_duration_seconds": 181})
        service = FakeService(self.state, api_key="synthetic")
        self.assertEqual(service.start("https://evil.invalid/video")["error"], "INVALID_VIDEO_ID_OR_PART")
        self.assertEqual(service.start("BV0000000000", part=0)["error"], "INVALID_VIDEO_ID_OR_PART")
        self.assertEqual(service.start("BV0000000000", "x" * 2001)["error"], "QUESTION_TOO_LONG")
        self.assertEqual(service.downloads, 0)

    def test_success_cleanup_cache_and_restart_without_more_calls(self):
        service = FakeService(self.state, api_key="synthetic")
        first = complete(service, service.start("BV0000000000"))
        self.assertTrue(first["result"]["temporary_files_cleaned"])
        self.assertEqual(list((service.root / "tmp").iterdir()), [])
        self.assertEqual((service.downloads, service.uploads, service.perceptions), (1, 1, 1))
        result = service.start("BV0000000000")
        self.assertTrue(result["cache_hit"])
        self.assertFalse(result["new_api_call"])
        restarted = FakeService(self.state, api_key="synthetic")
        self.assertTrue(restarted.start("BV0000000000")["cache_hit"])
        self.assertEqual(restarted.perceptions, 0)

    def test_provenance_comes_from_original_response_and_cache_does_not_erase_it(self):
        service = WatchService(self.state, api_key="only-provider-secret-value")
        original = {"ok": True, "provider_request_id": "synthetic-original-request", "model": MODEL,
            "completed_at": "synthetic-original-time", "coverage": "entire_selected_part_file_submitted",
            "analysis_range_seconds": [0, 20], "usage": {"prompt_tokens_details": {"video_tokens": 30, "audio_tokens": 4}}}
        from watch.engine import atomic_json
        atomic_json(service.root / "analyses" / (service._key("BV0000000000", "", 1) + ".json"), original)
        cached = service.start("BV0000000000")
        provenance = cached["observation_provenance"]
        self.assertTrue(cached["cache_hit"])
        self.assertFalse(cached["new_api_call"])
        self.assertFalse(provenance["new_provider_request_in_this_call"])
        self.assertEqual(provenance["original_provider_request_id"], original["provider_request_id"])
        self.assertEqual(provenance["original_completed_at"], original["completed_at"])
        self.assertEqual(provenance["original_usage_evidence"]["video_tokens"], 30)
        self.assertEqual(provenance["original_usage_evidence"]["audio_tokens"], 4)
        self.assertEqual(cached["coverage"], original["coverage"])
        missing = service._observation_provenance({"ok": True, "model": MODEL}, reused=True)
        self.assertIsNone(missing["original_provider_request_id"])
        self.assertIsNone(missing["original_usage_evidence"]["video_tokens"])

    def test_followup_reuses_upload_but_has_real_call_count(self):
        service = FakeService(self.state, api_key="synthetic")
        complete(service, service.start("BV0000000000"))
        result = complete(service, service.start("BV0000000000", "抬爪的时间？"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["upload_reused"])
        self.assertEqual((service.downloads, service.uploads, service.perceptions), (1, 1, 2))
        service.config["daily_api_calls"] = 2
        denied = complete(service, service.start("BV0000000000", "new question"))
        self.assertEqual(denied["error"], "WATCH_DAILY_API_CALL_LIMIT")
        self.assertEqual(service.perceptions, 2)

    def test_provider_failure_cleans_media_and_does_not_auto_retry(self):
        service = FakeService(self.state, api_key="synthetic")
        service.fail_perception = True
        result = complete(service, service.start("BV0000000000"))
        self.assertEqual(result["error"], "SYNTHETIC_PROVIDER_FAILURE")
        self.assertTrue(result["temporary_files_cleaned"])
        self.assertEqual(service.perceptions, 1)
        self.assertEqual(list((service.root / "tmp").iterdir()), [])

    def test_secret_and_private_reference_never_enter_results(self):
        os.environ["DASHSCOPE_API_KEY"] = "synthetic-inherited-secret"
        service = FakeService(self.state)
        self.assertNotIn("DASHSCOPE_API_KEY", os.environ)
        (self.state / "bili_credential.json").write_text(json.dumps({"sessdata": "synthetic-cookie"}))
        result = service._clean_output({"text": "synthetic-inherited-secret synthetic-cookie oss://private-path/file.mp4"})
        text = json.dumps(result)
        self.assertNotIn("synthetic-inherited-secret", text)
        self.assertNotIn("synthetic-cookie", text)
        self.assertNotIn("oss://", text)

    def test_actual_wire_has_full_video_resource_audio_capable_model_and_usage(self):
        requests = []

        def handle(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(request.headers["x-dashscope-ossresourceresolve"], "enable")
            self.assertEqual(body["model"], MODEL)
            self.assertEqual(body["modalities"], ["text"])
            self.assertEqual(body["messages"][0]["content"][0],
                {"type": "video_url", "video_url": {"url": "oss://synthetic/video.mp4"}})
            self.assertNotIn("audio", body)  # no output audio generation parameter
            events = [{"id": "synthetic", "model": MODEL, "choices": [{"delta": {"content": "observed"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2,
                    "prompt_tokens_details": {"video_tokens": 8, "audio_tokens": 2}, "total_tokens": 14}}]
            return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")

        service = WatchService(self.state, api_key="synthetic")
        service._client = lambda: httpx.Client(transport=httpx.MockTransport(handle))
        result = service._perceive("oss://synthetic/video.mp4", "动作？")
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["usage"]["prompt_tokens_details"]["audio_tokens"], 2)
        self.assertEqual(result["text"], "observed")

    def test_live_owner_lock_cannot_be_stolen_and_dead_owner_media_is_recovered(self):
        with patch("watch.engine.os.name", "posix"), patch("watch.engine.os.kill", side_effect=PermissionError):
            self.assertTrue(process_alive(12345))
        service = WatchService(self.state, api_key="synthetic")
        lock = service.root / "active.lock"
        lock.write_text(str(os.getpid()))
        os.utime(lock, (1, 1))
        with self.assertRaises(WatchError) as raised:
            service._acquire_process_lock()
        self.assertEqual(raised.exception.code, "WATCH_CONCURRENCY_LIMIT")
        dead = service.root / "tmp" / "watch-dead"
        dead.mkdir()
        (dead / "owner.pid").write_text("2147483647")
        (dead / "media.mp4").write_bytes(b"synthetic")
        live = service.root / "tmp" / "watch-live"
        live.mkdir()
        (live / "owner.pid").write_text(str(os.getpid()))
        service._cleanup_stale()
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_incomplete_stream_keeps_actual_usage_and_request_id(self):
        usage = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}
        events = [{"id": "synthetic-partial", "choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]},
            {"choices": [], "usage": usage}]
        service = WatchService(self.state, api_key="synthetic")
        service._client = lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events))))
        with self.assertRaises(WatchError) as raised:
            service._perceive("oss://synthetic/video.mp4", "")
        self.assertEqual(raised.exception.code, "DASHSCOPE_RESPONSE_INCOMPLETE")
        self.assertEqual(raised.exception.usage, usage)
        self.assertEqual(raised.exception.request_id, "synthetic-partial")

    def test_upload_is_streamed_to_official_region_without_api_key_header(self):
        calls = []
        policy = {"upload_host": "https://dashscope-file-synthetic.oss-cn-beijing.aliyuncs.com",
            "max_file_size_mb": 100, "upload_dir": "synthetic-private", "oss_access_key_id": "synthetic-oss-id",
            "signature": "synthetic-signature", "policy": "synthetic-policy",
            "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}

        def handle(request):
            calls.append(request)
            if request.method == "GET":
                self.assertEqual(request.url.params["model"], MODEL)
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-key")
                return httpx.Response(200, json={"data": policy})
            self.assertNotIn("authorization", request.headers)
            self.assertTrue(request.headers["content-type"].startswith("multipart/form-data"))
            self.assertIn(b"synthetic-test-file", request.content)
            return httpx.Response(200)

        service = WatchService(self.state, api_key="synthetic-key")
        service._client = lambda: httpx.Client(transport=httpx.MockTransport(handle))
        media = self.state / "synthetic.mp4"
        media.write_bytes(b"synthetic-test-file")
        result = service._upload(media)
        self.assertTrue(result.startswith("oss://synthetic-private/"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
