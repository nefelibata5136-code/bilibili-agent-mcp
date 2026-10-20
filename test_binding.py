"""Offline process-binding and account-isolation regressions."""
from dataclasses import FrozenInstanceError
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binding import PROCESS_BINDING, ProcessBinding, from_environment
from social.module import SocialTools, register as register_social
from dm.module import DMTools, register as register_dm
from account_actions import AccountActions


class BindingTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_defaults_and_validation(self):
        self.assertEqual(from_environment({}), ProcessBinding("local-agent", None))
        self.assertEqual(from_environment({"BILI_LIFE_ID": "test-a", "BILI_OWNER_UID": "00123"}),
            ProcessBinding("test-a", "123"))
        for owner in ("0", "-1", "1.0", "true", "１２３", "9223372036854775808"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                from_environment({"BILI_OWNER_UID": owner})

    def test_binding_is_frozen_and_environment_changes_do_not_rebind(self):
        environment = {"BILI_LIFE_ID": "test-a", "BILI_OWNER_UID": "123"}
        binding = from_environment(environment)
        environment["BILI_OWNER_UID"] = "456"
        self.assertEqual(binding.owner_uid, "123")
        with self.assertRaises(FrozenInstanceError):
            binding.owner_uid = "456"
        with patch.dict("os.environ", {"BILI_OWNER_UID": "456"}):
            self.assertIs(SocialTools.__init__.__kwdefaults__["binding"], PROCESS_BINDING)
            self.assertIs(DMTools.__init__.__kwdefaults__["binding"], PROCESS_BINDING)

    async def test_unconfigured_owner_cannot_adopt_policy_or_contact_account(self):
        async def forbidden(*args, **kwargs):
            self.fail("Unconfigured owner must fail before account HTTP")
        upstream = SimpleNamespace(user=SimpleNamespace(get_self_info=forbidden),
            get_cred=lambda: self.fail("Unconfigured owner must fail before credentials"))
        with tempfile.TemporaryDirectory() as temp:
            binding = ProcessBinding("test-agent")
            social, dm = SocialTools(upstream, temp, binding=binding), DMTools(upstream, temp, binding=binding)
            account = AccountActions(social)
            disabled = json.loads(await social.invoke("send", bvid="BV1xx411c7mD",
                text="fixture", action_id="default-disabled-001"))
            self.assertEqual(disabled["error"], "SOCIAL_PERMISSION_DISABLED_COMMENT")
            self.assertEqual(json.loads(await dm.invoke("sessions"))["error"], "BILI_OWNER_NOT_CONFIGURED")
            self.assertEqual(json.loads(await social.invoke("notifications"))["error"], "BILI_OWNER_NOT_CONFIGURED")
            self.assertEqual(json.loads(await social.invoke("social_events", cursor="cached-page"))["error"], "BILI_OWNER_NOT_CONFIGURED")
            self.assertFalse(json.loads(await account.invoke("permissions"))["permissions"]["dm"])
            Path(temp, "bili_social_policy.json").write_text(json.dumps({"life_id": "test-agent",
                "owner_uid": "123", "permissions": {"comment": True}}), encoding="utf-8")
            adopted = json.loads(await social.invoke("send", bvid="BV1xx411c7mD",
                text="fixture", action_id="must-not-adopt-001"))
            self.assertEqual(adopted["error"], "BILI_OWNER_NOT_CONFIGURED")
            self.assertFalse(Path(temp, "bili_dm_actions.sqlite3").exists())

    async def test_policy_cannot_change_fixed_life_or_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            binding = ProcessBinding("test-a", "123")
            social = SocialTools(SimpleNamespace(), temp, binding=binding)
            dm = DMTools(SimpleNamespace(), temp, binding=binding)
            for life, owner in (("test-b", "123"), ("test-a", "456")):
                policy = {"life_id": life, "owner_uid": owner, "permissions": {"comment": True},
                    "send_enabled": True}
                for name in ("bili_social_policy.json", "bili_dm_policy.json"):
                    Path(temp, name).write_text(json.dumps(policy), encoding="utf-8")
                self.assertEqual(json.loads(await dm.invoke("sessions"))["error"], "DM_OWNER_POLICY_INVALID")
                self.assertEqual(json.loads(await social.invoke("send", bvid="BV1xx411c7mD",
                    text="fixture", action_id="wrong-binding-001"))["error"], "SOCIAL_OWNER_POLICY_INVALID")

    async def test_no_registered_tool_exposes_binding_configuration(self):
        class Server:
            def __init__(self):
                self.functions = []
            def tool(self):
                def bind(function):
                    self.functions.append(function)
                    return function
                return bind
        with tempfile.TemporaryDirectory() as temp:
            server = Server()
            binding = ProcessBinding("test-agent", "123")
            with patch("dm.module.request_settings.set_wbi_retry_times"):
                register_social(server, SimpleNamespace(), temp, binding=binding)
                register_dm(server, SimpleNamespace(), temp, binding=binding)
            for function in server.functions:
                self.assertFalse({"binding", "life_id", "owner_uid", "BILI_OWNER_UID"}
                    & set(inspect.signature(function).parameters))


if __name__ == "__main__":
    unittest.main()
