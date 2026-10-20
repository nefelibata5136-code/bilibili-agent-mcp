"""Owner verification used only before the downloader's login fallback."""
import asyncio

from binding import PROCESS_BINDING, ProcessBinding


async def sdk_identity(credentials):
    """Use the installed SDK and close only clients created for this short loop."""
    from bilibili_api import Credential, user
    from bilibili_api.utils import network

    loop = asyncio.get_running_loop()
    try:
        credential = Credential(**{key: credentials.get(key) for key in
            ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")})
        return await user.get_self_info(credential)
    finally:
        # SDK 17.4.2 keys clients/settings by event loop. This function owns this
        # short worker loop, never a main-server loop or another owner's client.
        for client_name, pool in list(network.session_pool.items()):
            client = pool.pop(loop, None)
            network.lazy_settings.get(client_name, {}).pop(loop, None)
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), 2)
                except Exception:
                    pass


def verify_owner(credentials, binding=PROCESS_BINDING, verifier=None):
    """Return only a fixed result code; callers never receive credentials/errors."""
    if not isinstance(binding, ProcessBinding):
        return "BILI_WATCH_BINDING_INVALID"
    if binding.owner_uid is None:
        return "BILI_WATCH_OWNER_NOT_CONFIGURED"
    if not isinstance(credentials, dict) or not credentials.get("sessdata"):
        return "BILI_LOGIN_REQUIRED"
    if str(credentials.get("dedeuserid") or "") != binding.owner_uid:
        return "BILI_WATCH_CREDENTIAL_OWNER_MISMATCH"

    async def verify():
        return await asyncio.wait_for((verifier or sdk_identity)(credentials), 8)

    try:
        info = asyncio.run(verify())
    except Exception:
        return "BILI_WATCH_OWNER_VERIFICATION_FAILED"
    if not isinstance(info, dict) or str(info.get("mid")) != binding.owner_uid:
        return "BILI_WATCH_AUTHENTICATED_OWNER_MISMATCH"
    return None
