#!/bin/sh
set -e
cd /app

PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
PW_MIRROR="https://npmmirror.com/mirrors/playwright"

if ! python -c "import nonebot, nonebot.adapters.qq, nonebot_plugin_alconna, fastapi, uvicorn, websockets, httpx, jinja2, PIL, playwright" >/dev/null 2>&1; then
    echo "[entrypoint] 检测到依赖缺失，开始自动安装..."
    pip install --no-cache-dir -r requirements.txt -i "$PIP_MIRROR" \
        || pip install --no-cache-dir -r requirements.txt \
        || echo "[entrypoint] 依赖安装失败，请检查容器网络"
fi

if ! python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.executable_path; p.stop()" >/dev/null 2>&1; then
    echo "[entrypoint] 检测到 Chromium 缺失，开始自动安装..."
    PLAYWRIGHT_DOWNLOAD_HOST="$PW_MIRROR" python -m playwright install chromium \
        || python -m playwright install chromium \
        || echo "[entrypoint] Chromium 安装失败，请检查容器网络"
fi

exec "$@"
