# arcaea-bot（设计文档 / 进行中）

NoneBot2 + QQ 官方适配器机器人，部署在雨云云应用上运行本插件，
成绩数据通过 **Arcaea Server 的 HTTP API** 获取（不再直读 SQLite），
图片仍由 `arcaea-render` 渲染服务生成。

## 架构

```text
QQ 官方服务器 ◄──WebSocket──► arcaea-bot（雨云云应用，NoneBot2 + adapter-qq）
                                  │
                                  ├─ Arcaea Server API（/api/v1，Token 头）
                                  └─ arcaea-render（POST /render → JPEG）
```

## 服务端 API（已确认可用）

鉴权：请求头 `Token: <API_TOKEN>`，`API_TOKEN` 在 arcaea-server 的
`config.py` 中设置（`Config.API_TOKEN`），机器人当作系统客户端使用。

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/bot/users/by-code/<code>` | 好友码 → user_id/name（本 fork 新增 `api/bot.py`） |
| `GET /api/v1/users/<id>` | 用户信息（UserInfo.to_dict，含最近成绩列） |
| `GET /api/v1/users/<id>/best?limit=60&offset=0` | 全部 best 成绩（B50 计算） |
| `GET /api/v1/users/<id>/rating?duration=N` | PTT 历史（/trend 数据源） |
| `GET /api/v1/songs/<song_id>/<difficulty>/rank?limit=10` | 单谱排行榜 |
| `GET /api/v1/bot/users/<id>/recent` | 最近一次成绩（本 fork 新增） |
| `GET /api/v1/bot/users/<id>/score-history?song_id=&difficulty=` | 单谱游玩记录（/his 数据源，本 fork 新增） |

响应统一为 `{"code": 0, "message": "...", "data": ...}`（`api_code.success_return`）。

## Bot 项目结构（待实现）

```text
arcaea-bot/
├── bot.py                  # nonebot.init + adapter-qq + load_plugin
├── .env.example            # HOST/PORT、QQ_BOTS(appid/token/secret/intents)、插件配置
├── requirements.txt        # nonebot2[fastapi]、nonebot-adapter-qq、nonebot-plugin-alconna、httpx
├── Dockerfile              # python:3.12-slim，无 Chromium（渲染走云服务）
└── arcaea/                 # 适配后的插件（从 zhenxun_arcaea_b50 移植）
    ├── __init__.py         # matcher 定义与处理（on_alconna，逻辑同现插件）
    ├── config.py           # nonebot get_driver().config + env（API_BASE/API_TOKEN/RENDER_*/LOG_DB 无需）
    ├── api.py              # ServerAPI：by-code/best/rank/recent/score-history/rating-history
    ├── catalog.py          # songlist/constants/chartmeta 加载（复制现 data_source 的目录部分）
    ├── binds.py            # sqlite3(platform_qq_id → user_id/name)，to_thread
    ├── render.py           # 复用 remote_render + 模板上下文（从现 render.py 移植）
    ├── templates/          # 复制 zhenxun_arcaea_b50/templates（7 个模板 + style.css）
    └── resources/          # 复制 zhenxun_arcaea_b50/resources（目录路径仅供上下文使用）
