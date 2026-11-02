"""QQ 用户 ↔ Arcaea 账号绑定（sqlite）。"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import settings


def _connect() -> sqlite3.Connection:
    path = str(settings.get("bind_db", "binds.db"))
    Path_ = __import__("pathlib").Path(path)
    Path_.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "create table if not exists binds("
        "qq_id text primary key, user_id int, user_code text, name text)"
    )
    return conn


async def get_bind(qq_id: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                "select user_id, user_code, name from binds where qq_id = ?",
                (qq_id,),
            ).fetchone()
        return (
            {"user_id": row[0], "user_code": row[1], "name": row[2]}
            if row
            else None
        )

    import asyncio

    return await asyncio.to_thread(_run)


async def set_bind(qq_id: str, user_id: int, user_code: str, name: str) -> None:
    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "insert into binds(qq_id, user_id, user_code, name) "
                "values(?,?,?,?) on conflict(qq_id) do update set "
                "user_id=excluded.user_id, user_code=excluded.user_code, "
                "name=excluded.name",
                (qq_id, user_id, user_code, name),
            )
            conn.commit()

    import asyncio

    await asyncio.to_thread(_run)


async def del_bind(qq_id: str) -> None:
    def _run() -> None:
        with _connect() as conn:
            conn.execute("delete from binds where qq_id = ?", (qq_id,))
            conn.commit()

    import asyncio

    await asyncio.to_thread(_run)


async def get_bind_by_code(user_code: str) -> dict[str, Any] | None:
    import asyncio

    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                "select qq_id, user_id, name from binds where user_code = ?",
                (user_code,),
            ).fetchone()
        return (
            {"qq_id": row[0], "user_id": row[1], "name": row[2]}
            if row
            else None
        )

    return await asyncio.to_thread(_run)
