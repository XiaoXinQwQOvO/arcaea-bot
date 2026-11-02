"""Arcaea 查分插件（NoneBot2 + QQ 官方适配器，本地渲染，成绩走服务端 API）。"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from nonebot import get_driver
from nonebot.adapters.qq import Message, MessageSegment
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna

from . import api, binds
from .data_source import (
    ArcaeaError,
    build_b50_data,
    build_completion_data,
    fmt_floor,
    load_catalog,
    resolve_chart,
    resolve_song,
)
from .engine import RenderError, RenderService
from .settings import BASE_DIR

_engine = RenderService(BASE_DIR / "templates")

driver = get_driver()
driver.on_startup(_engine.start)
driver.on_shutdown(_engine.close)
driver.on_shutdown(api.close_client)


def _send(matcher: Matcher, caption: str, image: bytes):
    return matcher.send(
        Message([MessageSegment.text(caption), MessageSegment.file_image(image)])
    )


def _text(arparma: Arparma, key: str) -> str:
    value = arparma.all_matched_args.get(key)
    if isinstance(value, list | tuple):
        return " ".join(str(i) for i in value).strip()
    return str(value or "").strip()


def _score_text(value: int) -> str:
    return f"{value:,}".replace(",", "'")


async def _get_bind(matcher: Matcher, event) -> dict[str, Any]:
    bind = await binds.get_bind(event.get_user_id())
    if bind is None:
        await matcher.finish("尚未绑定 Arcaea 账号，请先 /bind <好友码> <用户名>。")
    return bind


async def _user_info(matcher: Matcher, event) -> dict:
    bind = await _get_bind(matcher, event)
    try:
        info = await api.fetch_user_by_code(bind["user_code"])
    except api.ApiError as exc:
        await matcher.finish(str(exc))
    return {"user_id": bind["user_id"], "user_code": bind["user_code"], "info": info}


async def _play_scores(matcher: Matcher, user_id: int):
    from .data_source import PlayScore, compute_play_rating, load_catalog

    catalog = load_catalog()
    scores = []
    for item in await api.fetch_best_scores(user_id, 60):
        chart = catalog.by_key.get(
            (str(item["song_id"]), int(item["difficulty"]))
        )
        if chart is None:
            continue
        score = int(item["score"])
        if score <= 0:
            continue
        perfect = int(item["perfect_count"] or 0)
        scores.append(
            PlayScore(
                chart=chart,
                score=score,
                perfect=perfect,
                critical_perfect=min(
                    int(item["shiny_perfect_count"] or 0), perfect
                ),
                far=int(item["near_count"] or 0),
                lost=int(item["miss_count"] or 0),
                rating=(
                    float(item["rating"])
                    if item.get("rating") is not None
                    else compute_play_rating(score, chart.constant)
                ),
            )
        )
    scores.sort(key=lambda item: (item.rating, item.score), reverse=True)
    return scores


async def _resolve_chart(matcher: Matcher, query: str):
    try:
        return await asyncio.to_thread(resolve_chart, query)
    except ArcaeaError as exc:
        await matcher.finish(str(exc))


async def _resolve_song(matcher: Matcher, query: str):
    try:
        return await asyncio.to_thread(resolve_song, query)
    except ArcaeaError as exc:
        await matcher.finish(str(exc))


# ---------------------------------------------------------------- bind/unbind

bind_matcher = on_alconna(
    Alconna("bind", Args["query", Any]), priority=5, block=True
)
unbind_matcher = on_alconna(Alconna("unbind"), priority=5, block=True)
unbind_cn_matcher = on_alconna(Alconna("解绑"), priority=5, block=True)


@bind_matcher.handle()
async def bind_handler(matcher: Matcher, event, arparma: Arparma):
    raw = _text(arparma, "query")
    matched = re.match(r"^(\d[\d\s]{8,})\s+(.+)$", raw)
    if not matched:
        await matcher.finish("用法：/bind <9位好友码> <Arcaea用户名>")
    code = re.sub(r"\D", "", matched.group(1)).zfill(9)
    name = matched.group(2).strip()
    if not code or not name:
        await matcher.finish("用法：/bind <9位好友码> <Arcaea用户名>")
    try:
        info = await api.fetch_user_by_code(code, expected_name=name)
    except api.ApiError as exc:
        await matcher.finish(str(exc))
    qq_id = event.get_user_id()
    owner = await binds.get_bind_by_code(info.user_code)
    if owner is not None and owner["qq_id"] != qq_id:
        await matcher.finish(
            f"好友码 {info.user_code} 已被其他用户绑定，请原绑定者先 /解绑。"
        )
    await binds.set_bind(qq_id, info.user_id, info.user_code, info.name)
    await matcher.finish(
        f"绑定成功！\nArcaea：{info.name}\n好友码：{info.user_code}"
    )


@unbind_matcher.handle()
@unbind_cn_matcher.handle()
async def unbind_handler(matcher: Matcher, event):
    bind = await binds.get_bind(event.get_user_id())
    if bind is None:
        await matcher.finish("当前还没有绑定 Arcaea 账号。")
    await binds.del_bind(event.get_user_id())
    await matcher.finish("已解除 Arcaea 账号绑定。")


# ------------------------------------------------------------------------ b50

b50_matcher = on_alconna(Alconna("b50"), priority=5, block=True)


@b50_matcher.handle()
async def b50_handler(matcher: Matcher, event):
    user = await _user_info(matcher, event)
    try:
        scores = await _play_scores(user["user_id"])
        data = build_b50_data(
            user["info"], scores, int(__import__("arcaea.settings", fromlist=["_config"])._config.arcaea_b50_count)
        )
        image = await _render("b50", data)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    caption = (
        f"Arcaea B50 · {user['info'].name}\n"
        f"Max {fmt_floor(data.max_ptt, 4)} | "
        f"Best50 {fmt_floor(data.best50, 4)} | "
        f"Best10 {fmt_floor(data.best10, 4)}"
    )
    await _send(matcher, caption, image)


async def _render(template: str, *args) -> bytes:
    from . import render as _r

    builders = {
        "b50": _r.render_b50,
        "completion": _r.render_completion,
        "trend": _r.render_trend,
        "score": _r.render_score,
        "history": _r.render_history,
        "info": _r.render_info,
        "recent": _r.render_recent,
    }
    return await builders[template](*args)


# ------------------------------------------------------------------- 完成表

completion_matcher = on_alconna(
    Alconna("完成表", Args["lower?", float]["upper?", float]),
    priority=5,
    block=True,
)


@completion_matcher.handle()
async def completion_handler(matcher: Matcher, event, arparma: Arparma):
    from . import settings

    lo_default, hi_default = settings.get_completion_bounds()
    lower = arparma.all_matched_args.get("lower")
    upper = arparma.all_matched_args.get("upper")
    try:
        lo = float(lower) if lower is not None else lo_default
        hi = float(upper) if upper is not None else hi_default
    except (TypeError, ValueError):
        await matcher.finish("定数范围格式不正确，示例：/完成表 11.0 12.0")
    lo, hi = min(lo, hi), max(lo, hi)
    user = await _user_info(matcher, event)
    try:
        scores = await _play_scores(user["user_id"])
        data = await asyncio.to_thread(
            build_completion_data,
            user["info"],
            scores,
            lo,
            hi,
            settings.get_completion_show_no_record(),
            settings.get_completion_max_charts(),
        )
        image = await _render("completion", data)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    percent = (
        data.played_count / data.total_count * 100 if data.total_count else 0
    )
    caption = (
        f"Arcaea 完成表 · {user['info'].name}\n"
        f"定数 {data.lower:.1f} ~ {data.upper:.1f} | "
        f"已玩 {data.played_count}/{data.total_count} ({percent:.1f}%)"
    )
    await _send(matcher, caption, image)


# ---------------------------------------------------------------------- score

score_matcher = on_alconna(
    Alconna("score", Args["query", Any]), priority=5, block=True
)


@score_matcher.handle()
async def score_handler(matcher: Matcher, event, arparma: Arparma):
    from . import settings

    query = _text(arparma, "query")
    if not query:
        await matcher.finish("用法：/score <曲目名称> [难度]")
    resolution = await _resolve_chart(matcher, query)
    if resolution.error:
        await matcher.finish(resolution.error)
    if resolution.candidates:
        lines = [f"「{query}」匹配到多个曲目，请写得更具体："]
        for chart in resolution.candidates:
            lines.append(
                f"· {chart.title} [{chart.short_difficulty} {chart.constant:.1f}]"
            )
        await matcher.finish("\n".join(lines))
    chart = resolution.chart
    if chart is None:
        await matcher.finish("没有找到匹配的谱面。")
    user = await _user_info(matcher, event)
    try:
        entries = await api.fetch_leaderboard(
            chart.song_id, chart.difficulty_index, settings.get_score_top_n()
        )
        image = await _render("score", user["info"], chart, entries)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    await _send(
        matcher,
        f"Arcaea 排行榜 · {chart.title} "
        f"[{chart.short_difficulty} {chart.constant:.1f}]",
        image,
    )


# ------------------------------------------------------------------------ his

his_matcher = on_alconna(
    Alconna("his", Args["query", Any]), priority=5, block=True
)


@his_matcher.handle()
async def his_handler(matcher: Matcher, event, arparma: Arparma):
    query = _text(arparma, "query")
    if not query:
        await matcher.finish("用法：/his <曲目名称> [难度]")
    resolution = await _resolve_chart(matcher, query)
    if resolution.error:
        await matcher.finish(resolution.error)
    if resolution.candidates:
        await matcher.finish(
            "「{}」匹配到多个曲目，请写得更具体：\n{}".format(
                query,
                "\n".join(
                    f"· {c.title} [{c.short_difficulty} {c.constant:.1f}]"
                    for c in resolution.candidates
                ),
            )
        )
    chart = resolution.chart
    if chart is None:
        await matcher.finish("没有找到匹配的谱面。")
    user = await _user_info(matcher, event)
    try:
        history = await api.fetch_score_history(user["user_id"], chart)
        if history is None:
            await matcher.finish("暂无这张谱面的游玩记录。")
        image = await _render("history", user["info"], history)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    await _send(
        matcher,
        f"Arcaea 推分记录 · {chart.title} "
        f"[{chart.short_difficulty} {chart.constant:.1f}]\n"
        f"{history.play_count} 次游玩 | 最高 {_score_text(history.best_score)}",
        image,
    )


# ------------------------------------------------------------------ info/rand

info_matcher = on_alconna(
    Alconna("info", Args["query", Any]), priority=5, block=True
)
rand_matcher = on_alconna(Alconna("rand"), priority=5, block=True)


@info_matcher.handle()
async def info_handler(matcher: Matcher, arparma: Arparma):
    query = _text(arparma, "query")
    if not query:
        await matcher.finish("用法：/info <曲目名称>")
    resolution = await _resolve_song(matcher, query)
    if resolution.error:
        await matcher.finish(resolution.error)
    if resolution.candidates:
        await matcher.finish(
            "「{}」匹配到多首曲目，请写得更具体：\n{}".format(
                query,
                "\n".join(
                    f"· {s.title} · {s.artist}" for s in resolution.candidates
                ),
            )
        )
    song = resolution.song
    if song is None:
        await matcher.finish("没有找到匹配的曲目。")
    try:
        image = await _render("info", song, resolution.charts, resolution.difficulty, False)
    except RenderError as exc:
        await matcher.finish(str(exc))
    await _send(matcher, f"Arcaea 曲目信息 · {song.title}", image)


@rand_matcher.handle()
async def rand_handler(matcher: Matcher):
    catalog = await asyncio.to_thread(load_catalog)
    if not catalog.songs:
        await matcher.finish("曲目数据为空。")
    song = random.choice(list(catalog.songs.values()))
    charts = sorted(
        (c for c in catalog.charts if c.song_id == song.song_id),
        key=lambda c: (c.difficulty_index, c.constant),
    )
    try:
        image = await _render("info", song, charts, None, True)
    except RenderError as exc:
        await matcher.finish(str(exc))
    await _send(matcher, f"Arcaea 随机曲目 · {song.title}", image)


# --------------------------------------------------------------------- recent

recent_matcher = on_alconna(Alconna("recent"), priority=5, block=True)


@recent_matcher.handle()
async def recent_handler(matcher: Matcher, event):
    user = await _user_info(matcher, event)
    try:
        recent = await api.fetch_recent(user["user_id"])
        if recent is None:
            await matcher.finish("最近没有游玩记录。")
        image = await _render("recent", user["info"], recent)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    await _send(
        matcher,
        f"Arcaea 最近游玩 · {recent.chart.title}\n"
        f"{_score_text(recent.score)}",
        image,
    )


# ---------------------------------------------------------------------- trend

trend_matcher = on_alconna(Alconna("trend"), priority=5, block=True)


@trend_matcher.handle()
async def trend_handler(matcher: Matcher, event):
    user = await _user_info(matcher, event)
    try:
        history = await api.fetch_rating_history(user["user_id"])
        if not history:
            await matcher.finish("暂无 PTT 历史记录。")
        image = await _render("trend", user["info"], history)
    except (api.ApiError, ArcaeaError, RenderError) as exc:
        await matcher.finish(str(exc))
    await _send(
        matcher,
        f"Arcaea PTT 轨迹 · {user['info'].name}\n记录 {len(history)} 条",
        image,
    )
