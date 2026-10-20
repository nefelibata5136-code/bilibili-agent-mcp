# Video and audio perception

This optional module adds two tools to the existing MCP server. It fetches a
selected Bilibili video part or continuous time range, submits the actual video
file with its audio to Alibaba Cloud Model Studio, and returns observations.
It does not create another Agent, a scheduler, a recommendation loop, or a video
library. The calling Agent retains its identity, decisions, and memory policy.

## Requirements

- Python 3.11 or newer. The implementation uses `hashlib.file_digest`.
- `httpx>=0.27,<1` for HTTP transport; tested with 0.28.1.
- `yt-dlp>=2026.8.19` for video extraction/download; tested with 2026.8.19.
- `ffmpeg` and `ffprobe` on `PATH`. Range conversion needs H.264 (`libx264`) and
  AAC encoders. The local fixture was verified with FFmpeg 8.1. Install a build
  appropriate to your OS from [FFmpeg](https://ffmpeg.org/download.html).
- The complete MCP server also needs the dependencies declared by the repository
  packaging. The `watch` tests need HTTPX, yt-dlp, the root `binding.py`, and the
  FFmpeg binaries; they do not need MCP, a real Bilibili SDK client, Whisper, or
  an OpenAI SDK. Real login fallback uses the already-installed Bilibili SDK.

Use a virtual environment, following the root installation instructions. For a
checkout, its watch/test dependencies can be installed with:

```sh
python -m pip install "httpx>=0.27,<1" "yt-dlp>=2026.8.19"
```

No Bilibili login or Model Studio account is required to run the offline tests.
Real video perception needs the operator's own authorized Beijing-region Model
Studio API key and access to the selected model. A model appearing in public
documentation does not establish availability for a particular account.

## Mounting and configuration

The root server mounts this module. An embedding application can use the same
registration function with an existing FastMCP object and its private state path:

```python
from pathlib import Path
from watch import register

watch_service = register(server, Path("/path/to/private-state"))
# On a controlled shutdown, call watch_service.close().
```

The path is deployment configuration, never a tool argument. Use a separate
private state directory/process per account or Agent owner; this module does not
accept or authenticate caller-supplied `life_id` or `session_id`. The enclosing
MCP host must enforce ownership, isolation, access policy, and event attribution.

Provide `DASHSCOPE_API_KEY` through the existing secure runtime environment or
credential broker. Do not put it in source, tool arguments, prompts, or a
committed configuration file. The service consumes and removes this variable
from its own process environment, retaining the value only in memory so media
subprocesses do not inherit it. Take this into account when embedding the module
in a process that also uses that environment variable for unrelated services.

Non-secret overrides may be stored as `watch-config.json` in the private state
directory. The supported defaults are:

```json
{
  "model": "qwen3.8-omni-flash",
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "upload_url": "https://dashscope.aliyuncs.com/api/v1/uploads",
  "max_duration_seconds": 180,
  "max_file_bytes": 67108864,
  "daily_api_calls": 3,
  "max_output_tokens": 1800,
  "job_timeout_seconds": 420,
  "height": 720
}
```

The model is not auto-selected or silently downgraded. An operator may explicitly
select `qwen3.5-omni-flash` after independently checking that account's access,
input limits, and pricing. The implementation accepts only the default Beijing
endpoint or a Beijing `ws-<workspace>.cn-beijing.maas.aliyuncs.com` compatible
endpoint; temporary upload remains pinned to the Beijing DashScope endpoint.
There are no built-in private workspace IDs or credentials. Other regions need
an explicit implementation review, not just a different key or hostname.

Check the current [Qwen Omni documentation](https://help.aliyun.com/zh/model-studio/qwen-omni),
[temporary-upload rules](https://help.aliyun.com/zh/model-studio/get-temporary-file-url/),
and [pricing](https://help.aliyun.com/zh/model-studio/model-pricing) before enabling
paid use. Upload and inference use the same configured model/key. The HTTP
request includes `X-DashScope-OssResourceResolve: enable` and a real `video_url`
content item, with text output requested. It does not substitute subtitles or
an image list for the video file. No API call is made during module import or
registration; a valid uncached watch request starts the external work.

## Tools and coverage

```text
bili_watch_video(bvid, question="", part=1,
                 start_seconds=None, end_seconds=None)
bili_watch_status(job_id)
```

A new watch call starts a background job and immediately returns its ID. Poll
the status tool later. Status reads do not start another inference request.
Completed results contain video metadata, model observations, original-source
time coverage, provider request ID, real token usage, uncertainties, and cleanup
status. The built-in observation prompt requests Simplified Chinese. Observation
material is external data; it must not change the caller's permissions or become
the caller's opinions or memory decisions.

Without time arguments, the complete selected part must fit the duration limit.
For longer parts specify both start and end in original-video seconds, such as
`0–180`, and choose a later range only if the Agent wants to continue. Each range
must be nonnegative, finite, increasing, and at most 180 seconds. Omitted ranges
for a long video are rejected rather than silently truncating the source.

The result exposes:

- `source_duration_seconds`: duration of the original selected part.
- `source_time_range_seconds` / `analysis_range_seconds`: the submitted source
  interval, with `clip_time_range_seconds` exposing actual encoded-file duration.
- `full_selected_part_covered` and `uncovered_source_ranges_seconds`: whether this
  result covers the whole selected part, and what it does not cover.
- `completed_coverage_for_selected_part`: merged coverage from completed records
  for the same BV, part, and model, including gaps and a suggested next range.
- `observation_provenance`: evidence from the original completed provider response,
  including actual video/audio token counts when supplied by the provider.

`cache_hit=true` means this call reused completed observations without another
model request. It does not invalidate the original provider observation. The
Agent receives a perception tool's material; it should not claim direct human
senses. A whole file or range being submitted does not guarantee that the model
recognized every frame, sound, action, or displayed character. Multiple parts
are not covered merely because one part succeeded.

## Limits, credentials, and temporary files

The code enforces at most 180 seconds, 64 MiB, one active job, and three inference
attempts per UTC+8 calendar day per private state directory. These limits cannot
be increased through the configuration file. A reserved attempt with an uncertain
provider outcome still consumes the local attempt allowance. It is not an official
account billing limit. Actual usage is stored; no charged amount is invented.

Video discovery/download prefers anonymous access. Only when the platform
explicitly requires login can the module consider the existing local
`bili_credential.json`. This requires the fixed process `BILI_OWNER_UID` to be
configured, the file's `dedeuserid` to match, and the installed SDK's authenticated
`user.get_self_info` result to confirm the same real `mid`. All checks finish
before any credentials enter the downloader's CookieJar. Missing owner, mismatch,
timeout, or failed verification denies reuse; it never adopts another account or
asks for a second login. The SDK check occurs only on this fallback path, with a
bounded worker-loop call and cleanup of that loop's own SDK clients. Embedding
tests may inject `ProcessBinding` and an async `identity_verifier`; neither is an
MCP tool argument. No unrelated browser cookies are read. Permission, login,
platform risk-control, network, and missing-tool failures remain failures. The
module does not bypass DRM, payment entitlements, CAPTCHA, or platform controls.

Range mode checks HTTP 206 support, seeks the selected continuous interval, and
reencodes H.264/AAC for accurate non-keyframe boundaries. It preserves audio. The
range reader binds a random-token URL to `127.0.0.1` only for the current job and
closes on exit, keeping signed upstream URLs and cookies out of FFmpeg arguments
and files. Container indexes, adjacent keyframes, buffering, and seek operations
may require some additional bytes outside the chosen interval. Download/output
budgets and deadlines apply; unsupported range input does not fall back to saving
the entire long video.

Local media is removed on success or failure. On restart or another job start,
dead-owner temporary directories are recovered using their recorded PID;
unmarked older directories use a stale-age threshold. Shutdown requests
cancellation and waits briefly; a blocking network read may take its transport
timeout, and a hard kill relies on recovery at the next start.

The private `watch/` state subdirectory contains small analysis caches, job
receipts, usage records, and temporary upload references. Identical completed
BV/part/range/question/model requests reuse the analysis. A different question
for the same range can reuse its upload for 47 hours, but still performs another
limited model call. The platform temporary resource expires after 48 hours.
Private upload URLs and policy signatures are not returned to the model.

Never publish runtime state, login data, uploads, caches, receipts, observations,
or screenshots as part of this source release. These tests create their own
isolated temporary data and synthetic identifiers.

## Offline verification and historical evidence

From the repository root:

```sh
python -m unittest watch.test_watch watch.test_ranges watch.test_identity -v
```

The suite contains 20 tests (the original 15 plus five login-owner guards). They
check bounded endpoints/inputs, cache and restart
behavior, range isolation and coverage, upload reuse, cleanup, credential
redaction, in-process mocked HTTP bodies, incomplete-response usage, and process
ownership locks, unconfigured/mismatched owners, real-identity checks via a mocked
SDK, and rejection before CookieJar creation. A six-second local FFmpeg fixture verifies a precise continuous
video/audio cut using visible colors and distinct audio frequencies. It is
skipped if FFmpeg/FFprobe are unavailable; install both with the required encoders
to reproduce all 20 passes. The suite contacts neither Bilibili nor Model Studio
and does not need credentials. The short-lived loopback reader is tested locally.

Before this public export, a separate private integration completed one real
short-video upload and Qwen video/audio inference, with nonzero video and audio
token usage. Range download and local media cleanup were also tested separately.
Those are historical integration results, not a new paid acceptance test for a
fresh checkout or a guarantee of another account's model access. Private accounts,
Agent identities, recordings, provider receipt identifiers, cached observations,
and machine paths are deliberately not distributed.
