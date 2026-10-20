"""Mock dispatch and uncertainty tests; never use a real Bilibili account."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from account_actions import AccountActions
from social.module import SocialTools
from binding import ProcessBinding

LIFE_ID, OWNER_UID = "test-agent", "10001"
BINDING = ProcessBinding(LIFE_ID, OWNER_UID)


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.raise_error = False
        async def self_info(cred):
            return {"mid": OWNER_UID}
        async def dispatch(**kwargs):
            self.calls.append(kwargs)
            if self.raise_error:
                raise TimeoutError()
            return {}
        async def follow(relation):
            return await dispatch(relation=relation)
        obj = SimpleNamespace(like=dispatch, set_favorite=dispatch, modify_relation=follow)
        self.upstream = SimpleNamespace(get_cred=lambda: SimpleNamespace(),
            user=SimpleNamespace(get_self_info=self_info, User=lambda **kw: obj,
                RelationType=SimpleNamespace(SUBSCRIBE=1, UNSUBSCRIBE=2)),
            video=SimpleNamespace(Video=lambda **kw: obj))
        self.social = SocialTools(self.upstream, self.root, binding=BINDING)
        self.tools = AccountActions(self.social)

    def tearDown(self):
        self.temp.cleanup()

    def policy(self, **permissions):
        (self.root / "bili_social_policy.json").write_text(json.dumps({"life_id": LIFE_ID,
            "owner_uid": OWNER_UID, "permissions": permissions, "max_actions_per_hour": 3}), encoding="utf-8")

    async def call(self, **kwargs):
        return json.loads(await self.tools.invoke("perform", **kwargs))

    async def test_each_default_gate_blocks_before_http(self):
        async def forbidden(*a, **kw):
            self.fail("Disabled gate must not contact server")
        self.upstream.user.get_self_info = forbidden
        for kind in ("like", "favorite", "follow"):
            result = await self.call(kind=kind, action_id="disabled-" + kind,
                bvid="BV1xx411c7mD", uid="123", media_id="456")
            self.assertEqual(result["error"], "SOCIAL_PERMISSION_DISABLED_" + kind.upper())
        self.assertEqual(self.calls, [])

    async def test_separate_permission_and_same_id_deduplication(self):
        self.policy(like=True)
        first = await self.call(kind="like", action_id="like-test-001", bvid="BV1xx411c7mD")
        second = await self.call(kind="like", action_id="like-test-001", bvid="BV1xx411c7mD")
        other = await self.call(kind="follow", action_id="follow-test-001", uid="123")
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(second["duplicate_suppressed"])
        self.assertEqual(other["error"], "SOCIAL_PERMISSION_DISABLED_FOLLOW")
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_blocks_changed_action_and_payload(self):
        self.policy(like=True)
        self.raise_error = True
        result = await self.call(kind="like", action_id="uncertain-test-1", bvid="BV1xx411c7mD")
        again = await self.call(kind="like", action_id="uncertain-test-2", bvid="BV1xx411c7mD", enabled=False)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(again["dispatched_now"])
        self.assertEqual(len(self.calls), 1)

    async def test_exact_folder_add_and_remove(self):
        self.policy(favorite=True)
        for enabled, action in ((True, "folder-test-add"), (False, "folder-test-remove")):
            result = await self.call(kind="favorite", action_id=action, bvid="BV1xx411c7mD", media_id="8000000000000000001", enabled=enabled)
            self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.calls[0], {"add_media_ids": [8000000000000000001], "del_media_ids": []})
        self.assertEqual(self.calls[1], {"add_media_ids": [], "del_media_ids": [8000000000000000001]})

    async def test_owner_mismatch_and_hourly_cap(self):
        self.policy(follow=True)
        for i in range(3):
            await self.call(kind="follow", action_id=f"follow-cap-{i}", uid=str(100+i))
        blocked = await self.call(kind="follow", action_id="follow-cap-last", uid="500")
        self.assertEqual(blocked["error"], "SOCIAL_HOURLY_LIMIT_REACHED")
        async def wrong_owner(cred):
            return {"mid": "999"}
        self.upstream.user.get_self_info = wrong_owner
        mismatch = await self.call(kind="follow", action_id="owner-mismatch", uid="501")
        self.assertEqual(mismatch["error"], "SOCIAL_ACCOUNT_OWNER_MISMATCH")

    async def test_status_never_sends_and_permission_tool_cannot_edit(self):
        result = json.loads(await self.tools.invoke("action_status", action_id="not-yet-sent"))
        self.assertIsNone(result["action"])
        permission = json.loads(await self.tools.invoke("permissions"))
        self.assertFalse(permission["permission_edit_tool_available"])
        self.assertFalse(any(permission["permissions"].values()))
        self.assertEqual(self.calls, [])

    async def test_permission_revoked_during_owner_await_blocks_dispatch(self):
        self.policy(like=True)
        async def self_info_and_revoke(cred):
            self.policy(like=False)
            return {"mid": OWNER_UID}
        self.upstream.user.get_self_info = self_info_and_revoke
        result = await self.call(kind="like", action_id="revoked-during-await", bvid="BV1xx411c7mD")
        self.assertEqual(result["error"], "SOCIAL_PERMISSION_DISABLED_LIKE")
        self.assertEqual(self.calls, [])

    async def test_finish_failure_preserves_prepared_and_uncertainty(self):
        self.policy(like=True)
        def fail_finish(*args, **kwargs):
            raise OSError("FIXTURE local storage failure")
        self.social.store.finish = fail_finish
        result = await self.call(kind="like", action_id="finish-failure-001", bvid="BV1xx411c7mD")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["dispatched_now"])
        self.assertTrue(result["dispatch_may_have_occurred"])
        self.assertEqual(self.social.store.get("finish-failure-001", OWNER_UID)["status"], "prepared")
        repeated = await self.call(kind="like", action_id="finish-failure-002", bvid="BV1xx411c7mD")
        self.assertFalse(repeated["dispatched_now"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
