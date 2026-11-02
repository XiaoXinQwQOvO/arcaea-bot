"""Arcaea 服务端数据库访问与成绩计算。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, field
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from nonebot.log import logger

from .settings import DATA_PATH, get_db_path, get_log_db_path

DIFFICULTY_NAMES = ("Past", "Present", "Future", "Beyond", "Eternal")
DIFFICULTY_KEYS = ("PST", "PRS", "FTR", "BYD", "ETR")
DIFFICULTY_SHORT = {
    "Past": "PST",
    "Present": "PRS",
    "Future": "FTR",
    "Beyond": "BYD",
    "Eternal": "ETR",
    "Inscribed": "INS",
}
DIFFICULTY_ORDER = {
    "Past": 0,
    "Present": 1,
    "Future": 2,
    "Beyond": 3,
    "Inscribed": 3,
    "Eternal": 4,
}
RANK_ORDER = ("PM", "FR", "EX+", "EX", "AA", "A", "B", "C", "D")


class ArcaeaError(Exception):
    """Arcaea 查分过程中的可预期错误。"""


class ArcaeaBindError(ArcaeaError):
    """绑定信息校验失败。"""


@dataclass(frozen=True, slots=True)
class Chart:
    song_id: str
    idx: int
    title: str
    difficulty: str
    difficulty_index: int
    constant: float
    jacket: str
    notes: int | None = None
    rating_plus: bool = False
    chart_designer: str = ""
    jacket_designer: str = ""

    @property
    def short_difficulty(self) -> str:
        return DIFFICULTY_SHORT.get(self.difficulty, self.difficulty[:3].upper())

    @property
    def level(self) -> str:
        """游戏内显示的等级，例如 FTR 10+。"""
        plus = "+" if self.rating_plus else ""
        return f"{self.short_difficulty}{int(self.constant)}{plus}"

    @property
    def key(self) -> tuple[str, int]:
        return self.song_id, self.difficulty_index


@dataclass(frozen=True, slots=True)
class SongInfo:
    song_id: str
    idx: int
    title: str
    artist: str
    bpm: str
    set_name: str
    side: int
    version: str
    date: int
    duration: str
    jacket: str


@dataclass(slots=True)
class UserInfo:
    user_id: int
    name: str
    user_code: str
    rating_ptt: float | None = None
    is_hide_rating: int = 0

    @property
    def ptt(self) -> float | None:
        if self.is_hide_rating or self.rating_ptt is None:
            return None
        return self.rating_ptt


@dataclass(slots=True)
class PlayScore:
    chart: Chart
    score: int
    perfect: int
    critical_perfect: int
    far: int
    lost: int
    rating: float
    time_played: int = 0

    @property
    def normal_perfect(self) -> int:
        return max(0, self.perfect - self.critical_perfect)


@dataclass(slots=True)
class Catalog:
    charts: list[Chart]
    by_key: dict[tuple[str, int], Chart]
    songs: dict[str, SongInfo] = field(default_factory=dict)


@dataclass(slots=True)
class B50Data:
    user: UserInfo
    top: list[PlayScore]
    all_scores: list[PlayScore]
    best50: float
    best10: float
    max_ptt: float
    generated_at: str


@dataclass(slots=True)
class CompletionItem:
    chart: Chart
    score: PlayScore | None = None

    @property
    def played(self) -> bool:
        return self.score is not None


@dataclass(slots=True)
class CompletionGroup:
    constant: float
    items: list[CompletionItem] = field(default_factory=list)


@dataclass(slots=True)
class CompletionData:
    user: UserInfo
    groups: list[CompletionGroup]
    lower: float
    upper: float
    played_count: int
    total_count: int
    generated_at: str


def _pick_title(song: dict[str, Any]) -> str:
    localized = song.get("title_localized") or {}
    title = localized.get("en") or localized.get("ja") or ""
    if title:
        return str(title)
    return str(song.get("id", "Unknown"))


def _resolve_constant(row: dict[str, Any] | None, key: str) -> float | None:
    if not row:
        return None
    value = row.get(key)
    if value in (None, "", "-"):
        if key == "INS":
            value = row.get("BYD")
        else:
            return None
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """加载 songlist / constants / chartmeta，构建全谱面目录。"""
    songlist_path = DATA_PATH / "songlist"
    constants_path = DATA_PATH / "constants.json"
    chartmeta_path = DATA_PATH / "chartmeta.json"
    try:
        songlist = json.loads(songlist_path.read_text(encoding="utf-8"))
        constants = json.loads(constants_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArcaeaError(f"曲目数据加载失败：{exc}") from exc

    song_constants: dict[str, dict[str, Any]] = constants.get("songConstants") or {}
    chartmeta: dict[str, dict[str, Any]] = {}
    if chartmeta_path.exists():
        try:
            chartmeta = (
                json.loads(chartmeta_path.read_text(encoding="utf-8")).get(
                    "songChartInfo"
                )
                or {}
            )
        except (OSError, json.JSONDecodeError):
            logger.warning("Arcaea chartmeta.json 读取失败，将忽略物量信息")

    charts: dict[tuple[str, int], Chart] = {}
    songs_info: dict[str, SongInfo] = {}
    songs = songlist.get("songs") or []
    for song in songs:
        song_id = str(song.get("id") or "")
        if not song_id:
            continue
        idx = int(song.get("idx", 0))
        const_row = song_constants.get(str(idx))
        if not const_row:
            continue
        base_title = _pick_title(song)
        meta_row = chartmeta.get(str(idx)) or {}
        songs_info[song_id] = SongInfo(
            song_id=song_id,
            idx=idx,
            title=base_title,
            artist=str(song.get("artist") or "Unknown"),
            bpm=str(song.get("bpm") or song.get("bpm_base") or "-"),
            set_name=str(song.get("set") or "-"),
            side=int(song.get("side") or 0),
            version=str(song.get("version") or "-"),
            date=int(song.get("date") or 0),
            duration=str(meta_row.get("duration") or "-"),
            jacket=f"{song_id}.jpg",
        )
        for difficulty in song.get("difficulties") or []:
            rating_class = difficulty.get("ratingClass")
            if rating_class is None:
                continue
            rating_class = int(rating_class)
            if rating_class < 0 or rating_class > 4:
                continue
            alias = int(difficulty.get("ratingClassAlias") or 0)
            if rating_class == 3 and alias == 1:
                difficulty_name = "Inscribed"
            else:
                difficulty_name = DIFFICULTY_NAMES[rating_class]
                if difficulty_name == "Eternal" and rating_class == 3:
                    difficulty_name = "Beyond"

            const_key = (
                "INS"
                if difficulty_name == "Inscribed"
                else DIFFICULTY_KEYS[rating_class]
            )
            constant = _resolve_constant(const_row, const_key)
            if constant is None:
                continue

            diff_localized = difficulty.get("title_localized") or {}
            title = diff_localized.get("en") or diff_localized.get("ja") or base_title
            if difficulty.get("jacketOverride"):
                jacket = f"{song_id}_{rating_class}.jpg"
            else:
                jacket = f"{song_id}.jpg"

            notes = None
            notes_row = meta_row.get("notes") or {}
            raw_notes = notes_row.get(const_key)
            if raw_notes is None and difficulty_name == "Inscribed":
                raw_notes = notes_row.get("BYD")
            if raw_notes not in (None, ""):
                try:
                    notes = int(raw_notes)
                except (TypeError, ValueError):
                    notes = None

            chart = Chart(
                song_id=song_id,
                idx=idx,
                title=str(title),
                difficulty=difficulty_name,
                difficulty_index=rating_class,
                constant=constant,
                jacket=jacket,
                notes=notes,
                rating_plus=bool(difficulty.get("ratingPlus")),
                chart_designer=str(difficulty.get("chartDesigner") or ""),
                jacket_designer=str(difficulty.get("jacketDesigner") or ""),
            )
            charts[chart.key] = chart

    chart_list = sorted(
        charts.values(),
        key=lambda c: (c.idx, DIFFICULTY_ORDER.get(c.difficulty, 9), c.song_id),
    )
    return Catalog(charts=chart_list, by_key=charts, songs=songs_info)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ArcaeaError(
            f"Arcaea 数据库不存在：\n{path}\n"
            "请检查插件配置 arcaea:DB_PATH 是否正确。"
        )
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.OperationalError:
        # WAL 模式或目录不可写时，只读 URI 可能无法建立共享内存文件；
        # 退回普通连接并强制 query_only，避免误写服务端数据库。
        try:
            connection = sqlite3.connect(path, timeout=10)
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            raise ArcaeaError(f"无法打开 Arcaea 数据库：{exc}") from exc
    except sqlite3.Error as exc:
        raise ArcaeaError(f"无法打开 Arcaea 数据库：{exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _normalize_code(code: str) -> str:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if not digits:
        raise ArcaeaBindError("好友码格式不正确，应为 9 位数字。")
    if len(digits) > 9:
        raise ArcaeaBindError("好友码格式不正确，应为 9 位数字。")
    return digits.zfill(9)


def _as_flag(value: Any) -> int:
    """SQLite 中的布尔字段可能是 0/1、字符串或 NULL。"""
    if isinstance(value, str):
        return int(value.strip().lower() not in {"", "0", "false", "no", "off"})
    return int(bool(value))


def _rating_ptt_column_is_integer(connection: sqlite3.Connection) -> bool:
    """判断 user.rating_ptt 的声明类型是否为整数。"""
    try:
        rows = connection.execute("PRAGMA table_info(user)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if str(row["name"]).lower() == "rating_ptt":
            return "INT" in str(row["type"] or "").upper()
    return False


def parse_rating_ptt(value: Any, integer_column: bool = False) -> float | None:
    """把数据库中的潜力值统一换算为原值。

    不同 Arcaea 服务端的存储格式不同：整数百分制（7.90 存为 790）
    与浮点原值（7.90 存为 7.9），这里自动识别，避免 7.9 被截断成 0.07。
    """
    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return 0.0
    # 整数列里的整数是百分制；整数列里出现浮点说明服务端直接存了原值。
    if isinstance(value, int) and integer_column:
        return raw / 100
    # 浮点列中出现 20 以上的值，只可能是百分制（游戏内 PTT 上限约 13）。
    if raw >= 20:
        return raw / 100
    return raw


def _fetch_user_sync(code: str, expected_name: str | None = None) -> UserInfo:
    normalized = _normalize_code(code)
    with closing(_connect_readonly(get_db_path())) as connection:
        integer_column = _rating_ptt_column_is_integer(connection)
        row = connection.execute(
            "SELECT user_id, name, user_code, rating_ptt, is_hide_rating "
            "FROM user WHERE user_code = ? LIMIT 1",
            (normalized,),
        ).fetchone()
    if not row:
        raise ArcaeaBindError("未找到该好友码对应的 Arcaea 账号，请检查好友码。")
    user = UserInfo(
        user_id=int(row["user_id"]),
        name=str(row["name"]),
        user_code=str(row["user_code"] or normalized),
        rating_ptt=parse_rating_ptt(row["rating_ptt"], integer_column),
        is_hide_rating=_as_flag(row["is_hide_rating"]),
    )
    if expected_name is not None and user.name != expected_name:
        raise ArcaeaBindError(
            "好友码与用户名不匹配。\n"
            f"好友码 {user.user_code} 对应的用户名是「{user.name}」，请确认后重试。"
        )
    return user


def _fetch_scores_sync(user: UserInfo, catalog: Catalog) -> list[PlayScore]:
    with closing(_connect_readonly(get_db_path())) as connection:
        rows = connection.execute(
            "SELECT song_id, difficulty, score, shiny_perfect_count, perfect_count, "
            "near_count, miss_count, time_played "
            "FROM best_score WHERE user_id = ?",
            (user.user_id,),
        ).fetchall()

    scores: list[PlayScore] = []
    missing: set[str] = set()
    for row in rows:
        song_id = str(row["song_id"] or "")
        try:
            difficulty = int(row["difficulty"])
            raw_score = int(row["score"] or 0)
        except (TypeError, ValueError):
            continue
        if raw_score <= 0:
            continue
        chart = catalog.by_key.get((song_id, difficulty))
        if chart is None:
            missing.add(song_id)
            continue
        perfect = int(row["perfect_count"] or 0)
        critical = int(row["shiny_perfect_count"] or 0)
        # 服务端的 perfect_count 为 Pure 总数，critical 为其中的大 P。
        critical = min(critical, perfect)
        scores.append(
            PlayScore(
                chart=chart,
                score=raw_score,
                perfect=perfect,
                critical_perfect=critical,
                far=int(row["near_count"] or 0),
                lost=int(row["miss_count"] or 0),
                rating=compute_play_rating(raw_score, chart.constant),
                time_played=int(row["time_played"] or 0),
            )
        )
    if missing:
        logger.debug(
            "Arcaea 数据库中存在曲目目录未收录的成绩，已跳过: "
            + ", ".join(sorted(missing)[:20])
        )
    scores.sort(key=lambda item: (item.rating, item.score), reverse=True)
    return scores


def compute_play_rating(score: int, constant: float) -> float:
    """按 Arcaea 7.0 公式计算单曲潜力值。"""
    if score >= 10_000_000:
        rating = constant + 2.0
    elif score > 9_800_000:
        rating = constant + 1.0 + (score - 9_800_000) / 200_000
    else:
        rating = constant + (score - 9_500_000) / 300_000
        rating = max(rating, 0.0)
    if score >= 7_000_000:
        rating += 0.2
    return round(rating, 9)


def song_rank(score: int, far: int, lost: int) -> str:
    """根据分数和 Far/Lost 计算评级。"""
    if far != 0 and lost == 0:
        return "FR"
    ranges = (
        (8_599_999, "D"),
        (8_899_999, "C"),
        (9_199_999, "B"),
        (9_499_999, "A"),
        (9_799_999, "AA"),
        (9_899_999, "EX"),
        (9_999_999, "EX+"),
        (11_000_000, "PM"),
    )
    for upper, label in ranges:
        if score <= upper:
            return label
    return "PM"


def rank_counts(items: list[CompletionItem]) -> dict[str, int]:
    counts = dict.fromkeys(RANK_ORDER, 0)
    for item in items:
        score = item.score
        if score is None:
            continue
        rank = song_rank(score.score, score.far, score.lost)
        counts[rank] = counts.get(rank, 0) + 1
    return counts


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def fetch_user(code: str, expected_name: str | None = None) -> UserInfo:
    """异步获取 Arcaea 用户信息。"""
    return await asyncio.to_thread(_fetch_user_sync, code, expected_name)


async def fetch_scores(user: UserInfo) -> list[PlayScore]:
    """异步获取用户全部最佳成绩。"""
    catalog = await asyncio.to_thread(load_catalog)
    return await asyncio.to_thread(_fetch_scores_sync, user, catalog)


def build_b50_data(user: UserInfo, scores: list[PlayScore], count: int = 50) -> B50Data:
    """计算 B50 展示数据。"""
    if not scores:
        raise ArcaeaError("该账号还没有任何有效成绩，请先在游戏内游玩并同步成绩。")
    count = max(1, count)
    top = scores[:count]
    best10_sum = sum(item.rating for item in scores[:10])
    best50_sum = sum(item.rating for item in scores[:50])
    # 与 Arcaea 官方/B50 页面一致：固定分母计算平均值。
    best10 = best10_sum / 10
    best50 = best50_sum / 50
    max_ptt = (best50_sum + best10_sum) / 60
    return B50Data(
        user=user,
        top=top,
        all_scores=scores,
        best50=best50,
        best10=best10,
        max_ptt=max_ptt,
        generated_at=_now_text(),
    )


def build_completion_data(
    user: UserInfo,
    scores: list[PlayScore],
    lower: float,
    upper: float,
    show_no_record: bool = True,
    max_charts: int = 2200,
) -> CompletionData:
    """构建完成表数据。"""
    if lower > upper:
        lower, upper = upper, lower
    catalog = load_catalog()
    score_map = {item.chart.key: item for item in scores}
    selected = [
        chart
        for chart in catalog.charts
        if lower - 1e-9 <= chart.constant <= upper + 1e-9
    ]
    if not selected:
        raise ArcaeaError(
            f"定数 {lower:.1f} ~ {upper:.1f} 范围内没有找到谱面，请调整范围。"
        )
    if len(selected) > max_charts:
        raise ArcaeaError(
            f"该范围包含 {len(selected)} 个谱面，超过上限 {max_charts}，"
            "请缩小区间后重试。"
        )
    played_count = sum(1 for chart in selected if chart.key in score_map)
    selected.sort(
        key=lambda chart: (
            -chart.constant,
            chart.idx,
            DIFFICULTY_ORDER.get(chart.difficulty, 9),
            chart.song_id,
        )
    )
    groups: list[CompletionGroup] = []
    current: CompletionGroup | None = None
    for chart in selected:
        score = score_map.get(chart.key)
        if score is None and not show_no_record:
            continue
        if current is None or abs(current.constant - chart.constant) > 1e-9:
            current = CompletionGroup(constant=chart.constant)
            groups.append(current)
        current.items.append(CompletionItem(chart=chart, score=score))
    if not groups:
        raise ArcaeaError("该范围内没有可显示的成绩。")
    return CompletionData(
        user=user,
        groups=groups,
        lower=lower,
        upper=upper,
        played_count=played_count,
        total_count=len(selected),
        generated_at=_now_text(),
    )


def fmt_floor(value: float, digits: int = 4) -> str:
    """截断小数位，避免浮点误差导致多进一位。"""
    multiplier = 10**digits
    eps = abs(value) * multiplier * 2.220446049250313e-16 * 8 + 1e-9
    return f"{math.floor(value * multiplier + eps) / multiplier:.{digits}f}"


DIFFICULTY_ALIASES = {
    "pst": "Past",
    "past": "Past",
    "过去": "Past",
    "prs": "Present",
    "present": "Present",
    "现在": "Present",
    "ftr": "Future",
    "future": "Future",
    "未来": "Future",
    "byd": "Beyond",
    "beyond": "Beyond",
    "超越": "Beyond",
    "etr": "Eternal",
    "eternal": "Eternal",
    "永恒": "Eternal",
    "ins": "Inscribed",
    "inscribed": "Inscribed",
    "异象": "Inscribed",
}

CLEAR_TYPE_NAMES = {
    0: "TRACK LOST",
    1: "CLEAR",
    2: "FC",
    3: "PM",
    4: "EASY",
    5: "HARD",
}


def clear_type_name(value: int) -> str:
    """把 best_score.best_clear_type 转换为可读状态。"""
    try:
        return CLEAR_TYPE_NAMES.get(int(value), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


@dataclass(slots=True)
class LeaderboardEntry:
    rank: int
    user_id: int
    name: str
    score: int
    rating: float
    time_played: int
    clear_type: int


@dataclass(slots=True)
class Leaderboard:
    entries: list[LeaderboardEntry]
    total_players: int
    own_rank: int | None
    own_entry: LeaderboardEntry | None


@dataclass(slots=True)
class ChartResolution:
    chart: Chart | None
    candidates: list[Chart]
    difficulty: str | None
    error: str | None


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s\-_：:·'\"()（）\[\]【】]+", "", str(text).casefold())


def _extract_difficulty(query: str) -> tuple[str, str | None]:
    """从查询末尾提取难度，例如 ``Grievous Lady FTR``。"""
    stripped = query.strip()
    tokens = stripped.split()
    if len(tokens) >= 2:
        difficulty = DIFFICULTY_ALIASES.get(tokens[-1].casefold())
        if difficulty:
            return " ".join(tokens[:-1]).strip(), difficulty
    return stripped, None


def resolve_chart(query: str) -> ChartResolution:
    """把用户输入的曲名解析为唯一谱面。

    - 支持末尾附加难度（PST/PRS/FTR/BYD/ETR/INS 或中文）
    - 未指定难度时默认取该曲最高难度
    - 匹配到多首曲目时返回候选列表
    """
    query, difficulty = _extract_difficulty(query)
    normalized = _normalize_text(query)
    if not normalized:
        return ChartResolution(None, [], difficulty, "请输入曲目名称。")

    catalog = load_catalog()
    matches = [
        chart for chart in catalog.charts if normalized in _normalize_text(chart.title)
    ]
    if not matches:
        return ChartResolution(None, [], difficulty, f"没有找到匹配「{query}」的曲目。")

    by_song: dict[str, list[Chart]] = {}
    for chart in matches:
        by_song.setdefault(chart.song_id, []).append(chart)

    songs: list[list[Chart]] = []
    for charts in by_song.values():
        charts.sort(
            key=lambda item: (item.difficulty_index, item.constant),
            reverse=True,
        )
        songs.append(charts)

    exact = [
        charts for charts in songs if _normalize_text(charts[0].title) == normalized
    ]
    if exact:
        songs = exact

    if len(songs) > 1:
        return ChartResolution(
            None, [charts[0] for charts in songs[:5]], difficulty, None
        )

    charts = songs[0]
    if difficulty:
        chosen = next(
            (chart for chart in charts if chart.difficulty == difficulty),
            None,
        )
        if chosen is None:
            available = " / ".join(
                f"{chart.short_difficulty} {chart.constant:.1f}"
                for chart in sorted(charts, key=lambda item: item.difficulty_index)
            )
            return ChartResolution(
                None,
                [],
                difficulty,
                f"「{query}」没有 {difficulty} 难度，可选：{available}",
            )
        return ChartResolution(chosen, [], difficulty, None)

    chosen = max(charts, key=lambda item: (item.difficulty_index, item.constant))
    return ChartResolution(chosen, [], chosen.difficulty, None)


def _entry_from_row(row: sqlite3.Row, rank: int) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=rank,
        user_id=int(row["user_id"]),
        name=str(row["name"] or "未知"),
        score=int(row["score"] or 0),
        rating=float(row["rating"] or 0.0),
        time_played=int(row["time_played"] or 0),
        clear_type=int(row["clear_type"] or 0),
    )


def _fetch_leaderboard_sync(chart: Chart, user_id: int, limit: int) -> Leaderboard:
    with closing(_connect_readonly(get_db_path())) as connection:
        rows = connection.execute(
            """
            SELECT b.user_id AS user_id,
                   COALESCE(u.name, '未知') AS name,
                   b.score AS score,
                   b.rating AS rating,
                   b.time_played AS time_played,
                   b.best_clear_type AS clear_type
            FROM best_score b
            LEFT JOIN user u ON u.user_id = b.user_id
            WHERE b.song_id = ? AND b.difficulty = ?
            ORDER BY b.score DESC, b.time_played DESC
            LIMIT ?
            """,
            (chart.song_id, chart.difficulty_index, limit),
        ).fetchall()
        entries = [_entry_from_row(row, rank) for rank, row in enumerate(rows, start=1)]

        total_row = connection.execute(
            "SELECT COUNT(*) FROM best_score " "WHERE song_id = ? AND difficulty = ?",
            (chart.song_id, chart.difficulty_index),
        ).fetchone()
        total_players = int(total_row[0]) if total_row else 0

        own_row = connection.execute(
            """
            SELECT b.user_id AS user_id,
                   COALESCE(u.name, '未知') AS name,
                   b.score AS score,
                   b.rating AS rating,
                   b.time_played AS time_played,
                   b.best_clear_type AS clear_type
            FROM best_score b
            LEFT JOIN user u ON u.user_id = b.user_id
            WHERE b.user_id = ? AND b.song_id = ? AND b.difficulty = ?
            """,
            (user_id, chart.song_id, chart.difficulty_index),
        ).fetchone()

        own_rank = None
        own_entry = None
        if own_row is not None:
            better_row = connection.execute(
                """
                SELECT COUNT(*) FROM best_score
                WHERE song_id = ? AND difficulty = ?
                  AND (score > ? OR (score = ? AND time_played > ?))
                """,
                (
                    chart.song_id,
                    chart.difficulty_index,
                    own_row["score"],
                    own_row["score"],
                    own_row["time_played"],
                ),
            ).fetchone()
            own_rank = int(better_row[0]) + 1 if better_row else 1
            own_entry = _entry_from_row(own_row, own_rank)

    return Leaderboard(entries, total_players, own_rank, own_entry)


async def fetch_leaderboard(chart: Chart, user_id: int, limit: int = 10) -> Leaderboard:
    """查询某张谱面的在线最佳成绩排行榜。"""
    return await asyncio.to_thread(
        _fetch_leaderboard_sync, chart, user_id, max(1, limit)
    )


def _fetch_ptt_history_sync(
    user_id: int, limit: int = 20000
) -> list[tuple[int, float]]:
    path = get_log_db_path()
    if not path.is_file():
        raise ArcaeaError(
            f"PTT 日志数据库不存在：\n{path}\n"
            "请检查 LOG_DB_PATH，或确认 Arcaea 服务端已启用日志数据库。"
        )
    try:
        with closing(_connect_readonly(path)) as connection:
            rows = connection.execute(
                """
                SELECT time, rating_ptt FROM user_rating_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ArcaeaError(
            "PTT 历史读取失败，请确认日志数据库包含 " f"user_rating_history 表。\n{exc}"
        ) from exc

    history: list[tuple[int, float]] = []
    for row in reversed(rows):
        try:
            timestamp = int(row["time"])
            value = float(row["rating_ptt"])
        except (TypeError, ValueError):
            continue
        if timestamp <= 0 or not math.isfinite(value):
            continue
        # 兼容百分制存储（12.34 存成 1234）的服务器。
        if value >= 20:
            value /= 100
        history.append((timestamp, value))
    return history


