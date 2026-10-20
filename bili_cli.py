"""Human-only local setup and QR login. Never registered as Agent tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

from binding import PROCESS_BINDING


class SetupError(Exception):
    """Known safe local validation failure, never an SDK exception."""


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".bili-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configured_owner():
    if not PROCESS_BINDING.owner_uid:
        raise SetupError("Set BILI_OWNER_UID to the account you control before local setup")
    return PROCESS_BINDING.owner_uid


def initialize(state: Path):
    owner = configured_owner()
    shared = {"life_id": PROCESS_BINDING.life_id, "owner_uid": owner}
    values = {
        "bili_social_policy.json": {**shared, "permissions": {
            key: False for key in ("comment", "reply", "like", "favorite", "follow")},
            "max_actions_per_hour": 3},
        "bili_dm_policy.json": {**shared, "send_enabled": False, "max_sends_per_hour": 3},
    }
    if any((state / name).exists() for name in values):
        raise SetupError("Policies already exist; inspect them locally instead of overwriting")
    for name, value in values.items():
        private_json(state / name, value)
    return {"ok": True, "policies_created": list(values), "all_writes_enabled": False}


async def login(state: Path, timeout: int = 180):
    owner = configured_owner()
    from bilibili_api import user
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
    session = QrCodeLogin()
    qr = state / "login-qr.png"
    state.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        state.chmod(0o700)
    try:
        await session.generate_qrcode()
        fd = os.open(qr, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(session.get_qrcode_picture().content)
        print("Open this local QR image and confirm login in your Bilibili app:", qr.resolve(), flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await session.check_state()
            if status == QrCodeLoginEvents.TIMEOUT:
                raise SetupError("QR_EXPIRED; run login again")
            if status == QrCodeLoginEvents.DONE:
                cred = session.get_credential()
                info = await user.get_self_info(cred)
                if str(info.get("mid")) != owner:
                    raise SetupError("ACCOUNT_OWNER_MISMATCH; existing credentials were preserved")
                private_json(state / "bili_credential.json", {
                    "sessdata": cred.sessdata, "bili_jct": cred.bili_jct,
                    "buvid3": cred.buvid3, "dedeuserid": owner,
                })
                return {"ok": True, "authenticated_owner_uid": owner, "credential_saved_locally": True}
            await asyncio.sleep(2)
        raise SetupError("QR_LOGIN_TIMEOUT; run login again")
    finally:
        qr.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "login"))
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = initialize(args.state.resolve()) if args.action == "init" else asyncio.run(login(args.state.resolve()))
    except SetupError as error:
        # Only locally constructed validation errors can reach this output.
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    except Exception as error:
        # SDK/transport messages may contain cookies or signed URLs.
        print(json.dumps({"ok": False, "error": type(error).__name__,
                          "hint": "Local operation failed; credentials were not printed."}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
