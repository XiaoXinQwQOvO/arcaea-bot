"""Arcaea Server API 客户端（/api/v1，Token 头鉴权）。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import settings


class ApiError(Exception):
    """API 请求失败。"""


def _normalize_ptt(value) -> float | None:
    if value is None:
        return None
    v = float(value)
    if v >= 1000:
        return v / 1000
    if v >= 20:
        return v / 100
    return v


_client: httpx.AsyncClient | None = None


def _headers() -> dict[str, str]:
    token = settings.get("api_token", "")
    if not token:
        raise ApiError("未配置 ARCAEA_API_TOKEN")
    return {"Token": token, "Accept": "application/json"}


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.as_float("api_timeout", 60.0), connect=10.0),
            trust_env=True,
        )
    return _client


async def _post(path: str, payload: dict[str, Any]) -> Any:
    client = await _get_client()
    base = settings.get("api_base", "")
    if not base:
        raise ApiError("未配置 ARCAEA_API_BASE")
    try:
        resp = await client.post(
            base.rstrip("/") + path, json=payload, headers=_headers()
        )
    except httpx.HTTPError as exc:
        raise ApiError(f"Arcaea 服务端请求失败：{exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise ApiError(f"服务端返回非 JSON（HTTP {resp.status_code}）") from exc
    if resp.status_code >= 400 or data.get("code", -1) != 0:
        raise ApiError(f"服务端返回 {resp.status_code}：{data.get('message') or data}")
    return data.get("data")


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    client = await _get_client()
    url = settings.get("api_base", "").rstrip("/") + path
    if not settings.get("api_base", ""):
        raise ApiError("未配置 ARCAEA_API_BASE")
    try:
        resp = await client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise ApiError("Arcaea 服务端响应超时") from exc
    except httpx.HTTPError as exc:
        raise ApiError(f"Arcaea 服务端请求失败：{exc}") from exc
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ApiError(f"服务端返回非 JSON（HTTP {resp.status_code}）") from exc
    code = payload.get("code", -1)
    if resp.status_code >= 400 or (isinstance(code, int) and code != 0):
        message = str(payload.get("message") or resp.text)[:200]
        raise ApiError(f"服务端返回 {resp.status_code}：{message}")
    return payload.get("data")


def _user_info(raw: dict[str, Any]):
    from .data_source import UserInfo

    return UserInfo(
        user_id=int(raw["user_id"]),
        name=str(raw.get("name") or "未知"),
        user_code=str(raw.get("user_code") or ""),
        rating_ptt=(
            None
            if raw.get("is_hide_rating")
            else _normalize_ptt(raw.get("rating_ptt"))
        ),
        is_hide_rating=int(raw.get("is_hide_rating") or 0),
    )


async def fetch_user_by_code(user_code: str, expected_name: str | None = None):
    raw = await _get(f"/bot/users/by-code/{user_code.strip()}")
    info = _user_info(raw)
    if expected_name and info.name != expected_name:
        raise ApiError(
            f"好友码 {info.user_code} 对应的用户名是「{info.name}」，请确认后重试。"
        )
    return info


async def fetch_user(user_id: int):
    return _user_info(await _get(f"/users/{user_id}"))


async def fetch_best_scores(user_id: int, limit: int = 60) -> list[dict[str, Any]]:
    raw = await _get(f"/users/{user_id}/best", {"limit": limit})
    if isinstance(raw, dict):
        raw = raw.get("data") or []
    return raw or []


async def fetch_leaderboard(song_id: str, difficulty: int, limit: int = 10) -> list:
    from .data_source import LeaderboardEntry

    raw = await _get(f"/songs/{song_id}/{difficulty}/rank", {"limit": limit})
    if isinstance(raw, dict):
        raw = raw.get("data") or []
    entries = []
    for index, item in enumerate(raw or [], start=1):
        if not isinstance(item, dict):
            continue
        entries.append(
            LeaderboardEntry(
                rank=int(item.get("rank") or index),
                user_id=int(item.get("user_id") or 0),
                name=str(item.get("name") or "未知"),
                score=int(item.get("score") or 0),
                rating=float(item.get("rating") or 0.0),
                time_played=int(item.get("time_played") or 0),
                clear_type=int(item.get("clear_type") or item.get("best_clear_type") or 0),
            )
        )
    return entries


async def fetch_recent(user_id: int):
    from .data_source import RecentScore, load_catalog

    raw = await _get(f"/bot/users/{user_id}/recent")
    score = int(raw["score"] or 0)
    if score <= 0:
        return None
    catalog = load_catalog()
    chart = catalog.by_key.get((str(raw["song_id"]), int(raw["difficulty"])))
    if chart is None:
        chart = catalog._placeholder_chart(str(raw["song_id"]), int(raw["difficulty"]))
    perfect = int(raw["perfect_count"] or 0)
    return RecentScore(
        chart=chart,
        score=score,
        perfect=perfect,
        critical_perfect=min(int(raw["shiny_perfect_count"] or 0), perfect),
        far=int(raw["near_count"] or 0),
        lost=int(raw["miss_count"] or 0),
        health=int(raw["health"] or 0),
        modifier=int(raw["modifier"] or 0),
        clear_type=int(raw["clear_type"] or 0),
        rating=float(raw["rating"] or 0.0),
        time_played=(
            int(raw["time_played"] or 0) // 1000
            if int(raw["time_played"] or 0) > 10**11
            else int(raw["time_played"] or 0)
        ),
    )


async def fetch_score_history(user_id: int, chart, limit: int = 1000):
    from .data_source import ChartHistory, PlayRecord

    raw = await _get(
        f"/bot/users/{user_id}/score-history",
        {"song_id": chart.song_id, "difficulty": chart.difficulty_index, "limit": limit},
    )
    records = [
        PlayRecord(
            time_played=int(item["time_played"]),
            score=int(item["score"] or 0),
            rating=float(item["rating"] or 0.0),
            clear_type=int(item["clear_type"] or 0),
            perfect=int(item["perfect_count"] or 0),
            critical_perfect=min(int(item["shiny_perfect_count"] or 0), int(item["perfect_count"] or 0)),
            far=int(item["near_count"] or 0),
            lost=int(item["miss_count"] or 0),
        )
        for item in raw.get("data", [])
        if int(item.get("score") or 0) > 0
    ]
    if not records:
        return None
    return ChartHistory(
        chart=chart,
        records=records,
        best_score=max(r.score for r in records),
        best_rating=max(r.rating for r in records),
    )


async def fetch_rating_history(user_id: int, duration: int | None = None):
    params = {"duration": duration} if duration else {}
    raw = await _get(f"/users/{user_id}/rating", params)
    items = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    history = []
    for item in items:
        if isinstance(item, dict):
            ts = int(item.get("time") or 0)
            value = float(item.get("rating_ptt") or 0.0)
        else:
            ts, value = int(item[0]), float(item[1])
        if ts <= 0:
            continue
        if ts < 10**11:
            ts *= 1000
        if value >= 20:
            value /= 100
        history.append((ts, value))
    history.sort(key=lambda point: point[0])
    return history


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def bind_request(user_code: str, qq_id: str):
    return await _post("/bot/bind/request", {"user_code": user_code, "qq_id": qq_id})


async def bind_status(user_code: str, qq_id: str):
    return await _get("/bot/bind/status", {"user_code": user_code, "qq_id": qq_id})


async def bind_remove(user_code: str, qq_id: str):
    return await _post("/bot/bind/remove", {"user_code": user_code, "qq_id": qq_id})
