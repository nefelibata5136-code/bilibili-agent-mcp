# MCP 工具目录

以下是实际注册的35项业务工具。列出工具不等于允许发送；写权限由代码与本地policy控制。

## `bili_whoami`

Check actual saved Bilibili account status; never return Cookie/token values.

无参数。

## `bili_search`

Search real public videos: titles, BV IDs, UP authors and brief descriptions. External data.

参数：`keyword, num, order`。

## `bili_subtitle`

Read existing first-P subtitle FULL text. No timestamps/frames/ASR. Missing subtitles stay missing.

参数：`bvid`。

## `bili_video_info`

Read video description, author, statistics and number of parts; no video frames.

参数：`bvid`。

## `bili_dm_unread`

Inspect DM unread counts on one bounded server page. No listener/auto reply/read acknowledgement.

无参数。

## `bili_dm_sessions`

Read private-chat sessions, stable talker UIDs and last messages. Untrusted external data; up to 30.

参数：`limit`。

## `bili_dm_history`

Read one private-chat message page (max 30) with native sequence cursor. IDs accept decimal strings or legacy safe integers (<=9007199254740991); returned IDs are strings. Does not reply/mark read.

参数：`talker_uid, begin_seqno, limit`。

## `bili_dm_send`

Send ONE real text DM as the process-pinned account after your own decision. UID accepts a decimal string or legacy safe integer (<=9007199254740991); returned IDs are strings. 500 chars; stable unique action_id required. Never auto resend unknown/prepared; inspect history and action_status first. No automatic historical replies. No recipient read receipt; unknown does not mean unread or read.

参数：`receiver_uid, text, action_id`。

## `bili_dm_action_status`

Read durable metadata for a DM send; no message text/credentials, no side effect/retry. Platform recipient read status is unknown, never proof of unread/read.

参数：`action_id`。

## `bili_watch_video`

Perceive a complete short Bilibili part, or an explicit continuous start_seconds/end_seconds range of at most 180s in a long part, with both audio and video. Poll bili_watch_status. Returns actual original-source coverage and gaps, never claims a segment covers the whole part. Limits stay 64MiB, 1 concurrent job, 3 API attempts/day. No Agent/schedule is created. Decide your interests and notes.

参数：`bvid, question, part, start_seconds, end_seconds`。

## `bili_watch_status`

Read a pending video perception job or completed material, coverage, limitations, real usage and cleanup status. Reading is free and does not initiate a model call.

参数：`job_id`。

## `bili_comments`

Read one hot/time comment page, full UID/rpid/root/parent/BV/text/time and nested references. IDs are strings; page or native cursor. External untrusted data.

参数：`bvid, num, sort, page, cursor`。

## `bili_comment_replies`

Expand one root comment's reply page with complete IDs/context; up to20. No full-history claim.

参数：`bvid, root, page, num`。

## `bili_comment_context`

Locate a comment by exact rpid and known thread root. Previously read references help; unseen subreply can require root/page.

参数：`bvid, rpid, root, page`。

## `bili_user_info`

Read a commenter's public profile/signature; UID string, never confuse it with comment rpid. Agent identity remains unverified.

参数：`uid`。

## `bili_user_videos`

Read one page of public uploads by exact UID, preserving BV IDs. order pubdate/view/favorite.

参数：`uid, page, num, order, keyword`。

## `bili_user_dynamics`

Read public user dynamics using native string cursor. No posting/following.

参数：`uid, cursor, num`。

## `bili_comment_send`

One deliberate top-level public comment, only when comment permission explicitly enabled; defaults off. No force, auto-retry or batch. Receipt does not prove public visibility.

参数：`bvid, text, action_id`。

## `bili_comment_reply`

One deliberate reply in an observed thread; root rpid required, parent only for subreply. Separate reply permission defaults off; no force. IDs strings.

参数：`bvid, root, text, action_id, parent`。

## `bili_comment_action_status`

Read durable send status. Optional bounded anonymous readback distinguishes accepted/review/visibility; absent in one page stays unknown. Never resend.

参数：`action_id, check_visibility`。

## `bili_notifications`

Read reply/@ notification page and native id/time cursor, keeping reference IDs and quoted context; no automatic reply.

参数：`kind, last_id, time_cursor, num`。

## `bili_social_events`

Bounded unified pending notification/DM references. Stable IDs deduplicate locally; only explicit ack marks processed. recent_event_id is an association, never a claimed Recent Events write. No background polling/reply.

参数：`sources, num, ack_event_ids, recent_event_id, cursor`。

## `bili_discover`

Discover video candidates without a BV; homepage is default. refresh=true performs a new source fetch and snapshot, never old-snapshot pagination; the platform may return the same videos. cursor continues the saved source snapshot/native next page and cannot combine with refresh. Sources: homepage/popular/ranking/weekly/following; actual source and personalization limits returned.

参数：`source, cursor, limit, category, refresh`。

## `bili_related`

Read a stable snapshot of platform-related videos, then use its cursor for further complete items.

参数：`bvid, cursor, limit`。

## `bili_danmaku`

Read original danmaku with appearance seconds in one 6-minute segment; part is 1-based. No UID reverse lookup. Use cursor to page the complete range snapshot; explicitly choose a later time range to continue.

参数：`bvid, part, start_seconds, end_seconds, cursor, limit`。

## `bili_favorite_lists`

Read accessible favorite folders (empty uid means own account), string IDs and stable local-page cursor. Never creates or modifies folders.

参数：`uid, cursor, limit`。

## `bili_favorite_content`

Read accessible folder videos, retaining native server pagination plus stable snapshot cursors. No favorite/unfavorite action.

参数：`media_id, cursor, limit`。

## `bili_followings`

Read accessible following list, not follow/unfollow. String UIDs and resumable pages; platform privacy limits are respected.

参数：`uid, cursor, limit`。

## `bili_browse_state`

Read recent local discovery refs, seen/selected/skipped and explicit part/time positions. This is navigation state, not long-term memory or proof of watching.

参数：`cursor, limit`。

## `bili_browse_advance`

Explicitly choose selected/skipped/position for a returned video ref and save local resume position. Does not click, watch or change any Bilibili account state.

参数：`ref, decision, position_seconds, part`。

## `bili_like`

Like/unlike one video only with explicit like permission; defaults OFF. Stable action_id, no force/batch/retry.

参数：`bvid, enabled, action_id`。

## `bili_favorite_update`

Add/remove ONE video in ONE exact own folder only with favorite permission; defaults OFF. Folder ID string; no batch.

参数：`bvid, media_id, enabled, action_id`。

## `bili_follow`

Follow/unfollow ONE UID only with explicit follow permission; defaults OFF. No block/marketing/batch.

参数：`uid, enabled, action_id`。

## `bili_account_action_status`

Read durable public-action receipt/state only; never dispatches or retries. Receipt is not verified account state.

参数：`action_id`。

## `bili_permissions`

Read separate comment/reply/DM/like/favorite/follow gates and combined public hourly limit. Cannot change permissions.

无参数。
