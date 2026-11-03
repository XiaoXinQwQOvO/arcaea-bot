"""OpenUptime 告警 webhook 接收端（复用 aiohttp driver 的 HTTP server）。"""

from __future__ import annotations

import json
import os
from typing import Any

import nonebot
from nonebot.adapters.qq import Bot, Message, MessageSegment
from nonebot.log import logger



def _format_alert(payload: Any) -> str:
    lines: list[str] = []

    def walk(obj: Any, prefix: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    walk(value, f"{prefix}{key}.")
                elif value not in (None, ""):
                    lines.append(f"{prefix}{key}: {value}")
        elif isinstance(obj, list):
            for index, value in enumerate(obj[:10]):
                walk(value, f"{prefix}{index}.")

    walk(payload)
    text = "\n".join(lines) or json.dumps(payload, ensure_ascii=False)
    return ("[OpenUptime 告警]\n" + text)[:1800]


async def _forward_to_qq(text: str) -> None:
    group = str(os.getenv("WEBHOOK_QQ_GROUP", "") or "").strip()
    if not group:
        return
    for bot in nonebot.get_bots().values():
        if not isinstance(bot, Bot):
            continue
        try:
            await bot.send_to_group(
                group_openid=group, message=Message(MessageSegment.text(text))
            )
            return
        except Exception as exc:
            logger.warning(f"webhook 转发 QQ 群失败: {exc}")


async def _handle(request):
    from aiohttp import web

    expected = str(os.getenv("WEBHOOK_TOKEN", "") or "").strip()
    if not expected:
        return web.json_response(
            {"error": "WEBHOOK_TOKEN 未配置"}, status=503
        )
    provided = (
        request.match_info.get("token")
        or request.query.get("token")
        or request.headers.get("X-Webhook-Token")
        or ""
    )
    if provided != expected:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.text())[:4000]}

    logger.info(
        "OpenUptime webhook: "
        + json.dumps(payload, ensure_ascii=False)[:2000]
    )
    await _forward_to_qq(_format_alert(payload))
    return web.json_response({"ok": True})


async def _health(request):
    from aiohttp import web

    return web.json_response({"ok": True})


_runner = None


async def start() -> None:
    """独立启动 webhook HTTP 服务（不依赖 nonebot driver）。"""
    global _runner
    from aiohttp import web

    host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("WEBHOOK_PORT", "443"))
    app = web.Application()
    app.router.add_post("/webhook", _handle)
    app.router.add_post("/webhook/{token}", _handle)
    app.router.add_get("/webhook/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _runner = runner
    logger.info(f"OpenUptime webhook 监听 {host}:{port}")


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
