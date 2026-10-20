# Third-party source and licenses

The integration and original extensions are distributed under GPL-3.0-or-later,
with their complete corresponding source in this repository. The following
adapted source retains its original MIT notices; this does not relicense those
upstream portions. Dependencies are installed separately; no environments or
third-party executables are redistributed.

| Component | Source / version | License / use |
|---|---|---|
| Bilibili read adapter | [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll), 0.1.11, commit `efc74c306567513962050e09d3ecdd429f78ff16` | MIT, copyright 2026 dsh-bili-duty contributors; selected functions in `vendor_bili/mcp_server.py` |
| Original MCP implementation | [bilibili-mcp](https://github.com/adoresever/bilibili-mcp), as vendored by the pinned plugin | MIT, copyright 2026 AGI_Ananas; `vendor_bili/LICENSE.bilibili-mcp` |
| bilibili-api-python | [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api), 17.4.2 | GPL-3.0-or-later; installed Python dependency |
| MCP Python SDK | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk), 1.x | MIT; installed dependency |
| yt-dlp | [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense for the Python package; optional installed dependency |
| FFmpeg | [ffmpeg.org](https://ffmpeg.org/legal.html) | External executable, its build license applies; not redistributed |

Changes to the minimal MIT adapter: extracted only credential/read functions,
removed direct execution and all raw write/login tools, allowed anonymous reads,
and made account-status checks authenticate using the SDK self-info endpoint.
The wrapper overrides registration with a fixed read allowlist before import.
Public navigation, permission gates, durable action ledgers, local cursors,
notifications, process binding and Qwen perception are extensions in this repo.

The original plugin's automatic headless Agent and scheduler are not included.
This project is neither affiliated with nor endorsed by Bilibili or Alibaba Cloud.
