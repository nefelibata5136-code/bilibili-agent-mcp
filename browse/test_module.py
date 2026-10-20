"""Offline navigation invariants; no real account, network, model or credentials."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browse.module import BrowseTools, TOOLS, register, BrowseError
from binding import ProcessBinding

BINDING = ProcessBinding("test-agent", "123")


def item(number=1):
    return {"bvid": "BV1xx411c7m" + str(number), "title": "视频" + str(number),
            "owner": {"mid": 9007199254740993123, "name": "作者"}, "duration": 60}


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cred = SimpleNamespace(sessdata="fixture-secret-not-real", bili_jct="", buvid3="", buvid4="")
        self.video = SimpleNamespace(get_related=AsyncMock(return_value=[item(2), item(3)]),
            get_danmakus=AsyncMock(return_value=[]))
        self.user = SimpleNamespace(get_followings=AsyncMock(return_value={"list": [], "total": 0}))
        self.upstream = SimpleNamespace(get_cred=lambda: self.cred,
            video=SimpleNamespace(Video=lambda **kwargs: self.video),
            user=SimpleNamespace(get_self_info=AsyncMock(return_value={"mid": 123}), User=lambda **kwargs: self.user),
            bili_favorite_lists=AsyncMock(return_value=json.dumps({"lists": [{"id": 9007199254740993123, "title": "收藏夹"}]})),
            bili_favorite_content=AsyncMock(return_value=json.dumps({"medias": [item(1)], "has_more": False})))
        self.service = BrowseTools(self.upstream, self.temp.name, binding=BINDING)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def invoke(self, name, **kwargs):
        return json.loads(await self.service.invoke(name, **kwargs))

    async def test_homepage_excludes_nonvideo_ads_and_preserves_precise_uid(self):
        with patch("browse.module.homepage.get_videos", AsyncMock(return_value={"item": [item(1), {"title": "广告"}, item(1), item(2)]})):
            result = await self.invoke("discover", limit=1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["items"][0]["author_uid"], "9007199254740993123")
        self.assertEqual(result["actual_source"], "homepage")
        self.assertNotEqual(result["personalization"], "personalized")
        self.assertTrue(result["next_cursor"])

    async def test_cursor_survives_process_and_does_not_refetch_snapshot(self):
        remote = AsyncMock(return_value={"item": [item(1), item(2), item(3)]})
        with patch("browse.module.homepage.get_videos", remote):
            first = await self.invoke("discover", limit=1)
            restarted = BrowseTools(self.upstream, self.temp.name, binding=BINDING)
            second = json.loads(await restarted.invoke("discover", cursor=first["next_cursor"], limit=1))
            repeated = json.loads(await restarted.invoke("discover", cursor=first["next_cursor"], limit=1))
        self.assertEqual(remote.await_count, 1)
        self.assertEqual(second["items"], repeated["items"])
        self.assertEqual(second["items"][0]["bvid"], item(2)["bvid"])
        with self.service.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM seen_pages").fetchone()[0], 2)

    async def test_native_next_page_cursor_replay_fetches_once(self):
        remote = AsyncMock(side_effect=[{"list": [item(1)], "no_more": False}, {"list": [item(2)], "no_more": True}])
        with patch("browse.module.hot.get_hot_videos", remote):
            first = await self.invoke("discover", source="popular")
            second = await self.invoke("discover", cursor=first["next_cursor"])
            repeated = await self.invoke("discover", cursor=first["next_cursor"])
        self.assertEqual(remote.await_count, 2)
        self.assertEqual(second["items"], repeated["items"])
        self.assertEqual(second["actual_source"], "popular")
        self.assertEqual(second["native_page"], 2)
        self.assertIsNone(second["next_cursor"])

    async def test_source_fallback_is_explicit(self):
        with patch("browse.module.homepage.get_videos", AsyncMock(side_effect=OSError())), patch("browse.module.hot.get_hot_videos", AsyncMock(return_value={"list": [item(1)], "no_more": True})):
            result = await self.invoke("discover")
        self.assertEqual(result["requested_source"], "homepage")
        self.assertEqual(result["actual_source"], "popular")
        self.assertEqual(result["fallback"]["from"], "homepage")

    async def test_platform_risk_stops_without_fallback(self):
        error = RuntimeError()
        error.code = -412
        fallback = AsyncMock()
        with patch("browse.module.homepage.get_videos", AsyncMock(side_effect=error)), patch("browse.module.hot.get_hot_videos", fallback):
            result = await self.invoke("discover")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "BILI_PLATFORM_RISK_CONTROL")
        fallback.assert_not_awaited()

    async def test_wrong_cursor_kind_is_rejected(self):
        with patch("browse.module.homepage.get_videos", AsyncMock(return_value={"item": [item(1), item(2)]})):
            first = await self.invoke("discover", limit=1)
        result = await self.invoke("related", video_bvid=item(1)["bvid"], cursor=first["next_cursor"])
        self.assertEqual(result["error"], "BROWSE_CURSOR_KIND_MISMATCH")
        self.video.get_related.assert_not_awaited()

    async def test_refresh_performs_second_request_even_if_same_videos(self):
        remote = AsyncMock(return_value={"item": [item(1), item(2)]})
        with patch("browse.module.homepage.get_videos", remote):
            first = await self.invoke("discover", limit=1)
            refreshed = await self.invoke("discover", refresh=True, limit=1)
            paged = await self.invoke("discover", cursor=first["next_cursor"], limit=1)
        self.assertEqual(remote.await_count, 2)
        self.assertNotEqual(first["discovery_batch_id"], refreshed["discovery_batch_id"])
        self.assertEqual(first["items"], refreshed["items"])
        self.assertTrue(refreshed["upstream_fetch_executed_now"])
        self.assertTrue(refreshed["refresh_requested"])
        self.assertFalse(paged["upstream_fetch_executed_now"])

    async def test_refreshed_snapshot_cursor_reports_only_current_request_refresh(self):
        remote = AsyncMock(return_value={"item": [item(1), item(2)]})
        with patch("browse.module.homepage.get_videos", remote):
            refreshed = await self.invoke("discover", refresh=True, limit=1)
            paged = await self.invoke("discover", cursor=refreshed["next_cursor"], limit=1)
        self.assertEqual(remote.await_count, 1)
        self.assertFalse(paged["refresh_requested"])
        self.assertFalse(paged["upstream_fetch_executed_now"])
        self.assertTrue(paged["snapshot_refresh_requested"])
        self.assertEqual(paged["refreshed_at"], refreshed["refreshed_at"])
        self.assertEqual(paged["refreshed_at_source"], "discovery_snapshot_creation")

    async def test_refresh_and_cursor_conflict_without_request(self):
        remote = AsyncMock()
        with patch("browse.module.homepage.get_videos", remote):
            result = await self.invoke("discover", cursor="abc", refresh=True)
        self.assertEqual(result["error"], "CURSOR_AND_REFRESH_ARE_MUTUALLY_EXCLUSIVE")
        remote.assert_not_awaited()

    async def test_secret_echo_is_redacted_before_local_snapshot(self):
        raw = item(1)
        raw["title"] = self.cred.sessdata
        with patch("browse.module.homepage.get_videos", AsyncMock(return_value={"item": [raw]})):
            result = await self.invoke("discover")
        self.assertEqual(result["items"][0]["title"], "[redacted]")
        with self.service.store.db() as db:
            stored = db.execute("SELECT items FROM snapshots").fetchone()[0]
        self.assertNotIn(self.cred.sessdata, stored)

    async def test_following_feed_preserves_native_offset_and_source(self):
        remote = AsyncMock(return_value={"items": [{"modules": {"module_author": {"mid": 9, "name": "UP"},
            "module_dynamic": {"major": {"archive": item(1)}}}}], "offset": "9007199254740993123", "has_more": True})
        with patch("browse.module.dynamic.get_dynamic_page_info", remote):
            result = await self.invoke("discover", source="following")
        self.assertEqual(result["native_offset"], "9007199254740993123")
        self.assertEqual(result["scope"], "following_video_feed")
        self.assertTrue(result["next_cursor"])

    async def test_related_can_navigate_to_another_bv(self):
        result = await self.invoke("related", video_bvid=item(1)["bvid"], limit=1)
        self.assertEqual(result["items"][0]["bvid"], item(2)["bvid"])
        self.assertEqual(result["bvid"], item(1)["bvid"])

    async def test_danmaku_preserves_time_full_text_and_string_id(self):
        self.video.get_danmakus.return_value = [SimpleNamespace(dm_time=13.25, text="全文\n第二行", send_time=1700000000,
            id_str="9007199254740993123", mode=1), SimpleNamespace(dm_time=28.0, text="不在范围", send_time=1700000001, id_str="2", mode=1)]
        result = await self.invoke("danmaku", video_bvid=item(1)["bvid"], start_seconds=10, end_seconds=20)
        dm = result["items"][0]
        self.assertEqual(dm["appears_at_seconds"], 13.25)
        self.assertEqual(dm["text"], "全文\n第二行")
        self.assertEqual(dm["danmaku_id"], "9007199254740993123")
        self.assertIsNone(dm["author_uid"])
        self.video.get_danmakus.assert_awaited_once_with(page_index=0, from_seg=0, to_seg=0)

    async def test_cross_segment_or_invalid_range_never_requests(self):
        result = await self.invoke("danmaku", video_bvid=item(1)["bvid"], start_seconds=350, end_seconds=370)
        self.assertFalse(result["ok"])
        self.video.get_danmakus.assert_not_awaited()

    async def test_danmaku_filters_empty_system_objects_and_deduplicates_ids(self):
        from bilibili_api.utils.danmaku import Danmaku
        self.video.get_danmakus.return_value = [Danmaku(""), Danmaku(" \n\t", id_str="lb.c"),
            Danmaku("可读原文", dm_time=1.25, send_time=1700000000, id_=9007199254740993123),
            Danmaku("重复同 ID", dm_time=1.25, send_time=1700000000, id_str="9007199254740993123")]
        result = await self.invoke("danmaku", video_bvid=item(1)["bvid"], end_seconds=30)
        self.assertTrue(result["ok"])
        self.assertEqual([dm["text"] for dm in result["items"]], ["可读原文"])
        self.assertEqual(result["items"][0]["danmaku_id"], "9007199254740993123")
        self.assertEqual(result["filtering"], {"empty_text_removed": 2, "duplicate_ids_removed": 1})

    async def test_danmaku_missing_zero_ids_have_distinct_stable_content_hashes(self):
        from bilibili_api.utils.danmaku import Danmaku
        self.video.get_danmakus.return_value = [
            Danmaku("甲", dm_time=1.25, send_time=1700000000, id_=0, id_str="0"),
            Danmaku("乙", dm_time=1.25, send_time=1700000000, id_=0, id_str="0"),
            Danmaku("甲", dm_time=1.25, send_time=1700000000),
            Danmaku("丙", dm_time=2.5, send_time=1700000001, id_=123, id_str="0")]
        first = await self.invoke("danmaku", video_bvid=item(1)["bvid"], end_seconds=30)
        second = await self.invoke("danmaku", video_bvid=item(1)["bvid"], end_seconds=30)
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(len(first["items"]), 3)
        self.assertEqual(len({dm["danmaku_id"] for dm in first["items"]}), 3)
        self.assertTrue(all(dm["danmaku_id"] != "0" for dm in first["items"]))
        self.assertEqual(first["items"][-1]["danmaku_id"], "123")
        self.assertEqual(first["filtering"]["duplicate_ids_removed"], 1)

    async def test_explicit_state_is_local_and_idempotent(self):
        await self.invoke("related", video_bvid=item(1)["bvid"])
        first = await self.invoke("advance", ref="video:" + item(2)["bvid"], decision="position", position_seconds=12.5, part=2)
        second = await self.invoke("advance", ref="video:" + item(2)["bvid"], decision="position", position_seconds=12.5, part=2)
        self.assertFalse(first["platform_state_changed"])
        self.assertTrue(second["unchanged"])
        state = await self.invoke("state")
        self.assertEqual(state["items"][0]["position"], 12.5)
        with self.service.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)

    async def test_unknown_video_ref_cannot_claim_selected(self):
        result = await self.invoke("advance", ref="video:" + item(1)["bvid"], decision="selected")
        self.assertEqual(result["error"], "BROWSE_REF_NOT_PREVIOUSLY_RETURNED")

    async def test_explicit_bv_resume_records_position_without_claiming_metadata(self):
        result = await self.invoke("advance", ref="video:" + item(1)["bvid"], decision="position", position_seconds=9)
        self.assertTrue(result["ok"])
        state = await self.invoke("state")
        self.assertFalse(state["items"][0]["item"]["metadata_verified"])
        self.assertEqual(state["items"][0]["position"], 9)

    async def test_state_cursor_reads_without_marking_more_discovery_seen(self):
        await self.invoke("related", video_bvid=item(1)["bvid"])
        first = await self.invoke("state", limit=1)
        second = await self.invoke("state", cursor=first["next_cursor"], limit=1)
        self.assertNotEqual(first["items"][0]["ref"], second["items"][0]["ref"])
        with self.service.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM seen_pages").fetchone()[0], 2)

    async def test_favorites_reuse_vendor_and_stringify_ids(self):
        result = await self.invoke("favorite_lists", owner_uid="9007199254740993123")
        self.upstream.bili_favorite_lists.assert_awaited_once_with(uid=9007199254740993123, credential=self.cred)
        self.assertEqual(result["items"][0]["id"], "9007199254740993123")
        self.assertFalse(result["native_pagination"])

    async def test_favorite_content_native_pagination_and_accessible_scope(self):
        result = await self.invoke("favorite_content", media_id="42")
        self.upstream.bili_favorite_content.assert_awaited_once_with(media_id=42, page=1, credential=self.cred)
        self.assertEqual(result["media_id"], "42")
        self.assertIn("accessible", result["scope"])

    async def test_favorite_lists_upstream_error_or_nonobject_is_failure_not_empty(self):
        for payload in ({"error": "private upstream detail " + self.cred.sessdata}, []):
            with self.subTest(payload_type=type(payload).__name__):
                self.upstream.bili_favorite_lists.return_value = json.dumps(payload)
                result = await self.invoke("favorite_lists", owner_uid="123")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "BILI_FAVORITE_LISTS_UPSTREAM_FAILURE")
                self.assertNotIn("items", result)
                self.assertTrue(result["empty_result_not_inferred"])
                self.assertNotIn("private upstream detail", json.dumps(result))
                self.assertNotIn(self.cred.sessdata, json.dumps(result))
        with self.service.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    async def test_favorite_content_upstream_error_or_nonobject_is_failure_not_empty(self):
        for payload in ({"error": "private upstream detail " + self.cred.sessdata}, None):
            with self.subTest(payload_type=type(payload).__name__):
                self.upstream.bili_favorite_content.return_value = json.dumps(payload)
                result = await self.invoke("favorite_content", media_id="42")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "BILI_FAVORITE_CONTENT_UPSTREAM_FAILURE")
                self.assertNotIn("items", result)
                self.assertTrue(result["empty_result_not_inferred"])
                self.assertNotIn("private upstream detail", json.dumps(result))
                self.assertNotIn(self.cred.sessdata, json.dumps(result))
        with self.service.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    async def test_only_declared_tools_registered(self):
        class Server:
            names = []
            def tool(self):
                def decorator(fn): self.names.append(fn.__name__); return fn
                return decorator
        server = Server()
        register(server, self.upstream, self.temp.name, binding=BINDING)
        self.assertEqual(set(server.names), set(TOOLS))
        self.assertFalse(any(name in server.names for name in ("bili_reply", "bili_follow", "bili_like", "bili_favorite")))

    async def test_unconfigured_owner_reads_public_homepage_and_folders_anonymously(self):
        service = BrowseTools(self.upstream, self.temp.name, binding=ProcessBinding("anonymous-test"))
        remote = AsyncMock(return_value={"item": [item(1)]})
        with patch("browse.module.homepage.get_videos", remote):
            result = json.loads(await service.invoke("discover"))
        self.assertEqual(result["personalization"], "anonymous_homepage")
        self.assertIsNone(result["credential_owner_uid"])
        self.assertFalse(remote.await_args.kwargs["credential"].sessdata)
        await service.invoke("favorite_lists", owner_uid="456")
        await service.invoke("favorite_content", media_id="42")
        self.assertFalse(self.upstream.bili_favorite_lists.await_args.kwargs["credential"].sessdata)
        self.assertFalse(self.upstream.bili_favorite_content.await_args.kwargs["credential"].sessdata)
        self.upstream.user.get_self_info.assert_not_awaited()

    async def test_unconfigured_own_account_operations_fail_before_platform_reads(self):
        service = BrowseTools(self.upstream, self.temp.name, binding=ProcessBinding("anonymous-test"))
        remote = AsyncMock()
        with patch("browse.module.dynamic.get_dynamic_page_info", remote):
            for method, args in (("favorite_lists", {}), ("followings", {}),
                    ("discover", {"source": "following"})):
                result = json.loads(await service.invoke(method, **args))
                self.assertEqual(result["error"], "BILI_OWNER_NOT_CONFIGURED")
        self.upstream.user.get_self_info.assert_not_awaited()
        self.upstream.bili_favorite_lists.assert_not_awaited()
        self.user.get_followings.assert_not_awaited()
        remote.assert_not_awaited()

    async def test_configured_owner_mismatch_blocks_credentialed_reads(self):
        self.upstream.user.get_self_info.return_value = {"mid": 456}
        home, following = AsyncMock(), AsyncMock()
        with patch("browse.module.homepage.get_videos", home), patch("browse.module.dynamic.get_dynamic_page_info", following):
            for method, args in (("discover", {}), ("discover", {"source": "following"}),
                    ("favorite_lists", {}), ("favorite_lists", {"owner_uid": "789"}),
                    ("favorite_content", {"media_id": "42"}), ("followings", {})):
                result = await self.invoke(method, **args)
                self.assertEqual(result["error"], "BILI_ACCOUNT_OWNER_MISMATCH")
        home.assert_not_awaited()
        following.assert_not_awaited()
        self.upstream.bili_favorite_lists.assert_not_awaited()
        self.upstream.bili_favorite_content.assert_not_awaited()
        self.user.get_followings.assert_not_awaited()

    async def test_cached_personal_cursor_rechecks_account_and_fixed_binding(self):
        with patch("browse.module.homepage.get_videos", AsyncMock(return_value={"item": [item(1), item(2)]})):
            first = await self.invoke("discover", limit=1)
        self.upstream.user.get_self_info.return_value = {"mid": 456}
        changed = await self.invoke("discover", cursor=first["next_cursor"])
        self.assertEqual(changed["error"], "BILI_ACCOUNT_OWNER_MISMATCH")
        for binding, code in ((ProcessBinding("test-agent"), "BROWSE_SNAPSHOT_OWNER_MISMATCH"),
                (ProcessBinding("another-agent", "123"), "BROWSE_SNAPSHOT_LIFE_MISMATCH")):
            other = BrowseTools(self.upstream, self.temp.name, binding=binding)
            result = json.loads(await other.invoke("discover", cursor=first["next_cursor"]))
            self.assertEqual(result["error"], code)

    async def test_configured_account_without_login_uses_anonymous_public_reads(self):
        self.cred.sessdata = ""
        remote = AsyncMock(return_value={"item": [item(1)]})
        with patch("browse.module.homepage.get_videos", remote):
            result = await self.invoke("discover")
        self.assertEqual(result["personalization"], "anonymous_homepage")
        self.assertFalse(remote.await_args.kwargs["credential"].sessdata)
        result = await self.invoke("favorite_lists")
        self.assertEqual(result["error"], "BILI_AUTH_OR_PERMISSION_REQUIRED")
        self.upstream.user.get_self_info.assert_not_awaited()

    async def test_sitewide_popular_and_local_state_do_not_authenticate_account(self):
        self.upstream.user.get_self_info.side_effect = AssertionError("No account HTTP for public popular/local state")
        with patch("browse.module.hot.get_hot_videos", AsyncMock(return_value={"list": [item(1)], "no_more": True})):
            result = await self.invoke("discover", source="popular")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["credential_owner_uid"])
        self.assertTrue((await self.invoke("state"))["ok"])
        self.upstream.user.get_self_info.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
