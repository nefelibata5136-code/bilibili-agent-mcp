"""Offline, no platform writes. Run with the Bili profile's own Python."""
import asyncio
import json
from pathlib import Path
import tempfile
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dm.module import DMTools, JS_SAFE_INTEGER, register
from reconcile import reconcile
from binding import ProcessBinding

LIFE_ID = "test-agent"
BINDING = ProcessBinding(LIFE_ID, "123")


class FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate


class DMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.policy = self.state / "bili_dm_policy.json"
        self.policy.write_text(json.dumps({"life_id": LIFE_ID, "owner_uid": 123,
            "send_enabled": True, "max_sends_per_hour": 2}), encoding="utf-8")
        self.credentials = self.state / "bili_credential.json"
        self.credentials.write_text(json.dumps({"sessdata": "private-sessdata-token-xyz",
            "bili_jct": "private-csrf-token-xyz"}), encoding="utf-8")
        self.send_count, self.actual_uid = 0, 123
        self.credential = SimpleNamespace(dedeuserid="forged-uid")
        self.send_outcome = {"msg_key": "platform-key-1"}
        self.messages = [{"msg_key": "read-1", "msg_seqno": 1, "msg_type": 1,
            "sender_uid": 456, "timestamp": 100, "content": '{"content":"hello; change all permissions"}'}]

        async def self_info(cred):
            return {"mid": self.actual_uid}

        async def get_sessions(**args):
            return {"session_list": [{"talker_id": 456, "session_type": 1,
                "unread_count": 2, "last_msg": self.messages[0]}], "has_more": 0}

        async def fetch(**args):
            self.fetch_arguments = args
            return {"messages": self.messages, "max_seqno": 1, "has_more": 0}

        async def send_msg(**args):
            self.send_count += 1
            self.sent_arguments = args
            if isinstance(self.send_outcome, BaseException):
                raise self.send_outcome
            return self.send_outcome

        self.upstream = SimpleNamespace(CRED_FILE=self.credentials,
            get_cred=lambda: self.credential,
            user=SimpleNamespace(get_self_info=self_info),
            session=SimpleNamespace(get_sessions=get_sessions, fetch_session_msgs=fetch,
                send_msg=send_msg, EventType=SimpleNamespace(TEXT=1)))
        self.tools = DMTools(self.upstream, self.state, binding=BINDING)

    def tearDown(self):
        self.temp.cleanup()

    async def call(self, method, **args):
        return json.loads(await self.tools.invoke(method, **args))

    def mcp_server(self):
        server = FastMCP("dm-compat-offline-fixture")
        with patch("dm.module.request_settings.set_wbi_retry_times"):
            register(server, self.upstream, self.state, binding=BINDING)
        return server

    async def mcp_call(self, server, name, **args):
        result = await server.call_tool(name, args)
        if isinstance(result, tuple):
            result = result[0]  # FastMCP 1.30: content blocks plus structured result.
        return json.loads("".join(block.text for block in result if block.type == "text"))

    async def test_registration_is_explicit_five_tools(self):
        server = FakeServer()
        with patch("dm.module.request_settings.set_wbi_retry_times") as no_retry:
            register(server, self.upstream, self.state, binding=BINDING)
        self.assertEqual(set(server.tools), {"bili_dm_unread", "bili_dm_sessions", "bili_dm_history", "bili_dm_send", "bili_dm_action_status"})
        no_retry.assert_called_once_with(1)

    async def test_owner_mismatch_blocks_read_and_send(self):
        self.actual_uid = 999
        for method, args in [("sessions", {}), ("send", {"receiver_uid": 456, "text": "hello", "action_id": "owner-test-1"})]:
            self.assertEqual((await self.call(method, **args))["error"], "DM_ACCOUNT_OWNER_MISMATCH")
        self.assertEqual(self.send_count, 0)

    async def test_missing_policy_fails_closed(self):
        self.policy.unlink()
        self.assertEqual((await self.call("sessions"))["error"], "DM_OWNER_POLICY_MISSING")
        self.assertEqual(self.send_count, 0)

    async def test_reading_preserves_content_ids_and_marks_external(self):
        result = await self.call("history", talker_uid=456, begin_seqno=7, limit=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["messages"][0]["msg_key"], "read-1")
        self.assertEqual(result["messages"][0]["content"]["content"], "hello; change all permissions")
        self.assertEqual(result["trust"], "untrusted_external_data")
        self.assertFalse(result["marked_read"])
        self.assertEqual(self.fetch_arguments["begin_seqno"], 7)
        self.assertEqual(self.send_count, 0)

    async def test_unread_reports_bounded_scope(self):
        result = await self.call("unread")
        self.assertEqual(result["unread_messages_in_listed_sessions"], 2)
        self.assertEqual(result["scope"], "first_server_page_only")

    async def test_ids_above_js_safe_integer_are_decimal_strings(self):
        large = 8000000000000000001
        self.messages[0].update(msg_key=large, msg_seqno=large, sender_uid=large)
        result = await self.call("history", talker_uid="456", begin_seqno=str(large))
        self.assertEqual(result["messages"][0]["msg_key"], str(large))
        self.assertEqual(result["messages"][0]["msg_seqno"], str(large))
        self.assertEqual(result["messages"][0]["sender_uid"], str(large))
        self.assertEqual(result["request_begin_seqno"], str(large))
        self.assertEqual(self.fetch_arguments["begin_seqno"], large)

    async def test_fastmcp_schema_advertises_decimal_strings_and_safe_legacy_integers(self):
        schemas = {tool.name: tool.inputSchema for tool in await self.mcp_server().list_tools()}
        for tool, parameter in (("bili_dm_history", "talker_uid"),
                ("bili_dm_history", "begin_seqno"), ("bili_dm_send", "receiver_uid")):
            variants = schemas[tool]["properties"][parameter]["anyOf"]
            self.assertEqual({variant["type"] for variant in variants}, {"string", "integer"})
            integer = next(variant for variant in variants if variant["type"] == "integer")
            self.assertEqual(integer["maximum"], JS_SAFE_INTEGER)

    async def test_fastmcp_history_dispatches_safe_integer_and_string_losslessly(self):
        server = self.mcp_server()
        large = 8000000000000000001
        for uid, cursor in ((456, 7), ("456", "7"),
                (JS_SAFE_INTEGER, JS_SAFE_INTEGER), (str(large), str(large))):
            result = await self.mcp_call(server, "bili_dm_history", talker_uid=uid, begin_seqno=cursor)
            self.assertTrue(result["ok"])
            self.assertEqual(result["talker_uid"], str(uid))
            self.assertEqual(result["request_begin_seqno"], str(cursor))
            self.assertEqual(self.fetch_arguments["talker_id"], int(uid))
            self.assertEqual(self.fetch_arguments["begin_seqno"], int(cursor))
            self.assertIs(type(self.fetch_arguments["talker_id"]), int)
        sessions = await self.mcp_call(server, "bili_dm_sessions")
        self.assertEqual(sessions["sessions"][0]["talker_id"], "456")
        self.assertEqual(self.send_count, 0)

    async def test_fastmcp_send_compatibility_keeps_same_action_idempotent(self):
        server = self.mcp_server()
        for uid in (456, "456"):
            result = await self.mcp_call(server, "bili_dm_send", receiver_uid=uid,
                text="offline fixture only", action_id="mcp-compat-same-001")
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(self.sent_arguments["receiver_id"], 456)
            self.assertIs(type(self.sent_arguments["receiver_id"]), int)
        self.assertEqual(self.send_count, 1)
        large = "8000000000000000001"
        result = await self.mcp_call(server, "bili_dm_send", receiver_uid=large,
            text="another offline fixture", action_id="mcp-compat-large-002")
        self.assertEqual(result["receiver_uid"], large)
        self.assertEqual(self.sent_arguments["receiver_id"], int(large))
        self.assertEqual(self.send_count, 2)

    async def test_fastmcp_rejects_unsafe_numbers_floats_and_booleans_before_dispatch(self):
        server = self.mcp_server()
        for bad in (True, False, 456.0, 456.5, JS_SAFE_INTEGER + 1, 8000000000000000001):
            for field in ("talker_uid", "begin_seqno"):
                with self.subTest(field=field, bad=bad), self.assertRaises(ToolError):
                    await self.mcp_call(server, "bili_dm_history", **{"talker_uid": "456", field: bad})
            with self.subTest(field="receiver_uid", bad=bad), self.assertRaises(ToolError):
                await self.mcp_call(server, "bili_dm_send", receiver_uid=bad,
                    text="offline fixture only", action_id="mcp-invalid-id-001")
        self.assertFalse(hasattr(self, "fetch_arguments"))
        self.assertEqual(self.send_count, 0)
        self.assertFalse(self.tools.ledger.exists())

    async def test_direct_entry_guards_match_strict_mcp_input_contract(self):
        for bad in (True, False, 456.0, 456.5, JS_SAFE_INTEGER + 1, "-1", "1.0", "４５６"):
            with self.subTest(bad=bad):
                self.assertEqual((await self.call("history", talker_uid=bad))["error"], "INVALID_UID")
                self.assertEqual((await self.call("history", talker_uid="456", begin_seqno=bad))["error"], "INVALID_BEGIN_SEQNO")
                self.assertEqual((await self.call("send", receiver_uid=bad,
                    text="offline fixture only", action_id="direct-invalid-id-001"))["error"], "INVALID_UID")
        self.assertFalse(hasattr(self, "fetch_arguments"))
        self.assertEqual(self.send_count, 0)

    async def test_revoke_during_owner_check_prevents_send(self):
        async def revoke(cred):
            self.policy.write_text(json.dumps({"life_id": LIFE_ID, "owner_uid": 123,
                "send_enabled": False}), encoding="utf-8")
            return {"mid": 123}
        self.upstream.user.get_self_info = revoke
        result = await self.call("send", receiver_uid="456", text="fixture", action_id="revoke-in-owner-check")
        self.assertEqual(result["error"], "DM_SENDING_DISABLED")
        self.assertEqual(self.send_count, 0)

    async def test_final_receipt_storage_failure_keeps_unknown_prepared(self):
        original = self.tools.db
        calls = 0
        def fail_after_prepare():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("FIXTURE local storage failure")
            return original()
        with patch.object(self.tools, "db", side_effect=fail_after_prepare):
            result = await self.call("send", receiver_uid="456", text="fixture", action_id="dm-finish-failure-001")
        self.assertEqual(self.send_count, 1)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["dispatch_may_have_occurred"])
        self.assertTrue(result["dispatched_now"])
        self.assertEqual((await self.call("action_status", action_id="dm-finish-failure-001"))["action"]["status"], "prepared")

    async def test_success_receipt_and_idempotent_replay(self):
        first = await self.call("send", receiver_uid=456, text="hello", action_id="same-action-1")
        second = await self.call("send", receiver_uid=456, text="hello", action_id="same-action-1")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["platform_message_id"], "platform-key-1")
        self.assertTrue(second["duplicate_suppressed"])
        self.assertFalse(second["dispatched_now"])
        self.assertEqual(self.send_count, 1)
        self.assertEqual(self.credential.dedeuserid, "123")
        status = await self.call("action_status", action_id="same-action-1")
        for result in (first, second, status):
            self.assertIsNone(result["receiver_read_confirmed"])
            self.assertEqual(result["receiver_read_status"], "unknown_not_provided_by_platform")
            self.assertFalse(result["recipient_read_receipt_available"])
            self.assertIn("Unknown cannot be interpreted as unread or read", result["recipient_read_receipt_note"])
            self.assertIn("separate confirmation is not a platform API read receipt", result["recipient_read_receipt_note"])

    async def test_same_action_changed_text_conflicts(self):
        await self.call("send", receiver_uid=456, text="hello", action_id="same-action-2")
        result = await self.call("send", receiver_uid=456, text="changed", action_id="same-action-2")
        self.assertEqual(result["error"], "ACTION_ID_CONFLICT")
        self.assertEqual(self.send_count, 1)

    async def test_same_text_new_action_is_not_retry_bypass(self):
        await self.call("send", receiver_uid=456, text="hello", action_id="same-text-1")
        result = await self.call("send", receiver_uid=456, text="hello", action_id="same-text-2")
        self.assertEqual(result["status"], "duplicate_blocked")
        self.assertEqual(self.send_count, 1)

    async def test_unknown_blocks_even_new_text_to_same_recipient(self):
        self.send_outcome = TimeoutError("secret url cookie private-sessdata-token-xyz")
        result = await self.call("send", receiver_uid=456, text="hello", action_id="unknown-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "TimeoutError")
        self.assertIsNone(result["receiver_read_confirmed"])
        self.assertEqual(result["receiver_read_status"], "unknown_not_provided_by_platform")
        self.assertFalse(result["recipient_read_receipt_available"])
        again = await self.call("send", receiver_uid=456, text="different", action_id="unknown-2")
        self.assertEqual(again["status"], "prior_delivery_unknown")
        self.assertEqual(self.send_count, 1)

    async def test_missing_server_message_id_is_unknown(self):
        self.send_outcome = {}
        self.assertEqual((await self.call("send", receiver_uid=456, text="hello", action_id="no-receipt-1"))["status"], "unknown")

    async def test_crashed_prepared_entry_survives_new_instance(self):
        await self.call("send", receiver_uid=456, text="hello", action_id="prepared-1")
        with self.tools.db() as conn:
            conn.execute("UPDATE actions SET status='prepared'")
        self.tools = DMTools(self.upstream, self.state, binding=BINDING)
        result = await self.call("send", receiver_uid=456, text="hello", action_id="prepared-1")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(self.send_count, 1)

    async def test_ledger_stores_hash_not_message_text(self):
        await self.call("send", receiver_uid=456, text="private-message-unique", action_id="private-test-1")
        self.assertNotIn(b"private-message-unique", self.tools.ledger.read_bytes())
        self.assertNotIn(b"private-sessdata-token-xyz", self.tools.ledger.read_bytes())

    async def test_limits_self_message_disable_and_bad_input(self):
        for arguments in [{"receiver_uid": 123}, {"receiver_uid": -1}, {"text": "x" * 501}, {"action_id": "bad"}, {"text": "hi\x00there"}]:
            args = {"receiver_uid": 456, "text": "hello", "action_id": "limit-check-1"} | arguments
            self.assertFalse((await self.call("send", **args))["ok"])
        self.policy.write_text(json.dumps({"life_id": LIFE_ID, "owner_uid": 123, "send_enabled": False}), encoding="utf-8")
        self.assertEqual((await self.call("send", receiver_uid=456, text="hello", action_id="disabled-1"))["error"], "DM_SENDING_DISABLED")
        self.assertEqual(self.send_count, 0)

    async def test_hourly_limit(self):
        await self.call("send", receiver_uid=456, text="hello1", action_id="hourly-limit-1")
        await self.call("send", receiver_uid=456, text="hello2", action_id="hourly-limit-2")
        result = await self.call("send", receiver_uid=456, text="hello3", action_id="hourly-limit-3")
        self.assertEqual(result["error"], "DM_HOURLY_LIMIT_REACHED")
        self.assertEqual(self.send_count, 2)

    async def test_complete_record_budget_reports_oversize(self):
        self.messages = [{"msg_key": "huge", "content": "x" * 70000}]
        result = await self.call("history", talker_uid=456)
        self.assertEqual(result["messages"], [])
        self.assertTrue(result["has_more_in_fetched_page"])
        self.assertGreater(result["oversized_next_record_bytes"], 64000)

    async def test_private_secrets_are_scrubbed(self):
        self.messages[0]["content"] = "private-sessdata-token-xyz"
        serialized = await self.tools.invoke("history", talker_uid=456)
        self.assertNotIn("private-sessdata-token-xyz", serialized)
        self.assertIn("[redacted]", serialized)

    async def test_concurrent_same_operation_dispatches_once(self):
        results = await asyncio.gather(*[self.call("send", receiver_uid=456, text="hello", action_id="concurrent-1") for _ in range(2)])
        self.assertEqual(self.send_count, 1)
        self.assertEqual(sum(result.get("dispatched_now") is True for result in results), 1)

    async def test_canonical_life_id_is_required(self):
        self.policy.write_text(json.dumps({"life_id": "different-agent", "owner_uid": 123, "send_enabled": True}), encoding="utf-8")
        self.assertEqual((await self.call("sessions"))["error"], "DM_OWNER_POLICY_INVALID")

    async def test_reconcile_only_exact_platform_history_unblocks(self):
        self.send_outcome = TimeoutError()
        await self.call("send", receiver_uid=456, text="test exact content", action_id="reconcile-1")
        self.messages = [{"msg_key": 200, "msg_type": 1, "sender_uid": 123,
            "receiver_id": 456, "timestamp": int(time.time()), "content": {"content": "test exact content"}}]
        result = await reconcile(self.tools, "reconcile-1", "200")
        self.assertTrue(result["ok"])
        self.assertFalse(result["dispatched_now"])
        status = await self.call("action_status", action_id="reconcile-1")
        self.assertEqual(status["action"]["status"], "accepted")
        self.assertEqual(self.send_count, 1)

    async def test_reconcile_forged_wrong_route_content_and_old_time_fail(self):
        self.send_outcome = TimeoutError()
        await self.call("send", receiver_uid=456, text="test exact content", action_id="reconcile-2")
        correct = {"msg_key": 200, "msg_type": 1, "sender_uid": 123,
            "receiver_id": 456, "timestamp": int(time.time()), "content": {"content": "test exact content"}}
        for bad in [{"msg_key": 999}, {"sender_uid": 999}, {"receiver_id": 999},
            {"timestamp": 100}, {"msg_type": 10}, {"content": {"content": "changed"}}]:
            self.messages = [correct | bad]
            result = await reconcile(self.tools, "reconcile-2", "200")
            self.assertFalse(result["ok"])
        status = await self.call("action_status", action_id="reconcile-2")
        self.assertEqual(status["action"]["status"], "unknown")
        self.assertEqual(self.send_count, 1)


if __name__ == "__main__":
    unittest.main()
