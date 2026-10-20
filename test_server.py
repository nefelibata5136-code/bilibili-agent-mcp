"""Verify public-read account isolation through actual FastMCP dispatch."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import readonly_server
from binding import ProcessBinding


class PublicReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_owner_never_loads_saved_account(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(readonly_server, "PROCESS_BINDING", ProcessBinding()):
            server, upstream = readonly_server.create_server(Path(__file__).parent, Path(temp))
            def forbidden():
                self.fail("Anonymous reads must not load an account")
            upstream.load_credential = forbidden
            async def subtitle(**kwargs):
                return json.dumps({"anonymous": not bool(upstream.get_cred().sessdata)})
            upstream.bili_subtitle = subtitle
            result = await server.call_tool("bili_subtitle", {"bvid": "BV0000000000"})
            self.assertIn('"anonymous": true', str(result))
            result = await server.call_tool("bili_whoami", {})
            self.assertIn("BILI_OWNER_NOT_CONFIGURED", str(result))

    async def test_real_owner_mismatch_blocks_public_tool_before_content_read(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(readonly_server, "PROCESS_BINDING", ProcessBinding("fixture", "123")):
            server, upstream = readonly_server.create_server(Path(__file__).parent, Path(temp))
            upstream.load_credential = lambda: SimpleNamespace(sessdata="synthetic-cookie")
            async def identity(credential):
                return {"mid": "456"}
            async def forbidden(**kwargs):
                self.fail("Wrong account must not read content")
            with patch.object(upstream.user, "get_self_info", identity):
                upstream.bili_subtitle = forbidden
                result = await server.call_tool("bili_subtitle", {"bvid": "BV0000000000"})
                self.assertIn("BILI_ACCOUNT_OWNER_MISMATCH", str(result))

    async def test_concurrent_public_reads_keep_credentials_in_their_own_context(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(readonly_server, "PROCESS_BINDING", ProcessBinding("fixture", "123")):
            server, upstream = readonly_server.create_server(Path(__file__).parent, Path(temp))
            credentials = iter([SimpleNamespace(sessdata="synthetic-A"), SimpleNamespace(sessdata="synthetic-B")])
            upstream.load_credential = lambda: next(credentials)
            async def identity(credential):
                await asyncio.sleep(0)
                return {"mid": "123"}
            async def subtitle(**kwargs):
                before = upstream.get_cred()
                await asyncio.sleep(.01)
                self.assertIs(before, upstream.get_cred())
                return json.dumps({"label": before.sessdata.removeprefix("synthetic-")})
            with patch.object(upstream.user, "get_self_info", identity):
                upstream.bili_subtitle = subtitle
                first, second = await asyncio.gather(*[
                    server.call_tool("bili_subtitle", {"bvid": "BV0000000000"}) for _ in range(2)])
            self.assertIn('"label": "A"', str(first))
            self.assertIn('"label": "B"', str(second))


if __name__ == "__main__":
    unittest.main()
