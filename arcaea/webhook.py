"""OpenUptime 告警 webhook 接收端（复用 aiohttp driver 的 HTTP server）。"""

from __future__ import annotations

import json
from typing import Any

import nonebot
from nonebot.adapters.qq import Bot, Message, MessageSegment
from nonebot.log import logger

from . import settings


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
    group = str(settings.get("webhook_qq_group", "") or "").strip()
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

    expected = str(settings.get("webhook_token", "") or "").strip()
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


def register(driver) -> None:
    """把 webhook 路由挂到 aiohttp driver 的 server_app 上。"""
    app = getattr(driver, "server_app", None)
    if app is None or not hasattr(app, "router"):
        logger.warning(
            "当前驱动没有 HTTP server（DRIVER 不是 ~aiohttp），webhook 未启用"
        )
        return
    app.router.add_post("/webhook", _handle)
    app.router.add_post("/webhook/{token}", _handle)
    app.router.add_get("/webhook/health", _health)
    logger.info("OpenUptime webhook 已注册: POST /webhook/{token}")
