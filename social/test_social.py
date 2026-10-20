"""Offline public-comment/navigation acceptance. Never writes Bilibili."""
import asyncio
import inspect
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from social.module import TOOLS, SocialTools, register
from social.actions import SocialError
from binding import ProcessBinding

LIFE_ID, OWNER_UID = "test-agent", "10001"
BINDING = ProcessBinding(LIFE_ID, OWNER_UID)

BV = "BV1xx411c7mD"
ROOT = "17000000000000000001"
CHILD = "17000000000000000002"
UID = "18000000000000000001"


class Server:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def bind(fn):
            self.tools[fn.__name__] = fn
            return fn
        return bind


class SocialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.credfile = self.path / "bili_credential.json"
        self.credfile.write_text(json.dumps({"sessdata": "SECRET_SESSION_VALUE"}), encoding="utf-8")
        self.calls, self.send_count, self.owner_count = [], 0, 0
        self.actual_uid = OWNER_UID
        self.send_result = {"rpid": 17000000000000000003}
        self.comment = {"rpid": int(ROOT), "root": 0, "parent": 0, "member": {"mid": int(UID), "uname": "public"},
            "content": {"message": "Public comment"}, "ctime": 100, "like": 3, "rcount": 1,
            "replies": [{"rpid": int(CHILD), "root": int(ROOT), "parent": int(ROOT),
                "member": {"mid": 123, "uname": "child"}, "content": {"message": "Nested"}, "ctime": 101, "like": 2, "rcount": 0}]}
        self.notification_id = "17000000000000000004"
        self.dm_key = "17000000000000000005"
        self.videos = [{"bvid": BV, "aid": 123, "mid": int(UID)}]
        test = self
        class Video:
            def __init__(self, bvid, credential=None):
                self.credential = credential
            def get_aid(self):
                return 123
        class Comment:
            def __init__(self, **kwargs):
                test.calls.append(("Comment", kwargs))
            async def get_sub_comments(self, **kwargs):
                test.calls.append(("sub", kwargs))
                return {"root": test.comment, "replies": test.comment["replies"], "page": {"num": kwargs["page_index"], "size": kwargs["page_size"], "count": 1}}
        class User:
            def __init__(self, **kwargs):
                test.calls.append(("User", kwargs))
            async def get_user_info(self):
                return {"mid": int(UID), "name": "public", "sign": "A public self-description"}
            async def get_videos(self, **kwargs):
                test.calls.append(("videos", kwargs))
                return {"list": {"vlist": test.videos}, "page": {"pn": kwargs["pn"]}}
            async def get_dynamics_new(self, **kwargs):
                test.calls.append(("dynamics", kwargs))
                return {"items": [{"id_str": ROOT, "modules": {"module_author": {"mid": int(UID)}}}], "offset": CHILD, "has_more": 1}
        async def owner(cred):
            self.owner_count += 1
            return {"mid": int(self.actual_uid)}
        async def get_comments(**kwargs):
            self.calls.append(("comments", kwargs))
            return {"replies": [self.comment], "page": {"num": kwargs["page_index"], "count": 21}}
        async def lazy(**kwargs):
            self.calls.append(("lazy", kwargs))
            return {"replies": [self.comment], "cursor": {"pagination_reply": {"next_offset": "NATIVE-NEXT"}}}
        async def send(**kwargs):
            self.send_count += 1
            self.calls.append(("send", kwargs))
            if isinstance(self.send_result, BaseException):
                raise self.send_result
            return self.send_result
        async def replies(**kwargs):
            self.calls.append(("notifications_reply", kwargs))
            return {"items": [{"id": int(self.notification_id), "reply_time": 100,
                "item": {"source_id": int(CHILD), "root_id": int(ROOT), "subject_id": 123, "uri": "https://www.bilibili.com/video/" + BV}}],
                "cursor": {"id": int(self.notification_id), "time": 100, "is_end": False}}
        async def at(**kwargs):
            self.calls.append(("notifications_at", kwargs))
            return {"items": [], "cursor": {"is_end": True}}
        async def sessions(**kwargs):
            return {"session_list": [{"talker_id": int(UID), "unread_count": 1,
                "last_msg": {"msg_key": int(self.dm_key), "timestamp": 100, "content": "PRIVATE_DM_BODY_MUST_NOT_SAVE"}}]}
        self.upstream = N(CRED_FILE=self.credfile, Credential=lambda: N(anonymous=True), get_cred=lambda: N(logged_in=True),
            video=N(Video=Video), comment=N(Comment=Comment, get_comments=get_comments,
                get_comments_lazy=lazy, send_comment=send, OrderType=N(LIKE=2, TIME=0), CommentResourceType=N(VIDEO=1)),
            user=N(User=User, get_self_info=owner, VideoOrder=N(PUBDATE="pubdate", VIEW="click", FAVORITE="stow")),
            session=N(get_replies=replies, get_at=at, get_sessions=sessions))
        self.tools = SocialTools(self.upstream, self.path, binding=BINDING)

    def tearDown(self):
        self.temp.cleanup()

    def policy(self, **switches):
        value = {"life_id": LIFE_ID, "owner_uid": OWNER_UID,
            "permissions": {kind: False for kind in ("comment", "reply", "like", "favorite", "follow")}, "max_actions_per_hour": 3}
        value["permissions"].update(switches)
        (self.path / "bili_social_policy.json").write_text(json.dumps(value), encoding="utf-8")

    async def call(self, method, **args):
        return json.loads(await self.tools.invoke(method, **args))

    async def test_explicit_registration_and_no_force_parameter(self):
        server = Server()
        register(server, self.upstream, self.path, binding=BINDING)
        self.assertEqual(tuple(server.tools), TOOLS)
        self.assertNotIn("bili_reply", server.tools)
        for tool in server.tools.values():
            self.assertNotIn("force", inspect.signature(tool).parameters)

    async def test_default_disabled_causes_zero_network_or_identity_requests(self):
        for arguments in [{}, {"root": ROOT, "action_kind": "reply"}]:
            result = await self.call("send", bvid=BV, text="test", action_id="disabled-1", **arguments)
            self.assertFalse(result["ok"])
            self.assertIn("SOCIAL_PERMISSION_DISABLED", result["error"])
        self.assertEqual(self.owner_count, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.send_count, 0)

    async def test_comment_reply_permissions_are_separate_and_no_empty_root_bypass(self):
        self.policy(comment=True)
        result = await self.call("send", bvid=BV, text="test", action_id="reply-off-1", root=ROOT, action_kind="reply")
        self.assertEqual(result["error"], "SOCIAL_PERMISSION_DISABLED_REPLY")
        result = await self.call("send", bvid=BV, text="test", action_id="reply-off-2", root="", action_kind="reply")
        self.assertEqual(result["error"], "ACTION_KIND_TARGET_MISMATCH")
        self.assertEqual(self.send_count, 0)

    async def test_large_ids_root_and_nested_fields_are_lossless_strings(self):
        result = await self.call("comments", bvid=BV)
        record = result["comments"][0]
        self.assertEqual(record["rpid"], ROOT)
        self.assertEqual(record["uid"], UID)
        self.assertEqual(record["root"], "0")
        self.assertEqual(record["replies"][0]["rpid"], CHILD)
        self.assertEqual(record["replies"][0]["parent"], ROOT)
        for key in ("text", "occurred_at", "like_count", "reply_count", "bvid", "nickname"):
            self.assertIn(key, record)

    async def test_hot_time_page_and_native_cursor_passthrough(self):
        await self.call("comments", bvid=BV, sort="time", page=3)
        args = self.calls[-1][1]
        self.assertEqual(args["order"], 0)
        self.assertEqual(args["page_index"], 3)
        result = await self.call("comments", bvid=BV, cursor="native-input")
        self.assertEqual(self.calls[-1][0], "lazy")
        self.assertEqual(self.calls[-1][1]["offset"], "native-input")
        self.assertEqual(result["next_cursor"], "NATIVE-NEXT")

    async def test_comments_not_limited_to_twenty_total_and_reply_page(self):
        await self.call("comments", bvid=BV, page=2)
        self.assertEqual(self.calls[-1][1]["page_index"], 2)
        result = await self.call("replies", bvid=BV, root=ROOT, page=3, num=20)
        self.assertEqual(self.calls[-1][1], {"page_index": 3, "page_size": 20})
        self.assertEqual(result["root_comment"]["rpid"], ROOT)

    async def test_context_locates_nested_rpid_through_preserved_root_reference(self):
        await self.call("comments", bvid=BV)
        result = await self.call("context", bvid=BV, rpid=CHILD)
        self.assertTrue(result["target_found_in_this_page"])
        self.assertEqual(result["root"], ROOT)
        self.assertEqual(result["target_comment"]["rpid"], CHILD)

    async def test_numeric_and_float_id_inputs_rejected_before_send(self):
        for uid in [int(UID), float(UID), "abc"]:
            result = await self.call("user_info", uid=uid)
            self.assertEqual(result["error"], "ID_MUST_BE_DECIMAL_STRING")
        self.assertEqual(self.calls, [])

    async def test_public_profile_uploads_and_dynamics_navigation(self):
        info = await self.call("user_info", uid=UID)
        self.assertEqual(info["user"]["mid"], UID)
        self.assertIn("unverified", info["identity_caution"])
        videos = await self.call("user_videos", uid=UID, page=2, num=5)
        self.assertEqual(videos["videos"][0]["bvid"], BV)
        self.assertEqual(self.calls[-1][1]["pn"], 2)
        dyn = await self.call("user_dynamics", uid=UID, cursor=ROOT)
        self.assertEqual(self.calls[-1][1]["offset"], ROOT)
        self.assertEqual(dyn["next_cursor"], CHILD)

    async def test_owner_switch_blocks_enabled_public_send(self):
        self.policy(comment=True)
        self.actual_uid = "999"
        result = await self.call("send", bvid=BV, text="test", action_id="owner-switch-1")
        self.assertEqual(result["error"], "SOCIAL_ACCOUNT_OWNER_MISMATCH")
        self.assertEqual(self.send_count, 0)

    async def test_top_comment_and_reply_sdk_root_parent_contract(self):
        self.policy(comment=True, reply=True)
        await self.call("send", bvid=BV, text="top", action_id="contract-top-1")
        self.assertIsNone(self.calls[-1][1]["root"])
        self.assertIsNone(self.calls[-1][1]["parent"])
        await self.call("comments", bvid=BV)
        await self.call("send", bvid=BV, text="root reply", action_id="contract-root-1", root=ROOT, action_kind="reply")
        self.assertEqual(self.calls[-1][1]["root"], int(ROOT))
        self.assertIsNone(self.calls[-1][1]["parent"])
        await self.call("send", bvid=BV, text="child reply", action_id="contract-child-1", root=ROOT, parent=CHILD, action_kind="reply")
        self.assertEqual(self.calls[-1][1]["parent"], int(CHILD))

    async def test_mock_loop_locate_reply_receipt_status_notification(self):
        self.policy(reply=True)
        context = await self.call("context", bvid=BV, rpid=CHILD, root=ROOT)
        self.assertTrue(context["target_found_in_this_page"])
        send = await self.call("send", bvid=BV, root=ROOT, parent=CHILD, text="offline reply", action_id="mock-closed-loop-1", action_kind="reply")
        self.assertEqual(send["status"], "accepted")
        self.assertEqual(send["result"]["public_visibility"], "unknown_not_checked")
        status = await self.call("action_status", action_id="mock-closed-loop-1")
        self.assertEqual(status["action"]["metadata"]["rpid"], "17000000000000000003")
        notif = await self.call("notifications", kind="reply")
        self.assertEqual(notif["notifications"][0]["item"]["root_id"], ROOT)
        self.assertEqual(self.send_count, 1)

    async def test_idempotent_replay_and_same_id_content_conflict(self):
        self.policy(comment=True)
        await self.call("send", bvid=BV, text="same", action_id="replay-1")
        replay = await self.call("send", bvid=BV, text="same", action_id="replay-1")
        self.assertTrue(replay["duplicate_suppressed"])
        conflict = await self.call("send", bvid=BV, text="changed", action_id="replay-1")
        self.assertEqual(conflict["error"], "ACTION_ID_CONFLICT")
        self.assertEqual(self.send_count, 1)

    async def test_duplicate_text_new_action_and_unknown_new_text_are_blocked(self):
        self.policy(comment=True)
        self.send_result = TimeoutError("SECRET_SESSION_VALUE")
        first = await self.call("send", bvid=BV, text="same", action_id="unknown-1")
        self.assertEqual(first["status"], "unknown")
        for text, action in [("same", "unknown-2"), ("changed", "unknown-3")]:
            result = await self.call("send", bvid=BV, text=text, action_id=action)
            self.assertFalse(result["dispatched_now"])
        self.assertEqual(self.send_count, 1)

    async def test_review_acceptance_and_visibility_are_separate(self):
        self.policy(comment=True)
        self.send_result["need_review"] = True
        result = await self.call("send", bvid=BV, text="test", action_id="review-1")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["result"]["moderation_status"], "review")
        self.assertEqual(result["result"]["public_visibility"], "unknown_not_checked")
        status = await self.call("action_status", action_id="review-1", check_visibility=True)
        self.assertEqual(status["action"]["metadata"]["public_visibility"], "unknown_not_found_in_bounded_page")

    async def test_anonymous_visibility_requires_exact_rpid_observation(self):
        self.policy(comment=True)
        self.send_result = {"rpid": int(ROOT)}
        await self.call("send", bvid=BV, text="test", action_id="visible-1")
        status = await self.call("action_status", action_id="visible-1", check_visibility=True)
        self.assertEqual(status["action"]["metadata"]["public_visibility"], "observed_on_anonymous_surface")
        self.assertTrue(self.calls[-1][1]["credential"].anonymous)

    async def test_notifications_native_id_time_cursor_preserves_precision(self):
        result = await self.call("notifications", kind="reply", last_id=ROOT, time_cursor="100")
        self.assertEqual(self.calls[-1][1]["last_reply_id"], int(ROOT))
        self.assertEqual(self.calls[-1][1]["reply_time"], 100)
        self.assertEqual(result["next_cursor"]["id"], self.notification_id)
        await self.call("notifications", kind="at", last_id=CHILD, time_cursor="101")
        self.assertEqual(self.calls[-1][1]["last_id"], int(CHILD))

    async def test_events_dedupe_explicit_ack_and_no_private_body(self):
        first = await self.call("social_events")
        second = await self.call("social_events")
        self.assertEqual([x["event_id"] for x in first["events"]], [x["event_id"] for x in second["events"]])
        self.assertNotIn("PRIVATE_DM_BODY_MUST_NOT_SAVE", json.dumps(first))
        event = first["events"][0]["event_id"]
        acknowledged = await self.call("social_events", ack_event_ids=[event], recent_event_id="native-event-reference")
        self.assertNotIn(event, [x["event_id"] for x in acknowledged["events"]])
        self.assertFalse(acknowledged["recent_events_write_performed"])
        self.assertNotIn(b"PRIVATE_DM_BODY_MUST_NOT_SAVE", self.tools.store.path.read_bytes())

    async def test_snapshot_cursor_has_fixed_membership_and_no_network(self):
        first = await self.call("social_events", num=1)
        self.assertIsNotNone(first["next_cursor"])
        self.notification_id = "17000000000000000006"
        await self.call("social_events")
        before = len(self.calls)
        following = await self.call("social_events", num=1, cursor=first["next_cursor"])
        self.assertFalse(following["network_reads_performed"])
        self.assertEqual(len(self.calls), before)
        self.assertEqual(following["snapshot_event_count"], 2)

    async def test_unknown_ack_cannot_create_fake_processed_event(self):
        result = await self.call("social_events", ack_event_ids=["reply:never-seen"])
        self.assertEqual(result["error"], "ACK_EVENT_NOT_PREVIOUSLY_OBSERVED")

    async def test_hourly_cap_and_bad_policy(self):
        self.policy(comment=True)
        for n in range(3):
            await self.call("send", bvid=BV, text=str(n), action_id=f"hourly-{n}")
        result = await self.call("send", bvid=BV, text="fourth", action_id="hourly-4")
        self.assertEqual(result["error"], "SOCIAL_HOURLY_LIMIT_REACHED")
        value = json.loads((self.path / "bili_social_policy.json").read_text())
        value["life_id"] = "someone-else"
        (self.path / "bili_social_policy.json").write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual((await self.call("send", bvid=BV, text="test", action_id="bad-life-1"))["error"], "SOCIAL_OWNER_POLICY_INVALID")

    async def test_account_state_inverse_then_forward_and_repeat_suppression(self):
        for kind in ("like", "favorite", "follow"):
            store = self.tools.store
            target = kind + ":fixture"
            for index, enabled in enumerate((True, False, True)):
                action_id = f"{kind}-transition-{index}"
                dispatch, _ = store.prepare(action_id, OWNER_UID, kind, target,
                    {"enabled": enabled}, {"enabled": enabled}, hourly_limit=100)
                self.assertTrue(dispatch)
                store.finish(action_id, "accepted", {"enabled": enabled})
            dispatch, _ = store.prepare(f"{kind}-repeat-1", OWNER_UID, kind, target,
                {"enabled": True}, {"enabled": True}, hourly_limit=100)
            self.assertFalse(dispatch)
            # An unresolved inverse cannot be bypassed by another desired state.
            dispatch, _ = store.prepare(f"{kind}-uncertain-1", OWNER_UID, kind, target,
                {"enabled": False}, {"enabled": False}, hourly_limit=100)
            self.assertTrue(dispatch)
            store.finish(f"{kind}-uncertain-1", "unknown", {"enabled": False})
            dispatch, _ = store.prepare(f"{kind}-uncertain-2", OWNER_UID, kind, target,
                {"enabled": True}, {"enabled": True}, hourly_limit=100)
            self.assertFalse(dispatch)

    async def test_permission_revoked_during_owner_await_prevents_dispatch(self):
        self.policy(comment=True)
        async def revoke_during_identity(credential):
            await asyncio.sleep(0)
            self.policy(comment=False)
            return {"mid": int(OWNER_UID)}
        self.upstream.user.get_self_info = revoke_during_identity
        result = await self.call("send", bvid=BV, text="must not send", action_id="revoke-owner-1")
        self.assertEqual(result["error"], "SOCIAL_PERMISSION_DISABLED_COMMENT")
        self.assertFalse(result["dispatched_now"])
        self.assertEqual(self.send_count, 0)
        self.assertIsNone(self.tools.store.get("revoke-owner-1", OWNER_UID))

    async def test_permission_revoked_during_context_await_prevents_dispatch(self):
        self.policy(reply=True)
        original = self.upstream.comment.Comment.get_sub_comments
        async def revoke_during_context(node, **arguments):
            result = await original(node, **arguments)
            self.policy(reply=False)
            return result
        self.upstream.comment.Comment.get_sub_comments = revoke_during_context
        result = await self.call("send", bvid=BV, root=ROOT, parent=CHILD,
            text="must not reply", action_id="revoke-context-1", action_kind="reply")
        self.assertEqual(result["error"], "SOCIAL_PERMISSION_DISABLED_REPLY")
        self.assertFalse(result["dispatched_now"])
        self.assertEqual(self.send_count, 0)
        self.assertIsNone(self.tools.store.get("revoke-context-1", OWNER_UID))

    async def test_finish_storage_failure_after_sdk_reports_uncertain_dispatch(self):
        self.policy(comment=True)
        for index, failure in enumerate((sqlite3.OperationalError("fixture db unavailable"), OSError("fixture disk unavailable"))):
            action_id = f"finish-failure-{index}"
            bvid = BV if index == 0 else "BV2xx411c7mD"
            with patch.object(self.tools.store, "finish", side_effect=failure):
                result = await self.call("send", bvid=bvid, text="one dispatch", action_id=action_id)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["dispatched_now"])
            self.assertTrue(result["dispatch_may_have_occurred"])
            self.assertEqual(result["action_id"], action_id)
            self.assertEqual(result["error"], "ACTION_RECEIPT_PERSISTENCE_FAILED")
            self.assertEqual(result["dispatch_outcome_before_persistence_failure"], "accepted")
            self.assertEqual(self.tools.store.get(action_id, OWNER_UID)["status"], "prepared")
            replay = await self.call("send", bvid=bvid, text="one dispatch", action_id=action_id)
            self.assertFalse(replay["dispatched_now"])
            self.assertEqual(replay["status"], "prepared")
        self.assertEqual(self.send_count, 2)


if __name__ == "__main__":
    unittest.main()