async def fetch_ptt_history(
    user: UserInfo, limit: int = 20000
) -> list[tuple[int, float]]:
    """读取用户 PTT 变化历史（毫秒时间戳, PTT）。"""
    return await asyncio.to_thread(_fetch_ptt_history_sync, user.user_id, limit)


@dataclass(slots=True)
class SongResolution:
    song: SongInfo | None
    charts: list[Chart]
    candidates: list[SongInfo]
    difficulty: str | None
    error: str | None


def resolve_song(query: str) -> SongResolution:
    """把用户输入的曲名解析为唯一歌曲（返回该曲全部难度）。"""
    query, difficulty = _extract_difficulty(query)
    normalized = _normalize_text(query)
    if not normalized:
        return SongResolution(None, [], [], difficulty, "请输入曲目名称。")

    catalog = load_catalog()
    matched_ids = {
        chart.song_id
        for chart in catalog.charts
        if normalized in _normalize_text(chart.title)
    }
    exact_ids = {
        chart.song_id
        for chart in catalog.charts
        if _normalize_text(chart.title) == normalized
    }
    songs = [
        catalog.songs[song_id] for song_id in matched_ids if song_id in catalog.songs
    ]
    if exact_ids:
        exact_songs = [song for song in songs if song.song_id in exact_ids]
        if exact_songs:
            songs = exact_songs
    if not songs:
        return SongResolution(
            None, [], [], difficulty, f"没有找到匹配「{query}」的曲目。"
        )
    songs.sort(key=lambda song: song.title)
    if len(songs) > 1:
        return SongResolution(None, [], songs[:5], difficulty, None)

    song = songs[0]
    charts = sorted(
        (chart for chart in catalog.charts if chart.song_id == song.song_id),
        key=lambda chart: (chart.difficulty_index, chart.constant),
    )
    return SongResolution(song, charts, [], difficulty, None)


