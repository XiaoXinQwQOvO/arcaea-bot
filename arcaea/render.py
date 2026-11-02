"""Arcaea B50 / 完成表：模板上下文构建与远程渲染。

Bot 只负责读取数据库、计算成绩和组装模板上下文；图片由独立部署的渲染服务
生成。渲染失败直接报错，不做本地截图回退。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
import math
import os
import re
from pathlib import Path
import time
from typing import ClassVar

from nonebot.log import logger

from .settings import (
    BG_PATH,
    JACKET_PATH,
    RANK_PATH,
    get_background_name,
    get_completion_columns,
    get_image_quality,
    get_max_image_height,
    get_output_width,
)
from .data_source import (
    RANK_ORDER,
    ArcaeaError,
    B50Data,
    Chart,
    ChartHistory,
    CompletionData,
    Leaderboard,
    PlayScore,
    RecentScore,
    SongInfo,
    UserInfo,
    clear_type_name,
    fmt_floor,
    rank_counts,
    song_rank,
)
from .engine import RenderService

TEMPLATE_DIR = (Path(__file__).parent / "templates").resolve()
MAX_IMAGE_BYTES = 4_500_000
RENDER_WIDTH = 1700
RENDER_FAILED_MESSAGE = "渲染服务器出错，请稍后重试。"


def _relative(path: Path) -> str | None:
    """把插件资源路径转换为模板目录下的相对 URL。"""
    if not path.is_file():
        return None
    try:
        return Path(os.path.relpath(path, TEMPLATE_DIR)).as_posix()
    except ValueError:
        return path.as_uri()


def _jacket_url(chart: Chart) -> str | None:
    return _relative(JACKET_PATH / chart.jacket)


def _rank_url(rank: str) -> str | None:
    return _relative(RANK_PATH / f"{rank}.png")


def _background_url() -> str | None:
    for name in (get_background_name(), "s9.webp", "1.webp"):
        url = _relative(BG_PATH / name)
        if url:
            return url
    return None


def _score_text(score: int) -> str:
    return f"{score:,}".replace(",", "'")


def _format_user_code(code: str) -> str:
    code = str(code or "")
    if len(code) >= 9:
        return f"{code[:3]} {code[3:6]} {code[6:9]}"
    return code


def _ptt_text(data: B50Data) -> str:
    ptt = data.user.ptt
    return f"PTT {ptt:.2f}" if ptt is not None else "PTT 隐藏"


def _score_context(score: PlayScore, index: int) -> dict:
    chart = score.chart
    theoretical = (
        score.score >= 10_000_000
        and score.perfect > 0
        and score.critical_perfect == score.perfect
    )
    return {
        "index": index,
        "title": chart.title,
        "placeholder": (chart.title[:1] or "?").upper(),
        "jacket": _jacket_url(chart),
        "difficulty": chart.difficulty.lower(),
        "level": chart.level,
        "constant": f"{chart.constant:.1f}",
        "info": f"{chart.level} [{chart.constant:.1f}]",
        "rating": fmt_floor(score.rating, 4),
        "score": _score_text(score.score),
        "perfect": score.perfect,
        "normal_perfect": score.normal_perfect,
        "far": score.far,
        "lost": score.lost,
        "rank_label": song_rank(score.score, score.far, score.lost),
        "rank_icon": _rank_url(song_rank(score.score, score.far, score.lost)),
        "theoretical": theoretical,
        "top_badge": index if index <= 3 else None,
    }


def _build_b50_context(data: B50Data) -> dict:
    top_chart = data.top[0].chart if data.top else None
    return {
        "user_name": data.user.name,
        "user_code": _format_user_code(data.user.user_code),
        "ptt_text": _ptt_text(data),
        "avatar_url": _jacket_url(top_chart) if top_chart else None,
        "avatar_letter": (top_chart.title[:1] or "?").upper() if top_chart else "?",
        "metrics": [
            {"label": "MAX", "value": fmt_floor(data.max_ptt, 4)},
            {"label": "BEST 50", "value": fmt_floor(data.best50, 4)},
            {"label": "BEST 10", "value": fmt_floor(data.best10, 4)},
        ],
        "scores": [
            _score_context(score, index)
            for index, score in enumerate(data.top, start=1)
        ],
        "background_url": _background_url(),
        "generated_at": data.generated_at,
    }


def _completion_item_context(item) -> dict:
    chart = item.chart
    score = item.score
    rank_label = (
        song_rank(score.score, score.far, score.lost) if score is not None else ""
    )
    return {
        "played": item.played,
        "title": chart.title,
        "placeholder": (chart.title[:1] or "?").upper(),
        "jacket": _jacket_url(chart),
        "difficulty": chart.difficulty.lower(),
        "level": chart.level,
        "constant": f"{chart.constant:.1f}",
        "info": f"{chart.level} {chart.constant:.1f}",
        "rating": fmt_floor(score.rating, 4) if score is not None else "-",
        "score": _score_text(score.score) if score is not None else "",
        "rank_label": rank_label,
        "rank_icon": _rank_url(rank_label) if rank_label else None,
    }


def _build_completion_context(data: CompletionData) -> dict:
    groups = []
    for group in data.groups:
        stats = rank_counts(group.items)
        total = len(group.items)
        groups.append(
            {
                "constant": f"{group.constant:.1f}",
                "played": sum(1 for item in group.items if item.played),
                "total": total,
                "stats": [
                    {
                        "label": rank,
                        "count": stats.get(rank, 0),
                        "total": total,
                        "icon": _rank_url(rank),
                        "is_pm": rank == "PM",
                        "is_fr": rank == "FR",
                    }
                    for rank in RANK_ORDER
                ],
                "cards": [_completion_item_context(item) for item in group.items],
            }
        )

    percent = data.played_count / data.total_count * 100 if data.total_count else 0.0
    return {
        "user_name": data.user.name,
        "user_code": _format_user_code(data.user.user_code),
        "lower": f"{data.lower:.1f}",
        "upper": f"{data.upper:.1f}",
        "played": data.played_count,
        "total": data.total_count,
        "percent": f"{percent:.1f}%",
        "columns": get_completion_columns(),
        "groups": groups,
        "background_url": _background_url(),
        "generated_at": data.generated_at,
    }


_engine = RenderService(BASE_DIR / "templates")


async def _render_via_service(template: str, context: dict, width: int) -> bytes:
    """本地 Playwright 渲染（模板与资源在插件目录内）。"""
    return await _engine.render(
        template,
        context,
        width=width,
        output_width=get_output_width(),
        quality=get_image_quality(),
        max_bytes=MAX_IMAGE_BYTES,
    )


def _format_trend_date(timestamp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "-"


def _downsample_history(
    points: list[tuple[int, float]], limit: int = 260
) -> list[tuple[int, float]]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    indices = sorted({round(index * step) for index in range(limit)})
    return [points[index] for index in indices]


def _build_trend_context(user: UserInfo, history: list[tuple[int, float]]) -> dict:
    points = _downsample_history(history)
    values = [value for _, value in points]
    low, high = min(values), max(values)
    if high - low < 0.5:
        low -= 0.25
        high += 0.25
    else:
        padding = (high - low) * 0.08
        low -= padding
        high += padding

    plot_width = TREND_WIDTH - TREND_LEFT - TREND_RIGHT
    plot_height = TREND_HEIGHT - TREND_TOP - TREND_BOTTOM
    count = len(points)
    coordinates: list[tuple[float, float]] = []
    for index, (_, value) in enumerate(points):
        x = TREND_LEFT + (
            index / (count - 1) * plot_width if count > 1 else plot_width / 2
        )
        y = TREND_TOP + (high - value) / (high - low) * plot_height
        coordinates.append((x, y))

    line_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coordinates)
    first_x = coordinates[0][0]
    last_x = coordinates[-1][0]
    bottom_y = TREND_TOP + plot_height
    area_path = (
        f"{line_path} L {last_x:.1f} {bottom_y:.1f} "
        f"L {first_x:.1f} {bottom_y:.1f} Z"
    )

    grid = []
    for index in range(5):
        ratio = index / 4
        grid.append(
            {
                "y": f"{TREND_TOP + plot_height * ratio:.1f}",
                "label": f"{high - (high - low) * ratio:.2f}",
            }
        )

    x_labels = []
    for index in sorted({0, count // 2, count - 1}):
        x_labels.append(
            {
                "x": f"{coordinates[index][0]:.1f}",
                "label": _format_trend_date(points[index][0]),
            }
        )

    max_index = max(range(count), key=lambda i: points[i][1])
    min_index = min(range(count), key=lambda i: points[i][1])
    current_index = count - 1
    marker_specs = (
        ("current", current_index),
        ("max", max_index),
        ("min", min_index),
    )
    markers = []
    seen: set[int] = set()
    for kind, index in marker_specs:
        if index in seen:
            continue
        seen.add(index)
        markers.append(
            {
                "kind": kind,
                "x": f"{coordinates[index][0]:.1f}",
                "y": f"{coordinates[index][1]:.1f}",
                "value": f"{points[index][1]:.2f}",
            }
        )

    current = values[-1]
    start = values[0]
    delta = current - start
    recent = []
    for index in range(max(0, len(history) - 5), len(history)):
        timestamp, value = history[index]
        previous = history[index - 1][1] if index > 0 else None
        change = value - previous if previous is not None else None
        if change is None:
            kind = "flat"
        elif change >= 0:
            kind = "up"
        else:
            kind = "down"
        recent.append(
            {
                "date": _format_trend_date(timestamp),
                "value": f"{value:.2f}",
                "delta": f"{change:+.2f}" if change is not None else "—",
                "kind": kind,
            }
        )
    recent.reverse()

    if delta > 0.005:
        delta_class = "up"
    elif delta < -0.005:
        delta_class = "down"
    else:
        delta_class = "flat"

    return {
        "user_name": user.name,
        "user_code": _format_user_code(user.user_code),
        "current": f"{current:.2f}",
        "start": f"{start:.2f}",
        "max_value": f"{max(values):.2f}",
        "min_value": f"{min(values):.2f}",
        "delta": f"{delta:+.2f}" if abs(delta) >= 0.005 else "0.00",
        "delta_class": delta_class,
        "records": len(history),
        "range_text": (
            f"{_format_trend_date(points[0][0])} ~ "
            f"{_format_trend_date(points[-1][0])}"
        ),
        "line_path": line_path,
        "area_path": area_path,
        "grid": grid,
        "x_labels": x_labels,
        "markers": markers,
        "recent": recent,
        "background_url": _background_url(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def render_trend(user: UserInfo, history: list[tuple[int, float]]) -> bytes:
    """渲染 PTT 推分轨迹图（远程渲染服务）。"""
    return await _render_via_service(
        "trend", _build_trend_context(user, history), RENDER_WIDTH
    )


def _format_unix_date(timestamp_seconds: int) -> str:
    if timestamp_seconds <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(timestamp_seconds).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "-"


def _format_score_value(value: int | float) -> str:
    return f"{int(value):,}".replace(",", "'")


def _clear_class(value: int) -> str:
    try:
        return {
            3: "pm",
            2: "fc",
            5: "hard",
            1: "clear",
            4: "easy",
        }.get(int(value), "lost")
    except (TypeError, ValueError):
        return "lost"


def _sanitize_name(value: str, limit: int = 18) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit] or "未知"


def _build_line_geometry(
    points: list[tuple[int, float]],
    value_format: Callable[[float], str],
    *,
    min_span: float = 0.5,
    pad_ratio: float = 0.08,
) -> dict:
    values = [value for _, value in points]
    low, high = min(values), max(values)
    if high - low < min_span:
        low -= min_span / 2
        high += min_span / 2
    else:
        padding = (high - low) * pad_ratio
        low -= padding
        high += padding

    width, height = 1600, 620
    left, right, top, bottom = 90, 50, 40, 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    count = len(points)
    coordinates: list[tuple[float, float]] = []
    for index, (_, value) in enumerate(points):
        x = (
            left + index / (count - 1) * plot_width
            if count > 1
            else left + plot_width / 2
        )
        y = top + (high - value) / (high - low) * plot_height
        coordinates.append((x, y))

    line_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coordinates)
    first_x = coordinates[0][0]
    last_x = coordinates[-1][0]
    bottom_y = top + plot_height
    area_path = (
        f"{line_path} L {last_x:.1f} {bottom_y:.1f} "
        f"L {first_x:.1f} {bottom_y:.1f} Z"
    )

    grid = []
    for index in range(5):
        ratio = index / 4
        grid.append(
            {
                "y": f"{top + plot_height * ratio:.1f}",
                "label": value_format(high - (high - low) * ratio),
            }
        )

    x_labels = []
    for index in sorted({0, count // 2, count - 1}):
        x_labels.append(
            {
                "x": f"{coordinates[index][0]:.1f}",
                "label": _format_trend_date(points[index][0]),
            }
        )

    max_index = max(range(count), key=lambda i: points[i][1])
    min_index = min(range(count), key=lambda i: points[i][1])
    markers = []
    seen: set[int] = set()
    for kind, index in (
        ("current", count - 1),
        ("max", max_index),
        ("min", min_index),
    ):
        if index in seen:
            continue
        seen.add(index)
        markers.append(
            {
                "kind": kind,
                "x": f"{coordinates[index][0]:.1f}",
                "y": f"{coordinates[index][1]:.1f}",
                "value": value_format(points[index][1]),
            }
        )

    return {
        "line_path": line_path,
        "area_path": area_path,
        "grid": grid,
        "x_labels": x_labels,
        "markers": markers,
    }


def _build_history_context(user: UserInfo, data: ChartHistory) -> dict:
    chart = data.chart
    points = _downsample_history(
        [(record.time_played * 1000, float(record.score)) for record in data.records]
    )
    geometry = _build_line_geometry(
        points,
        lambda value: _format_score_value(round(value)),
        min_span=10000.0,
    )
    first = data.records[0]
    last = data.records[-1]
    delta = last.score - first.score
    if delta > 0:
        delta_class = "up"
        delta_text = f"{delta:+,}".replace(",", "'")
    elif delta < 0:
        delta_class = "down"
        delta_text = f"{delta:+,}".replace(",", "'")
    else:
        delta_class = "flat"
        delta_text = "0"

    recent = []
    for record in reversed(data.records[-10:]):
        recent.append(
            {
                "date": _format_unix_date(record.time_played),
                "score": _format_score_value(record.score),
                "rating": f"{record.rating:.2f}",
                "clear": clear_type_name(record.clear_type),
                "clear_class": _clear_class(record.clear_type),
                "perfect": record.perfect,
                "far": record.far,
                "lost": record.lost,
            }
        )

    context = {
        "user_name": user.name,
        "user_code": _format_user_code(user.user_code),
        "chart_title": chart.title,
        "difficulty": chart.short_difficulty,
        "difficulty_key": chart.difficulty.lower(),
        "level": chart.level,
        "constant": f"{chart.constant:.1f}",
        "jacket_url": _jacket_url(chart),
        "play_count": data.play_count,
        "best_score": _format_score_value(data.best_score),
        "best_rating": f"{data.best_rating:.2f}",
        "first_date": _format_unix_date(first.time_played),
        "last_date": _format_unix_date(last.time_played),
        "delta": delta_text,
        "delta_class": delta_class,
        "recent": recent,
        "background_url": _background_url(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    context.update(geometry)
    return context


async def render_history(user: UserInfo, data: ChartHistory) -> bytes:
    """渲染单张谱面的推分记录图（远程渲染服务）。"""
    return await _render_via_service(
        "history", _build_history_context(user, data), RENDER_WIDTH
    )


def _build_score_context(user: UserInfo, chart: Chart, board: Leaderboard) -> dict:
    entries = []
    for entry in board.entries:
        entries.append(
            {
                "rank": entry.rank,
                "name": _sanitize_name(entry.name),
                "score": _format_score_value(entry.score),
                "clear": clear_type_name(entry.clear_type),
                "clear_class": _clear_class(entry.clear_type),
                "rating": f"{entry.rating:.2f}",
                "date": _format_unix_date(entry.time_played),
                "is_me": entry.user_id == user.user_id,
            }
        )

    own = None
    if board.own_entry is not None:
        own = {
            "rank": board.own_entry.rank,
            "score": _format_score_value(board.own_entry.score),
            "clear": clear_type_name(board.own_entry.clear_type),
            "clear_class": _clear_class(board.own_entry.clear_type),
            "rating": f"{board.own_entry.rating:.2f}",
        }
    own_in_top = any(entry["is_me"] for entry in entries)

    return {
        "user_name": user.name,
        "user_code": _format_user_code(user.user_code),
        "chart_title": chart.title,
        "difficulty": chart.short_difficulty,
        "difficulty_key": chart.difficulty.lower(),
        "level": chart.level,
        "constant": f"{chart.constant:.1f}",
        "jacket_url": _jacket_url(chart),
        "total_players": board.total_players,
        "entries": entries,
        "own": own,
        "own_in_top": own_in_top,
        "background_url": _background_url(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def render_score(user: UserInfo, chart: Chart, board: Leaderboard) -> bytes:
    """渲染单张谱面的排行榜（远程渲染服务）。"""
    return await _render_via_service(
        "score", _build_score_context(user, chart, board), RENDER_WIDTH
    )


SIDE_LABELS = {0: "LIGHT", 1: "CONFLICT", 2: "COLORLESS"}


def _build_info_context(
    song: SongInfo,
    charts: list[Chart],
    difficulty: str | None = None,
    random_pick: bool = False,
) -> dict:
    charts_context = []
    for chart in sorted(
        charts, key=lambda item: (item.difficulty_index, item.constant)
    ):
        charts_context.append(
            {
                "difficulty": chart.short_difficulty,
                "difficulty_key": chart.difficulty.lower(),
                "level": chart.level,
                "constant": f"{chart.constant:.1f}",
                "notes": chart.notes if chart.notes is not None else "-",
                "designer": chart.chart_designer or "-",
                "jacket_designer": chart.jacket_designer or "",
                "highlight": difficulty is not None and chart.difficulty == difficulty,
            }
        )
    return {
        "song_title": song.title,
        "artist": song.artist,
        "bpm": song.bpm,
        "set_name": song.set_name,
        "side": SIDE_LABELS.get(song.side, f"SIDE {song.side}"),
        "version": song.version,
        "release_date": _format_unix_date(song.date),
        "duration": song.duration,
        "jacket_url": _relative(JACKET_PATH / song.jacket),
        "charts": charts_context,
        "random_pick": random_pick,
        "background_url": _background_url(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def render_info(
    song: SongInfo,
    charts: list[Chart],
    difficulty: str | None = None,
    random_pick: bool = False,
) -> bytes:
    """渲染歌曲详细信息（远程渲染服务）。"""
    return await _render_via_service(
        "info",
        _build_info_context(song, charts, difficulty, random_pick),
        RENDER_WIDTH,
    )


def _build_recent_context(user: UserInfo, recent: RecentScore) -> dict:
    chart = recent.chart
    return {
        "user_name": user.name,
        "user_code": _format_user_code(user.user_code),
        "chart_title": chart.title,
        "difficulty": chart.short_difficulty,
        "difficulty_key": chart.difficulty.lower(),
        "level": chart.level,
        "constant": f"{chart.constant:.1f}",
        "jacket_url": _jacket_url(chart),
        "score": _format_score_value(recent.score),
        "perfect": recent.perfect,
        "critical_perfect": recent.critical_perfect,
        "normal_perfect": recent.normal_perfect,
        "far": recent.far,
        "lost": recent.lost,
        "rating": f"{recent.rating:.2f}",
        "clear": clear_type_name(recent.clear_type),
        "clear_class": _clear_class(recent.clear_type),
        "date": _format_unix_date(recent.time_played),
        "background_url": _background_url(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def render_recent(user: UserInfo, recent: RecentScore) -> bytes:
    """渲染最近一次游玩成绩（远程渲染服务）。"""
    return await _render_via_service(
        "recent", _build_recent_context(user, recent), RENDER_WIDTH
    )


async def render_b50(data: B50Data) -> bytes:
    """渲染 B50 成绩图（远程渲染服务）。"""
    return await _render_via_service("b50", _build_b50_context(data), RENDER_WIDTH)


async def render_completion(data: CompletionData) -> bytes:
    """渲染完成表图片（远程渲染服务）。"""
    columns = get_completion_columns()
    rows = sum(max(1, math.ceil(len(group.items) / columns)) for group in data.groups)
    estimated_height = 250 + rows * 150 + len(data.groups) * 80 + 96
    max_height = get_max_image_height()
    if estimated_height > max_height:
        raise ArcaeaError(
            f"完成表范围过大（预计高度约 {estimated_height}px，上限 {max_height}px），"
            "请缩小区间后重试。"
        )
    return await _render_via_service(
        "completion", _build_completion_context(data), RENDER_WIDTH
    )


class RenderQueue:
    """串行化渲染与发送：一张图渲染并发送完成后才开始下一张。

    用法::

        async with render_queue.slot("b50") as (position, eta):
            # position/eta 可先发送排队提示
            image = await render_b50(data)
            await send(image)
    """

    _DEFAULT_ETA: ClassVar[dict[str, float]] = {
        "b50": 5.0,
        "completion": 10.0,
        "trend": 8.0,
        "score": 5.0,
        "his": 8.0,
        "info": 5.0,
        "rand": 5.0,
        "recent": 5.0,
    }

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._waiting = 0
        self._ema: dict[str, float] = {}

    def _estimate(self, kind: str) -> float:
        return self._ema.get(kind) or self._DEFAULT_ETA.get(kind, 20.0)

    @asynccontextmanager
    async def slot(self, kind: str) -> AsyncIterator[tuple[int, float]]:
        """获取一个排队位，返回 ``(第几位, 预计秒数)``，锁在退出时释放。"""
        self._waiting += 1
        position = self._waiting
        eta = self._estimate(kind) * position
        try:
            await self._lock.acquire()
        except BaseException:
            self._waiting -= 1
            raise
        try:
            started = time.monotonic()
            yield position, eta
            duration = time.monotonic() - started
            previous = self._ema.get(kind)
            self._ema[kind] = (
                duration if previous is None else previous * 0.7 + duration * 0.3
            )
        finally:
            self._lock.release()
            self._waiting -= 1


render_queue = RenderQueue()


def format_eta(seconds: float) -> str:
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"约 {seconds} 秒"
    return f"约 {seconds // 60} 分 {seconds % 60:02d} 秒"
