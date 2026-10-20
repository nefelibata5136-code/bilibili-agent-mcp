"""Read-only SDK discovery, resumable pages and explicit local navigation state.

No Agent, scheduler, account write, browser Cookie import or memory writer.
Snapshots/cursors are local evidence of fetched public pages, not proof of watching.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from bilibili_api import homepage, hot, rank, dynamic, Credential
from bilibili_api.utils.network import request_settings
from binding import PROCESS_BINDING, ProcessBinding

TOOLS = ("bili_discover", "bili_related", "bili_danmaku", "bili_favorite_lists",
         "bili_favorite_content", "bili_followings", "bili_browse_state", "bili_browse_advance")
BV = re.compile(r"BV[0-9A-Za-z]{10}\Z")
TOKEN = re.compile(r"[a-f0-9]{32}:[0-9]{1,7}\Z")
MAX_OUTPUT_BYTES = 96000


class BrowseError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 2**63 - 1:
        raise BrowseError("INVALID_ID")
    return str(int(value))


def bvid(value):
    if not isinstance(value, str) or not BV.fullmatch(value):
        raise BrowseError("INVALID_BVID")
    return value


def ids(value):
    """JSON IDs retain full Python integer precision before any JS consumer."""
    if isinstance(value, list):
        return [ids(x) for x in value]
    if isinstance(value, dict):
        return {k: str(v) if isinstance(v, int) and not isinstance(v, bool)
                and (k in {"id", "mid", "uid", "aid", "cid", "fid", "media_id", "talker_id"}
                     or k.endswith("_id")) else ids(v) for k, v in value.items()}
    return value


def failure(error):
    code = getattr(error, "code", None)
    if code in (-352, -412, 412, 429) or "captcha" in type(error).__name__.lower():
        return "BILI_PLATFORM_RISK_CONTROL"
    if code in (-101, -111, -400):
        return "BILI_AUTH_OR_PERMISSION_REQUIRED"
    if isinstance(error, asyncio.TimeoutError):
        return "BILI_READ_TIMEOUT"
    return "BILI_SOURCE_UNAVAILABLE"


def video_item(item):
    if not isinstance(item, dict):
        return None
    bv = item.get("bvid") or item.get("bv_id")
    if not isinstance(bv, str) or not BV.fullmatch(bv):
        return None  # ads, live/PGC entries and arbitrary links are not videos here
    owner = item.get("owner") or item.get("upper") or {}
    if not isinstance(owner, dict):
        owner = {}
    return {"ref": "video:" + bv, "bvid": bv, "title": item.get("title", ""),
        "description": item.get("desc", ""), "author": owner.get("name") or item.get("author", ""),
        "author_uid": str(owner.get("mid") or owner.get("uid") or "") or None,
        "duration_seconds": item.get("duration"), "published_at_epoch": item.get("pubdate"),
        "reason": item.get("rcmd_reason"), "url": "https://www.bilibili.com/video/" + bv,
        "statistics": ids(item.get("stat") or item.get("cnt_info") or {})}


class BrowseStore:
    def __init__(self, state):
        self.path = Path(state) / "bili_browse.sqlite3"

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,kind TEXT NOT NULL,items TEXT NOT NULL,meta TEXT NOT NULL,continuation TEXT,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cursors(token TEXT PRIMARY KEY,snapshot TEXT NOT NULL,offset INTEGER NOT NULL,resolved TEXT);
        CREATE TABLE IF NOT EXISTS seen(ref TEXT PRIMARY KEY,item TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,decision TEXT,part INTEGER,position REAL);
        CREATE TABLE IF NOT EXISTS seen_pages(snapshot TEXT NOT NULL,ref TEXT NOT NULL,PRIMARY KEY(snapshot,ref));
        CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,ref TEXT NOT NULL,decision TEXT NOT NULL,part INTEGER NOT NULL,position REAL,occurred TEXT NOT NULL);
        """)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def snapshot(self, kind, items, meta, continuation=None):
        sid = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", (sid, kind,
                json.dumps(items, ensure_ascii=False), json.dumps(meta, ensure_ascii=False),
                json.dumps(continuation) if continuation else None, now()))
        return sid

    def get(self, sid):
        with self.db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
        if not row:
            raise BrowseError("BROWSE_SNAPSHOT_NOT_FOUND")
        return {**dict(row), "items": json.loads(row["items"]), "meta": json.loads(row["meta"]),
                "continuation": json.loads(row["continuation"]) if row["continuation"] else None}

    def cursor(self, sid, offset):
        token = sid + ":" + str(offset)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO cursors VALUES(?,?,?,NULL)", (token, sid, offset))
        return token

    def resolve(self, token, kind):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise BrowseError("INVALID_BROWSE_CURSOR")
        with self.db() as db:
            row = db.execute("SELECT * FROM cursors WHERE token=?", (token,)).fetchone()
        if not row:
            raise BrowseError("BROWSE_CURSOR_NOT_FOUND")
        snapshot = self.get(row["snapshot"])
        if snapshot["kind"] != kind:
            raise BrowseError("BROWSE_CURSOR_KIND_MISMATCH")
        return snapshot, row["offset"], row["resolved"]

    def mark_seen(self, sid, items):
        with self.db() as db:
            for item in items:
                ref = item.get("ref")
                if not ref:
                    continue
                added = db.execute("INSERT OR IGNORE INTO seen_pages VALUES(?,?)", (sid, ref)).rowcount
                if added:
                    at = now()
                    db.execute("INSERT INTO seen VALUES(?,?,?,?,NULL,NULL,NULL) ON CONFLICT(ref) DO UPDATE SET item=excluded.item,last_seen=excluded.last_seen",
                        (ref, json.dumps(item, ensure_ascii=False), at, at))