@dataclass(slots=True)
class RecentScore:
    chart: Chart
    score: int
    perfect: int
    critical_perfect: int
    far: int
    lost: int
    health: int
    modifier: int
    clear_type: int
    rating: float
    time_played: int

    @property
    def normal_perfect(self) -> int:
        return max(0, self.perfect - self.critical_perfect)


def _placeholder_chart(song_id: str, difficulty: int) -> Chart:
    difficulty_name = (
        DIFFICULTY_NAMES[difficulty]
        if 0 <= difficulty < len(DIFFICULTY_NAMES)
        else "Unknown"
    )
    return Chart(
        song_id=song_id,
        idx=0,
        title=song_id,
        difficulty=difficulty_name,
        difficulty_index=difficulty,
        constant=0.0,
        jacket=f"{song_id}.jpg",
    )


def _fetch_recent_sync(user: UserInfo) -> RecentScore | None:
    with closing(_connect_readonly(get_db_path())) as connection:
        row = connection.execute(
            """
            SELECT song_id, difficulty, score, shiny_perfect_count,
                   perfect_count, near_count, miss_count, health, modifier,
                   time_played, clear_type, rating
            FROM user WHERE user_id = ? LIMIT 1
            """,
            (user.user_id,),
        ).fetchone()
    if row is None:
        return None

    song_id = str(row["song_id"] or "")
    score = int(row["score"] or 0)
    if not song_id or score <= 0:
        return None
    try:
        difficulty = int(row["difficulty"])
    except (TypeError, ValueError):
        return None

    chart = load_catalog().by_key.get((song_id, difficulty))
    if chart is None:
        chart = _placeholder_chart(song_id, difficulty)

    raw_time = int(row["time_played"] or 0)
    # user.time_played 在该服务端存毫秒，兼容旧版秒级存储。
    time_played = raw_time // 1000 if raw_time > 10**11 else raw_time
    perfect = int(row["perfect_count"] or 0)
    critical = min(int(row["shiny_perfect_count"] or 0), perfect)

    return RecentScore(
        chart=chart,
        score=score,
        perfect=perfect,
        critical_perfect=critical,
        far=int(row["near_count"] or 0),
        lost=int(row["miss_count"] or 0),
        health=int(row["health"] or 0),
        modifier=int(row["modifier"] or 0),
        clear_type=int(row["clear_type"] or 0),
        rating=float(row["rating"] or 0.0),
        time_played=time_played,
    )


