"""插件配置：全部来自环境变量 / .env（nonebot Config）。"""

from __future__ import annotations

from pathlib import Path

import os

BASE_DIR = Path(__file__).resolve().parent
RESOURCE_PATH = BASE_DIR / "resources"
DATA_PATH = RESOURCE_PATH / "data"
JACKET_PATH = RESOURCE_PATH / "jackets"
RANK_PATH = RESOURCE_PATH / "rank"
BG_PATH = RESOURCE_PATH / "bg"


def get(key: str, default=None):
    value = os.getenv(f"ARCAEA_{key.upper()}")
    return default if value is None else value


def as_int(key, default, minimum=None, maximum=None):
    v = _as_num(get(key, default), default)
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return int(v)


def as_float(key, default, minimum=None, maximum=None):
    v = _as_num(get(key, default), default)
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return float(v)


def as_bool(key, default):
    v = get(key, default)
    if isinstance(v, str):
        return v.strip().lower() not in {"", "false", "0", "off", "no"}
    if v is None:
        return default
    return bool(v)


def _as_num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# 兼容从 zhenxun 版复制来的 data_source.py 的取值接口
def get_db_path() -> Path:  # pragma: no cover - data_source 的 DB 函数不再使用
    return Path("")


def get_log_db_path() -> Path:  # pragma: no cover
    return Path("")


def get_b50_count() -> int:
    return as_int("b50_count", 50, minimum=1)


def get_completion_bounds() -> tuple[float, float]:
    lo = as_float("completion_min", 9.0, 0.0, 13.5)
    hi = as_float("completion_max", 12.0, 0.0, 13.5)
    return min(lo, hi), max(lo, hi)


def get_completion_columns() -> int:
    return as_int("completion_columns", 8, 4, 8)


def get_completion_show_no_record() -> bool:
    return as_bool("completion_show_no_record", True)


def get_completion_max_charts() -> int:
    return as_int("completion_max_charts", 2200, 50)


def get_image_quality() -> int:
    return as_int("image_quality", 90, 60, 100)


def get_output_width() -> int:
    return as_int("output_width", 1280, 680, 1700)


def get_max_image_height() -> int:
    return as_int("max_image_height", 18000, 4000)


def get_background_name() -> str:
    return str(get("background", "s9.webp") or "s9.webp")


def get_score_top_n() -> int:
    return as_int("score_top_n", 10, 1, 20)
