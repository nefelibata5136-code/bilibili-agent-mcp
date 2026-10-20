"""Login fallback owner guards, synthetic credentials and no external calls."""
import json
import asyncio
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from binding import ProcessBinding
from watch.engine import WatchService, WatchError
from watch.identity import verify_owner


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="watch-owner-test-")
        self.state = Path(self.temporary.name)
        self.credentials = {"sessdata": "synthetic-session", "bili_jct": "synthetic-csrf", "dedeuserid": "123"}
        (self.state / "bili_credential.json").write_text(json.dumps(self.credentials), encoding="utf-8")
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    async def verifier(self, credentials):
        self.calls.append("identity")
        return {"mid": "123"}

    def test_missing_owner_never_reads_or_adopts_local_cookie(self):
        service = WatchService(self.state, binding=ProcessBinding(), identity_verifier=self.verifier)
        with patch("watch.engine.load_json", side_effect=AssertionError("must not read credentials")):
            with self.assertRaises(WatchError) as raised:
                service._login_credentials()
        self.assertEqual(raised.exception.code, "BILI_WATCH_OWNER_NOT_CONFIGURED")
        self.assertEqual(self.calls, [])

    def test_claimed_uid_mismatch_blocks_before_any_identity_request(self):
        service = WatchService(self.state, binding=ProcessBinding(owner_uid="456"), identity_verifier=self.verifier)
        with self.assertRaises(WatchError) as raised:
            service._login_credentials()
        self.assertEqual(raised.exception.code, "BILI_WATCH_CREDENTIAL_OWNER_MISMATCH")
        self.assertEqual(self.calls, [])

    def test_real_uid_mismatch_and_failed_verification_do_not_return_cookie(self):
        async def wrong(credentials):
            return {"mid": "456"}
        async def failure(credentials):
            raise RuntimeError("synthetic-session must never be projected")
        binding = ProcessBinding(owner_uid="123")
        self.assertEqual(verify_owner(self.credentials, binding, wrong), "BILI_WATCH_AUTHENTICATED_OWNER_MISMATCH")
        self.assertEqual(verify_owner(self.credentials, binding, failure), "BILI_WATCH_OWNER_VERIFICATION_FAILED")
        # Exercise the default SDK path without importing a real network client.
        closed, received = [], []
        async def close():
            closed.append(True)
        network = SimpleNamespace(session_pool={"mock": {"unrelated-loop": "untouched"}}, lazy_settings={"mock": {}})
        async def self_info(credential):
            loop = asyncio.get_running_loop()
            network.session_pool["mock"][loop] = SimpleNamespace(close=close)
            network.lazy_settings["mock"][loop] = {}
            return {"mid": "123"}
        def credential(**arguments):
            received.append(arguments)
            return object()
        sdk, utilities = ModuleType("bilibili_api"), ModuleType("bilibili_api.utils")
        sdk.Credential, sdk.user = credential, SimpleNamespace(get_self_info=self_info)
        utilities.network = network
        with patch.dict(sys.modules, {"bilibili_api": sdk, "bilibili_api.utils": utilities}):
            self.assertIsNone(verify_owner(self.credentials, binding))
        self.assertEqual(received[0]["dedeuserid"], "123")
        self.assertEqual(closed, [True])
        self.assertEqual(network.session_pool["mock"], {"unrelated-loop": "untouched"})

    def test_matching_fixed_owner_verifies_before_creating_cookiejar(self):
        service = WatchService(self.state, binding=ProcessBinding(owner_uid="123"), identity_verifier=self.verifier)
        events = self.calls
        class Downloader:
            def __init__(self, options):
                self.cookiejar = SimpleNamespace(set_cookie=lambda cookie: events.append("cookie:" + cookie.name))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, *args, **kwargs):
                events.append("anonymous_or_authenticated_extract")
                raise RuntimeError("login required")
        with patch("yt_dlp.YoutubeDL", Downloader), patch("watch.engine.shutil.which", return_value="synthetic-tool"):
            with self.assertRaises(WatchError):
                service._download("BV0000000000", 1, self.state, time.monotonic())
        self.assertEqual(events[0], "anonymous_or_authenticated_extract")
        self.assertEqual(events[1], "identity")
        self.assertTrue(events[2].startswith("cookie:"))
        self.assertIn("cookie:DedeUserID", events)

    def test_failed_owner_guard_never_adds_cookies_to_downloader(self):
        events = []
        class Downloader:
            def __init__(self, options):
                self.cookiejar = SimpleNamespace(set_cookie=lambda cookie: events.append("cookie"))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, *args, **kwargs):
                events.append("anonymous_extract")
                raise RuntimeError("login required")
        for binding, expected in ((ProcessBinding(), "BILI_WATCH_OWNER_NOT_CONFIGURED"),
                (ProcessBinding(owner_uid="456"), "BILI_WATCH_CREDENTIAL_OWNER_MISMATCH")):
            service = WatchService(self.state, binding=binding, identity_verifier=self.verifier)
            with patch("yt_dlp.YoutubeDL", Downloader), patch("watch.engine.shutil.which", return_value="synthetic-tool"):
                with self.assertRaises(WatchError) as raised:
                    service._download("BV0000000000", 1, self.state, time.monotonic())
            self.assertEqual(raised.exception.code, expected)
        self.assertNotIn("cookie", events)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