async def fetch_recent(user: UserInfo) -> RecentScore | None:
    """读取 user 表中的最近一次游玩成绩。"""
    return await asyncio.to_thread(_fetch_recent_sync, user)


@dataclass(slots=True)
class PlayRecord:
    time_played: int
    score: int
    rating: float
    clear_type: int
    perfect: int
    critical_perfect: int
    far: int
    lost: int

    @property
    def normal_perfect(self) -> int:
        return max(0, self.perfect - self.critical_perfect)


@dataclass(slots=True)
class ChartHistory:
    chart: Chart
    records: list[PlayRecord]
    best_score: int
    best_rating: float

    @property
    def play_count(self) -> int:
        return len(self.records)


def _fetch_chart_history_sync(
    user: UserInfo, chart: Chart, limit: int = 1000
) -> ChartHistory | None:
    path = get_log_db_path()
    if not path.is_file():
        raise ArcaeaError(
            f"PTT 日志数据库不存在：\n{path}\n"
            "请检查 LOG_DB_PATH，或确认 Arcaea 服务端已启用日志数据库。"
        )
    try:
        with closing(_connect_readonly(path)) as connection:
            rows = connection.execute(
                """
                SELECT time_played, score, shiny_perfect_count,
                       perfect_count, near_count, miss_count,
                       clear_type, rating
                FROM user_score
                WHERE user_id = ? AND song_id = ? AND difficulty = ?
                ORDER BY time_played ASC
                LIMIT ?
                """,
                (user.user_id, chart.song_id, chart.difficulty_index, limit),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ArcaeaError(
            "游玩历史读取失败，请确认日志数据库包含 user_score 表。\n" f"{exc}"
        ) from exc

    records: list[PlayRecord] = []
    for row in rows:
        try:
            timestamp = int(row["time_played"])
            score = int(row["score"] or 0)
        except (TypeError, ValueError):
            continue
        if timestamp > 10**11:
            timestamp //= 1000
        if timestamp <= 0 or score <= 0:
            continue
        perfect = int(row["perfect_count"] or 0)
        critical = min(int(row["shiny_perfect_count"] or 0), perfect)
        records.append(
            PlayRecord(
                time_played=timestamp,
                score=score,
                rating=float(row["rating"] or 0.0),
                clear_type=int(row["clear_type"] or 0),
                perfect=perfect,
                critical_perfect=critical,
                far=int(row["near_count"] or 0),
                lost=int(row["miss_count"] or 0),
            )
        )
    if not records:
        return None
    return ChartHistory(
        chart=chart,
        records=records,
        best_score=max(record.score for record in records),
        best_rating=max(record.rating for record in records),
    )


async def fetch_chart_history(
    user: UserInfo, chart: Chart, limit: int = 1000
) -> ChartHistory | None:
    """读取用户在某张谱面上的全部游玩记录（日志库 user_score）。"""
    return await asyncio.to_thread(_fetch_chart_history_sync, user, chart, limit)
