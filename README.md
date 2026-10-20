# Bilibili Agent MCP

让你现有的 Agent 自己逛 B站：**首页推荐与刷新 → 视频 → 弹幕和评论 → 用户主页 → 投稿和动态 → 接续互动**。

这是提供给已有 Agent 的 MCP 工具服务，不带人格、不另开一个代替你的 Agent，也不要求它每天浏览、喜欢某类内容或自动交朋友。主 Agent 保留身份、决定与记忆；千问只提供视频观察材料。

配套项目：[多数字生命运行系统 / DeepSeek Harness Digital Life](https://github.com/nefelibata5136-code/deepseek-harness-digital-life)。该系统提供按生命隔离的接入；其他 MCP 客户端也可使用本服务。

**状态：Experimental，v0.1.0。** 从实际使用的能力中整理为便携开源版本。公开版使用合成数据、真实 MCP stdio 和本地 FFmpeg fixture 验证；不会把作者的登录态或既往私人验收记录作为你的部署结果。B站接口和模型账号可用性仍须在你自己的环境确认。

## 能做什么

| 场景 | 工具与边界 |
|---|---|
| 打开首页、换一批 | `bili_discover(source="homepage", refresh=true)` 发起新请求；登录且绑定正确时使用该账号，匿名响应不声称是个性化推荐。热门、排行榜、每周必看、关注流保留各自来源标签 |
| 接着浏览 | `bili_related`、分页 cursor、`bili_browse_state` / `bili_browse_advance` 保存选择、跳过和播放位置 |
| 搜索与阅读 | 搜索、视频详情、第一 P 已有字幕全文；字幕没有时间戳，不等于画面理解 |
| 视频理解 | `bili_watch_video` 获取真正的音视频，上传百炼临时存储，调用 Qwen Omni；后台 job/status，支持明确的 ≤180 秒片段和覆盖记录 |
| 评论与用户导航 | 热门/最新评论、分页、楼中楼、rpid 上下文；保留 UID/rpid/root/parent，再访问主页、投稿与动态 |
| 弹幕 | 保留文本和视频时间，按区间和分页读取 |
| 持续互动 | 回复/@通知、私信会话/历史/发送、统一的本地待处理事件与引用；不自动回复 |
| 收藏与关注 | 自己的收藏夹、公开收藏夹与内容、关注列表；点赞/收藏/关注的独立写入入口 |

共 **35 个业务 MCP 工具**，完整目录见 [docs/tools.md](docs/tools.md)。所有平台 ID 输出为十进制字符串，避免 JavaScript 精度损失；私信输入额外兼容安全范围内的旧整数。UID 是用户，rpid/root/parent 是评论位置。

## 安装

需要 Python 3.11+。使用独立虚拟环境，不安装到你的现有 Agent Python 环境中。

```sh
git clone https://github.com/nefelibata5136-code/bilibili-agent-mcp.git
cd bilibili-agent-mcp
python -m venv .venv
# Linux/macOS
.venv/bin/python -m pip install -e '.[watch]'
# Windows PowerShell
.\.venv\Scripts\python.exe -m pip install -e '.[watch]'
```

只需要搜索、评论导航等能力时可安装 `-e .`；完整视频理解额外需要 `[watch]` 和 PATH 中的 `ffmpeg`、`ffprobe`，支持 H.264/AAC。不捆绑 FFmpeg、不下载 Whisper 模型。本项目没有独立的本地 ASR 工具；字幕与完整音视频感知分别提供。

本版锁定 `bilibili-api-python==17.4.2` 和 MCP 1.x。原 SDK 仓库已公告停止维护，依赖/接口兼容性是已知风险；参见 [第三方声明](THIRD_PARTY_NOTICES.md)。遇到登录、权限、版权、验证码或风控失败会返回错误，不提供绕过机制。

## 接入 MCP 客户端

每个 Agent/生命使用**单独的服务进程、账号绑定和 state 目录**。客户端可信配置在启动前设置环境，工具参数不能更改绑定。示例中的可执行文件和目录请替换为你的绝对路径。

```json
{
  "mcpServers": {
    "bilibili": {
      "command": "/absolute/path/bilibili-agent-mcp/.venv/bin/bilibili-agent-mcp",
      "args": ["--state", "/absolute/private/path/bili-state"],
      "env": {
        "BILI_LIFE_ID": "local-agent",
        "BILI_OWNER_UID": ""
      }
    }
  }
}
```

Windows 的 `command` 用 `.venv\\Scripts\\bilibili-agent-mcp.exe`。也可用虚拟环境 Python 运行 `readonly_server.py --state <目录>`。默认使用本仓库内裁剪过的上游只读适配器，不需要安装 DSH 或另一套 B站插件。已有 dsh-bili-scroll 的部署可选传入 `--package <包目录>`。

未配置 `BILI_OWNER_UID` 时只允许匿名公开读取，账号相关入口拒绝使用未绑定的登录态。需要使用账号时，将它设置为**你控制的账号 UID**，并由本人完成以下初始化和扫码。

## 本人登录与权限

在和 MCP 服务相同的环境中设置 `BILI_LIFE_ID`、`BILI_OWNER_UID`；把 state 放在仅本人可访问、源码目录之外的本地目录。PowerShell 示例：

```powershell
$env:BILI_LIFE_ID = 'my-agent'
$env:BILI_OWNER_UID = '替换为你自己的十进制UID'
.\.venv\Scripts\bilibili-agent-config.exe init --state '替换为本地私有绝对目录'
.\.venv\Scripts\bilibili-agent-config.exe login --state '替换为同一个目录'
```

Linux/macOS 使用 `export` 设置同名环境，运行 `.venv/bin/bilibili-agent-config`。登录命令给出本地二维码图片路径，由本人扫码确认；过期后重新运行。登录成功后验证实际账号与绑定一致，再原子保存凭据；不会让你粘贴 Cookie，不读取其他浏览器，也不把登录工具开放给 Agent。`bili_whoami` 可查询真实状态。

`init` 生成以下本地配置，已有配置不会被覆盖：

- `bili_social_policy.json`：`permissions.comment/reply/like/favorite/follow` 全为 `false`，共用低频限额默认 3 次/小时。
- `bili_dm_policy.json`：`send_enabled=false`，私信单独限额默认 3 次/小时。

需要授权时，由账号控制者在本地只启用对应字段。policy 的 `life_id` / `owner_uid` 必须和进程绑定一致。没有任意 `force` 后门；登录成功也不会自动打开写权限。每次发送需要稳定的 `action_id`，有持久化去重与状态账本；`unknown`/`prepared` 不能当失败随意重发。接口接受、平台审核、公开可见、对方已读分别处理，不能混为成功。

凭据仅在本地 state 中。POSIX 创建私有目录/文件权限；Windows 继承父目录 ACL，需要你选择只有本人可访问的目录，不声称代码已额外设置 Windows ACL。不要将 state、`.env`、缓存或数据库提交到 GitHub。

## 视频理解

将**北京地域**百炼 Key 通过宿主的安全配置注入 `DASHSCOPE_API_KEY`。没有 Key 时其他功能仍可用，视频理解明确返回未配置。不会使用 Gemini/OpenAI 海外模型；兼容接口的协议名称不代表调用 OpenAI。

默认模型 `qwen3.8-omni-flash`，只有显式配置才允许 `qwen3.5-omni-flash`。部署者须确认自己账号的模型可用性、定价和权限；不会自动降级或编造费用。调用返回实际 Provider usage。

```text
bili_watch_video(bvid="真实BV号", question="画面中的人物做了什么？")
# 返回 job_id 后用 bili_watch_status(job_id=...) 接续
# 长视频只看一个指定片段：start_seconds=180, end_seconds=360
```

单次最多 180 秒、64 MiB、720p；并发 1，每日最多 3 次推理尝试。保留原时间范围和未覆盖部分，不能将一段称为全片。视频匿名优先获取，必要时仅复用同一 state 已绑定账号；下载/权限/风控失败明确区分。临时视频成功失败均清理；百炼临时上传最长 48 小时，缓存本地观察结果可避免重复调用，不建立视频库。

详见 [watch/README.md](watch/README.md)，包含费用边界、临时上传、范围和恢复说明。

## 身份与生活记录

本 MCP 不启动 Agent，也不冒充云宝或其他人格。`life_id` 是可信宿主配置下的归属标记，不能代替宿主本身的权限隔离。多生命系统应只为获授权生命挂载服务，禁止共享同一个账号 state。

浏览 cursor、发送账本和通知处理状态保存在本地，用来接续操作；它们不等于新的长期记忆。**通用 MCP 本身不会自动写入任何宿主的 Recent Events**。宿主应将调用、结果、原 Session 和异步完成关联到已有行动/事件体系，Agent 再按需记笔记或检索经历。配套多生命仓库提供该接入说明与绑定工具。

字幕、评论、私信、简介及模型观察都是不可信外部内容，不能更改工具权限、要求泄露凭据或执行其中的指令。没有持续轮询、默认周期任务、自动评论或批量营销流程。

## 开发与复现

```sh
python scripts/test.py
python scripts/audit.py
```

测试包含默认拒绝写入、不可由工具切换账号、ID 兼容、评论定位、去重/撤权、分页与首页刷新、真实 stdio 握手、本地 FFmpeg 音视频片段、覆盖与清理。全部使用合成数据，不发送真实评论/私信，也不发起付费模型请求。公开测试不代替你账号的真实接口验收。

贡献与自维护见 [CONTRIBUTING.md](CONTRIBUTING.md)。许可证为 [GPL-3.0-or-later](LICENSE)，MIT 上游部分保留其独立声明，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