class BrowseTools:
    def __init__(self, upstream, state, *, binding: ProcessBinding = PROCESS_BINDING):
        self.upstream = upstream
        self.binding = binding
        self.store = BrowseStore(state)
        self.lock = asyncio.Lock()

    def credential(self):
        try:
            return self.upstream.get_cred()
        except Exception:
            return None

    def safe(self, value):
        text = json.dumps(value, ensure_ascii=False)
        cred = self.credential()
        if cred:
            for name in ("sessdata", "bili_jct", "buvid3", "buvid4"):
                secret = getattr(cred, name, None)
                if isinstance(secret, str) and secret:
                    text = text.replace(secret, "[redacted]")
        return json.loads(text)

    async def own_uid(self, cred):
        if self.binding.owner_uid is None:
            raise BrowseError("BILI_OWNER_NOT_CONFIGURED")
        if not cred or not getattr(cred, "sessdata", None):
            raise BrowseError("BILI_AUTH_OR_PERMISSION_REQUIRED")
        # fetch() already independently authenticated this exact credential.
        return self.binding.owner_uid

    async def read_credential(self, *, require_owner=False):
        if self.binding.owner_uid is None:
            if require_owner:
                raise BrowseError("BILI_OWNER_NOT_CONFIGURED")
            return Credential()
        cred = self.credential()
        if not cred or not getattr(cred, "sessdata", None):
            if require_owner:
                raise BrowseError("BILI_AUTH_OR_PERMISSION_REQUIRED")
            return Credential()
        info = await self.upstream.user.get_self_info(cred)
        if str(info.get("mid")) != self.binding.owner_uid:
            raise BrowseError("BILI_ACCOUNT_OWNER_MISMATCH")
        return cred

    async def fetch(self, kind, args):
        require_owner = ((kind in ("favorite_lists", "followings") and not args.get("uid"))
            or (kind == "discover" and args.get("source") == "following"))
        public_only = kind == "state" or (kind == "discover"
            and args.get("source") in ("popular", "ranking", "weekly"))
        cred = Credential() if public_only else await self.read_credential(require_owner=require_owner)
        items, meta, continuation = await self._fetch(kind, args, cred)
        meta.update(life_id=self.binding.life_id,
            credential_owner_uid=self.binding.owner_uid if getattr(cred, "sessdata", None) else None)
        return items, meta, continuation

    async def _fetch(self, kind, args, cred):
        if kind == "state":
            with self.store.db() as db:
                where = " WHERE last_seen<? OR (last_seen=? AND ref>?)" if args.get("before") else ""
                values = (args["before"], args["before"], args["after_ref"]) if where else ()
                rows = db.execute("SELECT * FROM seen" + where + " ORDER BY last_seen DESC,ref ASC LIMIT 101", values).fetchall()
                total = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            chosen = rows[:100]
            continuation = {"before": chosen[-1]["last_seen"], "after_ref": chosen[-1]["ref"]} if len(rows) > 100 else None
            return [{**dict(row), "item": json.loads(row["item"])} for row in chosen], {
                "source": "local_browse_navigation", "actual_source": "local_browse_navigation", "saved_locally": True,
                "total_refs": total, "scope": "navigation_only_not_long_term_memory_or_watch_history"}, continuation
        if kind == "discover":
            return await self.discover_fetch(args, cred)
        if kind == "related":
            data = await self.upstream.video.Video(bvid=args["bvid"], credential=cred).get_related()
            source = data if isinstance(data, list) else data.get("list", [])
            return self.videos(source), {"actual_source": "related", "bvid": args["bvid"],
                "scope": "platform_related_snapshot", "native_pagination": False}, None
        if kind == "danmaku":
            video = self.upstream.video.Video(bvid=args["bvid"], credential=cred)
            # Only one six-minute segment per native request; no full-video crawl.
            segment = int(args["start_seconds"] // 360)
            data = await video.get_danmakus(page_index=args["part"] - 1, from_seg=segment, to_seg=segment)
            items, seen_ids = [], set()
            empty_text_removed = duplicate_ids_removed = 0
            for dm in data:
                at = float(dm.dm_time.total_seconds() if hasattr(dm.dm_time, "total_seconds") else dm.dm_time)
                if args["start_seconds"] <= at < args["end_seconds"]:
                    text = getattr(dm, "text", None)
                    if not isinstance(text, str) or not text.strip():
                        empty_text_removed += 1
                        continue
                    ident = None
                    for candidate in (getattr(dm, "id_str", None), getattr(dm, "id_", None)):
                        if not isinstance(candidate, bool) and str(candidate).isdigit() and int(candidate) > 0:
                            ident = str(int(candidate))
                            break
                    if ident is None:
                        ident = hashlib.sha256(json.dumps([at, text, dm.send_time], ensure_ascii=False).encode()).hexdigest()
                    if ident in seen_ids:
                        duplicate_ids_removed += 1
                        continue
                    seen_ids.add(ident)
                    items.append({"ref": f'danmaku:{args["bvid"]}:{args["part"]}:{ident}',
                        "danmaku_id": ident, "bvid": args["bvid"], "part": args["part"],
                        "appears_at_seconds": at, "text": text, "sent_at_epoch": dm.send_time,
                        "mode": getattr(dm, "mode", None), "author_uid": None,
                        "identity_boundary": "No reverse lookup of hashed sender identity."})
            items.sort(key=lambda x: (x["appears_at_seconds"], x["danmaku_id"]))
            return items, {"actual_source": "danmaku", "bvid": args["bvid"], "part": args["part"],
                "range_seconds": [args["start_seconds"], args["end_seconds"]], "native_segment": segment,
                "filtering": {"empty_text_removed": empty_text_removed, "duplicate_ids_removed": duplicate_ids_removed},
                "scope": "current_server_segment_snapshot_not_all_video_danmaku", "native_pagination": False}, None
        if kind == "favorite_lists":
            # Reuse the installed vendor's exact read function, without registering its writes.
            owner = args.get("uid") or await self.own_uid(cred)
            data = json.loads(await self.upstream.bili_favorite_lists(uid=int(owner), credential=cred))
            if not isinstance(data, dict) or "error" in data:
                raise BrowseError("BILI_FAVORITE_LISTS_UPSTREAM_FAILURE")
            items = [{**ids(item), "ref": "favorite:" + str(item["id"])} for item in data.get("lists", [])]
            return items, {"actual_source": "favorite_lists", "uid": owner,
                "scope": "account_accessible_lists_only", "native_pagination": False}, None
        if kind == "favorite_content":
            data = json.loads(await self.upstream.bili_favorite_content(media_id=int(args["media_id"]), page=args["page"], credential=cred))
            if not isinstance(data, dict) or "error" in data:
                raise BrowseError("BILI_FAVORITE_CONTENT_UPSTREAM_FAILURE")
            continuation = {**args, "page": args["page"] + 1} if data.get("has_more") else None
            return self.videos(data.get("medias", [])), {"actual_source": "favorite_content",
                "media_id": args["media_id"], "native_page": args["page"],
                "scope": "account_accessible_favorite_folder", "native_pagination": True}, continuation
        if kind == "followings":
            owner = args.get("uid") or await self.own_uid(cred)
            data = await self.upstream.user.User(uid=int(owner), credential=cred).get_followings(pn=args["page"], ps=20)
            source = data.get("list") or []
            items = [{"ref": "user:" + uid(item.get("mid")), "uid": uid(item.get("mid")),
                "name": item.get("uname", ""), "signature": item.get("sign", ""),
                "url": "https://space.bilibili.com/" + uid(item.get("mid"))} for item in source]
            total = data.get("total")
            more = len(source) == 20 and (not isinstance(total, int) or args["page"] * 20 < total)
            continuation = {**args, "uid": owner, "page": args["page"] + 1} if more else None
            return items, {"actual_source": "followings", "uid": owner, "native_page": args["page"],
                "scope": "platform_accessible_followings_only", "native_pagination": True,
                "other_user_limit": "SDK documents a five-page access limit for other users; permission errors stop."}, continuation
        raise BrowseError("UNKNOWN_BROWSE_OPERATION")

    @staticmethod
    def videos(items):
        output, seen = [], set()
        for item in items:
            video = video_item(item)
            if video and video["bvid"] not in seen:
                output.append(video)
                seen.add(video["bvid"])
        return output

    async def discover_fetch(self, args, cred):
        source, page = args["source"], args.get("page", 1)
        continuation = None
        meta = {"requested_source": args.get("requested_source", source), "actual_source": source,
                "personalization": "not_claimed", "native_pagination": False,
                "refresh_requested": args.get("refresh_requested", False)}
        if args.get("fallback_reason"):
            meta["fallback"] = {"from": "homepage", "to": "popular", "reason": args["fallback_reason"]}
        try:
            if source == "homepage":
                data = await homepage.get_videos(credential=cred)
                raw = data.get("item") or []
                meta.update(scope="homepage_recommendation_snapshot", personalization="credential_context_supplied_algorithm_unknown" if getattr(cred, "sessdata", None) else "anonymous_homepage", refresh_hint="SDK has no native homepage cursor; start a new discovery request to refresh after this snapshot ends.")
            elif source == "popular":
                data = await hot.get_hot_videos(pn=page, ps=20)
                raw = data.get("list") or []
                meta.update(scope="sitewide_popular", native_pagination=True, native_page=page)
                continuation = {**args, "page": page + 1} if raw and not data.get("no_more", len(raw) < 20) else None
            elif source == "ranking":
                category = args.get("category", "All")
                if category not in rank.RankType.__members__:
                    raise BrowseError("INVALID_RANKING_CATEGORY")
                data = await rank.get_rank(type_=rank.RankType[category])
                raw = data.get("list") or []
                meta.update(scope="sitewide_ranking" if category == "All" else "category_ranking", category=category)
            elif source == "weekly":
                weeks = await hot.get_weekly_hot_videos_list()
                entries = weeks.get("list") or []
                if not entries:
                    raise BrowseError("WEEKLY_LIST_EMPTY")
                number = int(entries[0]["number"])
                data = await hot.get_weekly_hot_videos(week=number)
                raw = data.get("list") or []
                meta.update(scope="sitewide_weekly_curated", week=str(number))
            elif source == "following":
                if not getattr(cred, "sessdata", None):
                    raise BrowseError("BILI_AUTH_OR_PERMISSION_REQUIRED")
                data = await dynamic.get_dynamic_page_info(credential=cred, _type=dynamic.DynamicType.VIDEO,
                    pn=page, offset=int(args["native_offset"]) if args.get("native_offset") else None)
                raw = []
                for item in data.get("items") or []:
                    modules = item.get("modules") or {}
                    archive = ((modules.get("module_dynamic") or {}).get("major") or {}).get("archive")
                    if isinstance(archive, dict):
                        author = modules.get("module_author") or {}
                        raw.append({**archive, "owner": {"mid": author.get("mid"), "name": author.get("name")}, "pubdate": author.get("pub_ts")})
                offset = data.get("offset")
                if data.get("has_more") and offset:
                    continuation = {**args, "page": page + 1, "native_offset": str(offset)}
                meta.update(scope="following_video_feed", native_pagination=True, native_offset=str(offset) if offset else None)
            else:
                raise BrowseError("INVALID_DISCOVERY_SOURCE")
            return self.videos(raw), meta, continuation
        except BrowseError:
            raise
        except Exception as error:
            code = failure(error)
            if source != "homepage" or code in ("BILI_PLATFORM_RISK_CONTROL", "BILI_AUTH_OR_PERMISSION_REQUIRED", "BILI_READ_TIMEOUT"):
                raise BrowseError(code) from None
            # One ordinary public-source fallback, never retry/bypass platform risk control.
            items, actual, continuation = await self.discover_fetch({"source": "popular", "requested_source": "homepage", "page": 1,
                "fallback_reason": code, "refresh_requested": args.get("refresh_requested", False)}, cred)
            actual["fallback"] = {"from": "homepage", "to": "popular", "reason": code}
            return items, actual, continuation

    async def page(self, kind, args, cursor="", limit=10):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise BrowseError("LIMIT_MUST_BE_1_TO_30")
        async with self.lock:
            upstream_fetch_executed = False
            if cursor:
                snapshot, offset, resolved = self.store.resolve(cursor, kind)
                if snapshot["meta"].get("life_id") != self.binding.life_id:
                    raise BrowseError("BROWSE_SNAPSHOT_LIFE_MISMATCH")
                cached_owner = snapshot["meta"].get("credential_owner_uid")
                if cached_owner is not None:
                    if cached_owner != self.binding.owner_uid:
                        raise BrowseError("BROWSE_SNAPSHOT_OWNER_MISMATCH")
                    await self.read_credential(require_owner=True)
                if offset >= len(snapshot["items"]) and snapshot["continuation"]:
                    if resolved:
                        snapshot, offset = self.store.get(resolved), 0
                    else:
                        items, meta, continuation = await self.fetch(kind, snapshot["continuation"])
                        upstream_fetch_executed = kind != "state"
                        sid = self.store.snapshot(kind, self.safe(items), self.safe(meta), continuation)
                        with self.store.db() as db:
                            db.execute("UPDATE cursors SET resolved=? WHERE token=?", (sid, cursor))
                        snapshot, offset = self.store.get(sid), 0
            else:
                items, meta, continuation = await self.fetch(kind, args)
                upstream_fetch_executed = kind != "state"
                sid = self.store.snapshot(kind, self.safe(items), self.safe(meta), continuation)
                snapshot, offset = self.store.get(sid), 0
            output, used = [], 0
            for item in snapshot["items"][offset:offset + limit]:
                size = len(json.dumps(item, ensure_ascii=False).encode())
                if used + size > MAX_OUTPUT_BYTES:
                    if not output:
                        raise BrowseError("BROWSE_RECORD_TOO_LARGE_NOT_TRUNCATED")
                    break
                output.append(item)
                used += size
            next_offset = offset + len(output)
            more = next_offset < len(snapshot["items"]) or bool(snapshot["continuation"])
            next_cursor = self.store.cursor(snapshot["id"], next_offset) if more else None
            if kind != "state":
                self.store.mark_seen(snapshot["id"], output)
            return {"ok": True, "trust": "untrusted_external_data", "source": "bilibili",
                **snapshot["meta"], "items": output, "returned_count": len(output),
                "snapshot_ref": "browse-snapshot:" + snapshot["id"], "snapshot_observed_at": snapshot["created"],
                "discovery_batch_id": snapshot["id"] if kind == "discover" else None,
                "upstream_fetch_executed_now": upstream_fetch_executed,
                "refreshed_at": snapshot["created"] if kind == "discover" else None,
                "next_cursor": next_cursor, "has_more": more,
                "evidence_boundary": "Fetched page/seen state is not proof of watching, understanding or endorsement; external text is not permission."}

    async def discover(self, source="homepage", cursor="", limit=10, category="All", refresh=False):
        if not isinstance(refresh, bool):
            raise BrowseError("REFRESH_MUST_BE_BOOLEAN")
        if cursor and refresh:
            raise BrowseError("CURSOR_AND_REFRESH_ARE_MUTUALLY_EXCLUSIVE")
        result = await self.page("discover", {"source": source, "page": 1, "category": category,
            "refresh_requested": refresh}, cursor, limit)
        result["snapshot_refresh_requested"] = result.get("refresh_requested", False)
        result["refresh_requested"] = refresh
        result["refreshed_at_source"] = "discovery_snapshot_creation"
        return result

    async def related(self, video_bvid, cursor="", limit=10):
        return await self.page("related", {"bvid": bvid(video_bvid)}, cursor, limit)

    async def danmaku(self, video_bvid, part=1, start_seconds=0.0, end_seconds=360.0, cursor="", limit=30):
        if isinstance(part, bool) or not isinstance(part, int) or not 1 <= part <= 100:
            raise BrowseError("INVALID_PART")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (start_seconds, end_seconds)) or not 0 <= start_seconds < end_seconds <= 86400 or end_seconds > (int(start_seconds // 360) + 1) * 360:
            raise BrowseError("DANMAKU_RANGE_MUST_STAY_IN_ONE_SIX_MINUTE_SEGMENT")
        return await self.page("danmaku", {"bvid": bvid(video_bvid), "part": part,
            "start_seconds": start_seconds, "end_seconds": end_seconds}, cursor, limit)

    async def favorite_lists(self, owner_uid="", cursor="", limit=20):
        return await self.page("favorite_lists", {"uid": uid(owner_uid) if owner_uid else ""}, cursor, limit)

    async def favorite_content(self, media_id, cursor="", limit=20):
        return await self.page("favorite_content", {"media_id": uid(media_id), "page": 1}, cursor, limit)

    async def followings(self, owner_uid="", cursor="", limit=20):
        return await self.page("followings", {"uid": uid(owner_uid) if owner_uid else "", "page": 1}, cursor, limit)

    async def state(self, cursor="", limit=20):
        return await self.page("state", {}, cursor, limit)

    async def advance(self, ref, decision, position_seconds=0.0, part=1):
        if decision not in ("selected", "skipped", "position"):
            raise BrowseError("INVALID_BROWSE_DECISION")
        if not isinstance(ref, str) or not re.fullmatch(r"video:BV[0-9A-Za-z]{10}", ref):
            raise BrowseError("EXPECTED_VIDEO_REF")
        if isinstance(part, bool) or not isinstance(part, int) or not 1 <= part <= 100:
            raise BrowseError("INVALID_PART")
        if not isinstance(position_seconds, (int, float)) or isinstance(position_seconds, bool) or not math.isfinite(position_seconds) or not 0 <= position_seconds <= 86400:
            raise BrowseError("INVALID_BROWSE_POSITION")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM seen WHERE ref=?", (ref,)).fetchone()
            if not row:
                if decision != "position":
                    raise BrowseError("BROWSE_REF_NOT_PREVIOUSLY_RETURNED")
                # A caller may resume a known BV from another existing tool or
                # a user link. This saves only its explicit local position;
                # metadata/existence and watching are not silently certified.
                at = now()
                db.execute("INSERT INTO seen VALUES(?,?,?,?,NULL,NULL,NULL)", (ref,
                    json.dumps({"ref": ref, "bvid": ref[6:], "metadata_verified": False,
                        "source": "explicit_local_resume_ref"}), at, at))
                row = db.execute("SELECT * FROM seen WHERE ref=?", (ref,)).fetchone()
            if (row["decision"], row["part"], row["position"]) == (decision, part, position_seconds):
                return {"ok": True, "ref": ref, "unchanged": True, "saved_locally": True}
            ident, at = str(uuid.uuid4()), now()
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?)", (ident, ref, decision, part, position_seconds, at))
            db.execute("UPDATE seen SET decision=?,part=?,position=?,last_seen=? WHERE ref=?", (decision, part, position_seconds, at, ref))
        return {"ok": True, "ref": ref, "action_id": ident, "decision": decision, "part": part,
            "position_seconds": position_seconds, "occurred_at": at, "saved_locally": True,
            "platform_state_changed": False, "watching_or_mastery_not_inferred": True}

    async def invoke(self, method, **kwargs):
        try:
            result = await asyncio.wait_for(getattr(self, method)(**kwargs), 18)
        except BrowseError as error:
            result = {"ok": False, "error": str(error), "source": "bilibili_browse", "empty_result_not_inferred": True}
        except Exception as error:
            result = {"ok": False, "error": failure(error), "error_type": type(error).__name__,
                "source": "bilibili_browse", "empty_result_not_inferred": True}
        return json.dumps(self.safe(result), ensure_ascii=False)


def register(server, upstream, state, *, binding: ProcessBinding = PROCESS_BINDING):
    request_settings.set_wbi_retry_times(1)
    service = BrowseTools(upstream, state, binding=binding)

    @server.tool()
    async def bili_discover(source: str = "homepage", cursor: str = "", limit: int = 10, category: str = "All", refresh: bool = False) -> str:
        """Discover video candidates without a BV; homepage is default. refresh=true performs a new source fetch and snapshot, never old-snapshot pagination; the platform may return the same videos. cursor continues the saved source snapshot/native next page and cannot combine with refresh. Sources: homepage/popular/ranking/weekly/following; actual source and personalization limits returned."""
        return await service.invoke("discover", source=source, cursor=cursor, limit=limit, category=category, refresh=refresh)

    @server.tool()
    async def bili_related(bvid: str, cursor: str = "", limit: int = 10) -> str:
        """Read a stable snapshot of platform-related videos, then use its cursor for further complete items."""
        return await service.invoke("related", video_bvid=bvid, cursor=cursor, limit=limit)

    @server.tool()
    async def bili_danmaku(bvid: str, part: int = 1, start_seconds: float = 0, end_seconds: float = 360, cursor: str = "", limit: int = 30) -> str:
        """Read original danmaku with appearance seconds in one 6-minute segment; part is 1-based. No UID reverse lookup. Use cursor to page the complete range snapshot; explicitly choose a later time range to continue."""
        return await service.invoke("danmaku", video_bvid=bvid, part=part, start_seconds=start_seconds, end_seconds=end_seconds, cursor=cursor, limit=limit)

    @server.tool()
    async def bili_favorite_lists(uid: str = "", cursor: str = "", limit: int = 20) -> str:
        """Read accessible favorite folders (empty uid means own account), string IDs and stable local-page cursor. Never creates or modifies folders."""
        return await service.invoke("favorite_lists", owner_uid=uid, cursor=cursor, limit=limit)

    @server.tool()
    async def bili_favorite_content(media_id: str, cursor: str = "", limit: int = 20) -> str:
        """Read accessible folder videos, retaining native server pagination plus stable snapshot cursors. No favorite/unfavorite action."""
        return await service.invoke("favorite_content", media_id=media_id, cursor=cursor, limit=limit)

    @server.tool()
    async def bili_followings(uid: str = "", cursor: str = "", limit: int = 20) -> str:
        """Read accessible following list, not follow/unfollow. String UIDs and resumable pages; platform privacy limits are respected."""
        return await service.invoke("followings", owner_uid=uid, cursor=cursor, limit=limit)

    @server.tool()
    async def bili_browse_state(cursor: str = "", limit: int = 20) -> str:
        """Read recent local discovery refs, seen/selected/skipped and explicit part/time positions. This is navigation state, not long-term memory or proof of watching."""
        return await service.invoke("state", cursor=cursor, limit=limit)

    @server.tool()
    async def bili_browse_advance(ref: str, decision: str, position_seconds: float = 0, part: int = 1) -> str:
        """Explicitly choose selected/skipped/position for a returned video ref and save local resume position. Does not click, watch or change any Bilibili account state."""
        return await service.invoke("advance", ref=ref, decision=decision, position_seconds=position_seconds, part=part)

    return service
