"""Optional account choices; same owner, permission file and durable social ledger.

Default policy denies every dispatch. No toggle tool, force option or batch API.
"""
import asyncio
import json
from social.module import ACTION, SocialError, envelope, ids, sid

TOOLS = ("bili_like", "bili_favorite_update", "bili_follow", "bili_account_action_status", "bili_permissions")


class AccountActions:
    def __init__(self, social):
        self.social = social

    async def perform(self, kind, action_id, bvid="", uid="", media_id="", enabled=True):
        if kind not in ("like", "favorite", "follow") or not isinstance(enabled, bool):
            raise SocialError("INVALID_ACCOUNT_ACTION")
        if not isinstance(action_id, str) or not ACTION.fullmatch(action_id):
            raise SocialError("INVALID_ACTION_ID")
        policy = self.social.policy()
        if policy["permissions"].get(kind) is not True:
            raise SocialError("SOCIAL_PERMISSION_DISABLED_" + kind.upper())
        policy, cred = await self.social.owner()
        if kind == "follow":
            uid = sid(uid)
            if uid == self.social.binding.owner_uid:
                raise SocialError("SELF_FOLLOW_NOT_ALLOWED")
            obj = self.social.upstream.user.User(uid=int(uid), credential=cred)
            target = "user:" + uid
        else:
            obj = self.social.video(bvid, cred)
            target = "video:" + bvid
        if kind == "favorite":
            media_id = sid(media_id)
            # Selecting the exact folder is mandatory for add and remove.
            target += ":folder:" + media_id
        metadata = {"kind": kind, "enabled": enabled, "bvid": bvid or None,
            "uid": uid or None, "media_id": media_id or None,
            "platform_accepted": None, "state_verified": False}
        # Owner/network awaits must not retain an authorization revoked meanwhile.
        policy = self.social.policy()
        if policy["permissions"].get(kind) is not True:
            raise SocialError("SOCIAL_PERMISSION_DISABLED_" + kind.upper())
        dispatch, prior = self.social.store.prepare(action_id, self.social.binding.owner_uid, kind, target,
            {"enabled": enabled}, metadata,
            hourly_limit=min(5, max(1, int(policy.get("max_actions_per_hour", 3)))))
        if not dispatch:
            return envelope(ok=prior["status"] == "accepted", action_id=action_id,
                prior_action_id=prior["action_id"], status=prior["status"],
                result=ids(json.loads(prior["metadata"])), dispatched_now=False, duplicate_suppressed=True)
        status, error, persistence_error = "unknown", None, None
        try:
            if kind == "like":
                await obj.like(status=enabled)
            elif kind == "favorite":
                await obj.set_favorite(add_media_ids=[int(media_id)] if enabled else [],
                    del_media_ids=[] if enabled else [int(media_id)])
            else:
                relation = self.social.upstream.user.RelationType
                await obj.modify_relation(relation.SUBSCRIBE if enabled else relation.UNSUBSCRIBE)
            # SDK successful return proves only an accepted operation, until a readback.
            status, metadata["platform_accepted"] = "accepted", True
        except BaseException as exc:
            error = type(exc).__name__
            if error == "ResponseCodeException" and isinstance(getattr(exc, "code", None), int):
                status, metadata["platform_accepted"] = "failed", False
                metadata["platform_error_code"] = exc.code
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            try:
                self.social.store.finish(action_id, status, metadata, error)
            except Exception as exc:
                # prepared remains durable: a missing local final receipt cannot
                # turn an already attempted platform write into "not dispatched".
                persistence_error, status = type(exc).__name__, "unknown"
        return envelope(ok=status == "accepted", action_id=action_id, status=status,
            result=metadata, dispatched_now=True, error=error,
            persistence_error=persistence_error, dispatch_may_have_occurred=True,
            note="A successful SDK response is not an independently verified account state. Unknown never automatically retries.")

    async def action_status(self, action_id):
        if not isinstance(action_id, str) or not ACTION.fullmatch(action_id):
            raise SocialError("INVALID_ACTION_ID")
        if self.social.binding.owner_uid is None:
            raise SocialError("BILI_OWNER_NOT_CONFIGURED")
        self.social.policy()
        return envelope(ok=True, action=self.social.store.get(action_id, self.social.binding.owner_uid), dispatched_now=False)

    async def permissions(self):
        policy = self.social.policy()
        # Only fixed, non-secret permission facts; no edit/enable endpoint.
        dm = self.social.state / "bili_dm_policy.json"
        try:
            data = json.loads(dm.read_text(encoding="utf-8"))
            dm_enabled = self.social.binding.owner_uid is not None and data.get("life_id") == policy["life_id"] and str(data.get("owner_uid")) == self.social.binding.owner_uid and data.get("send_enabled") is True
        except (OSError, ValueError):
            dm_enabled = False
        return envelope(ok=True, owner_uid=self.social.binding.owner_uid,
            permissions={kind: policy["permissions"].get(kind) is True for kind in ("comment", "reply", "like", "favorite", "follow")} | {"dm": dm_enabled},
            max_combined_public_actions_per_hour=min(5, max(1, int(policy.get("max_actions_per_hour", 3)))),
            permission_edit_tool_available=False)

    async def invoke(self, method, **arguments):
        try:
            result = await asyncio.wait_for(getattr(self, method)(**arguments), 22)
        except SocialError as exc:
            result = envelope(ok=False, error=str(exc), dispatched_now=False)
        except Exception as exc:
            result = envelope(ok=False, error=type(exc).__name__, dispatched_now=False,
                note="No automatic write retry. Inspect the durable action status after an uncertain result.")
        result["life_id"] = self.social.binding.life_id
        return json.dumps(ids(result), ensure_ascii=False)


def register(server, social):
    tools = AccountActions(social)

    @server.tool()
    async def bili_like(bvid: str, enabled: bool, action_id: str) -> str:
        """Like/unlike one video only with explicit like permission; defaults OFF. Stable action_id, no force/batch/retry."""
        return await tools.invoke("perform", kind="like", bvid=bvid, enabled=enabled, action_id=action_id)

    @server.tool()
    async def bili_favorite_update(bvid: str, media_id: str, enabled: bool, action_id: str) -> str:
        """Add/remove ONE video in ONE exact own folder only with favorite permission; defaults OFF. Folder ID string; no batch."""
        return await tools.invoke("perform", kind="favorite", bvid=bvid, media_id=media_id, enabled=enabled, action_id=action_id)

    @server.tool()
    async def bili_follow(uid: str, enabled: bool, action_id: str) -> str:
        """Follow/unfollow ONE UID only with explicit follow permission; defaults OFF. No block/marketing/batch."""
        return await tools.invoke("perform", kind="follow", uid=uid, enabled=enabled, action_id=action_id)

    @server.tool()
    async def bili_account_action_status(action_id: str) -> str:
        """Read durable public-action receipt/state only; never dispatches or retries. Receipt is not verified account state."""
        return await tools.invoke("action_status", action_id=action_id)

    @server.tool()
    async def bili_permissions() -> str:
        """Read separate comment/reply/DM/like/favorite/follow gates and combined public hourly limit. Cannot change permissions."""
        return await tools.invoke("permissions")

    return tools
