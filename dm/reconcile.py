"""Maintainer-only recovery of an uncertain send; never sends/retries anything.

Requires actual platform history to match owner, recipient, exact server msg_key,
message-text digest and timestamp. A model statement cannot clear a ledger entry.
Only the recent native 30-message page is checked. Failure preserves uncertainty.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dm.module import ACTION_ID, DMError, DMTools, _message, _payload


async def reconcile(tools, action_id, platform_message_id):
    if not ACTION_ID.fullmatch(action_id) or not str(platform_message_id).isdigit():
        raise DMError("INVALID_RECONCILE_ARGUMENTS")
    policy, cred = await tools.owner()
    with tools.db() as conn:
        action = conn.execute("SELECT * FROM actions WHERE action_id=? AND owner_uid=?",
            (action_id, str(policy["owner_uid"]))).fetchone()
    if not action:
        raise DMError("ACTION_NOT_FOUND")
    if action["status"] not in ("prepared", "unknown"):
        return _payload(ok=action["status"] == "accepted", status=action["status"], changed=False, dispatched_now=False)
    history = await tools.upstream.session.fetch_session_msgs(talker_id=int(action["receiver_uid"]),
        credential=cred, session_type=1, begin_seqno=0)
    found = False
    for raw in (history.get("messages") or [])[:30]:
        item = _message(raw)
        if not item or str(item.get("msg_key")) != str(platform_message_id):
            continue
        content = item.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("content"), str):
            continue
        try:
            timestamp = float(item.get("timestamp") or 0)
            if timestamp > 10**12:
                timestamp /= 1000
            same_route = (int(item.get("sender_uid") or 0) == policy["owner_uid"]
                and int(item.get("receiver_id") or 0) == int(action["receiver_uid"]))
        except (TypeError, ValueError):
            continue
        digest = hashlib.sha256(json.dumps([policy["owner_uid"], int(action["receiver_uid"]),
            content["content"].strip()], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        found = (same_route and item.get("msg_type") == 1 and digest == action["digest"]
            and timestamp >= action["created_at"] - 5 and timestamp <= time.time() + 5)
        if found:
            break
    if not found:
        return _payload(ok=False, error="PLATFORM_HISTORY_DOES_NOT_PROVE_DELIVERY", changed=False, dispatched_now=False)
    with tools.db() as conn:
        cursor = conn.execute("UPDATE actions SET status='accepted',updated_at=?,remote_id=?,error_code=NULL WHERE action_id=? AND digest=? AND status IN ('prepared','unknown')",
            (time.time(), str(platform_message_id), action_id, action["digest"]))
    return _payload(ok=cursor.rowcount == 1, status="accepted", changed=cursor.rowcount == 1,
        platform_message_id=str(platform_message_id), dispatched_now=False,
        evidence="Server-returned recent history matched owner, recipient, msg_key, content hash and timestamp.")


async def main():
    from readonly_server import load_upstream
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--platform-message-id", required=True)
    args = parser.parse_args()
    tools = DMTools(load_upstream(args.package, args.state), args.state)
    try:
        result = await asyncio.wait_for(reconcile(tools, args.action_id, args.platform_message_id), 18)
    except Exception as error:
        result = _payload(ok=False, error=str(error) if isinstance(error, DMError) else type(error).__name__, dispatched_now=False)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