```

## 命令 → API 映射

| 命令 | 数据获取 | 模板 |
| --- | --- | --- |
| `/bind <code> <name>` | `bot/users/by-code/<code>` 校验 name | 文字 |
| `/unbind` | 本地 binds | 文字 |
| `/b50` | `users/<id>/best?limit=100`（rating 排序取前 50） | b50 |
| `/完成表 [a] [b]` | 本地目录 + 逐谱不需要成绩，仅展示 | completion |
| `/score <曲名> [难度]` | `songs/<sid>/<diff>/rank?limit=10` | score |
| `/his <曲名> [难度]` | `bot/users/<id>/score-history` | history |
| `/info <曲名>` | 本地目录 | info |
| `/rand` | 本地目录 | info(random_pick) |
| `/recent` | `bot/users/<id>/recent` | recent |
| `/trend` | `users/<id>/rating?duration=…` | trend |

## 关键适配点（未完成，按序做）

1. **发图**：adapter-qq 的 `MessageSegment.image(url)` 只收 URL；群/C2C 发图需走
   官方媒体上传接口（`/v2/groups/{openid}/files`，`file_type=1`，`srv_send_msg=false`）
   拿 `file_info` 后以 `msg_type=7` 发送。优先确认 adapter-qq 是否内置
   （查 `adapters/qq/bot.py` 的 upload/call_api）；没有就封装
   `upload_image(bot, group_id, bytes) -> file_info`。
2. **平台 ID**：绑定键改用 `event.get_user_id()` + 适配器名（替代 zhenxun 的
   platform scope）。
3. **CD**：简单用 nonebot `Matcher` 的 `state` 或自带限流即可，可先不做。
4. **模板上下文**：现 `render.py` 的 `*_context` 与数据结构（UserInfo/Chart/…）
   保持字段一致，模板零改动；`Chart/SongInfo` 从 catalog 来，
   `UserInfo` 由 API 响应构造。
5. **部署（雨云云应用）**：镜像 `python:3.12-slim`；最低 0.5 核 / 512MB；
   服务类型「开放至公网」可不填（WebSocket 出站，无入站端口）；环境变量：
   `QQ_BOTS`（JSON）、`ARCAEA_API_BASE`、`ARCAEA_API_TOKEN`、
   `RENDER_SERVICE_URL`、`RENDER_SERVICE_TOKEN`、`COMMAND_START=/`。

## 部署（雨云云应用）

1. 推送本仓库，GitHub Actions 自动构建 `xiaoxinqwqovo/arcaea-bot:1.0.0`
   （需在仓库 Secrets 添加 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`）。
2. 云应用模板：镜像 `xiaoxinqwqovo/arcaea-bot:1.0.0`，最低 1 核 / 1024MB
   （本地 Chromium 渲染），无需入站端口（WebSocket 出站）。
3. 环境变量：把 `.env.example` 里的内容按实际填写，
   `QQ_APPID` / `QQ_SECRET` 填自己的机器人 AppID 和 AppSecret，
   `ARCAEA_API_BASE=https://arcapi-v3.arcaxa.xin/api/v1`、
   `ARCAEA_API_TOKEN=<服务端 API_TOKEN>`。

## 状态

- [x] 服务端新增 `api/bot.py`（by-code / recent / score-history），已注册
- [x] 架构变更：单镜像本地渲染（`arcaea/engine.py` = 原 render_service/renderer.py，
      `render.py` 的 `_render_via_service` 已改为调用本地 `_engine`）
- [x] `settings.py`（env 配置）、`api.py`（API 客户端）、`binds.py`（sqlite 绑定）
- [x] `data_source.py` / `templates/` / `resources/` 已复制并去掉 zhenxun 依赖
- [ ] **`__init__.py` 是未完成草稿**：缺 binds.get_bind_by_code；bind matcher 重复
      注册（`bind_handler` 装饰器和 `bind_matcher` 只留 on_alconna 一个）；缺
      b50/完成表/score/his/info/rand/recent/trend 处理器（逻辑照抄
      zhenxun_arcaea_b50/__init__.py，把 MessageUtils 换成 `_send()`，
      DB 调用换成 api.py；render_* 函数已在 render.py 就绪）
- [ ] bot.py / requirements.txt / Dockerfile / .env.example
- [ ] adapter-qq 发图：用 `MessageSegment.file_image(bytes)`（已确认 adapter 内部
      会自动走媒体上传），验证群聊可用
- [ ] 雨云部署：镜像基于 `mcr.microsoft.com/playwright/python:v1.57.0-jammy`，
      最低 1 核 / 1024MB，无入站端口
