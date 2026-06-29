"""SQLite 异步封装:消息存储、查询、分析缓存、推送日志。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiosqlite

DB_DIRNAME = "astrbot_plugin_anime_daily"
DB_FILENAME = "anime.db"


def get_db_path(plugin_data_dir: str | os.PathLike) -> str:
    """根据 AstrBot 的 data 目录解析出本插件的 db 绝对路径。

    plugin_data_dir 通常由调用方从 Star 上下文或配置中获取。
    """
    p = Path(plugin_data_dir) / DB_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return str(p / DB_FILENAME)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_str TEXT NOT NULL,
    group_id TEXT NOT NULL,
    group_name TEXT,
    user_id TEXT NOT NULL,
    user_name TEXT,
    message_id TEXT,
    raw_text TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_date_group ON messages(date_str, group_id);
CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, date_str);

CREATE TABLE IF NOT EXISTS analysis_cache (
    date_str TEXT NOT NULL,
    scope TEXT NOT NULL,         -- 'group:<group_id>' 或 'global'
    payload_json TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    PRIMARY KEY (date_str, scope)
);

CREATE TABLE IF NOT EXISTS push_log (
    date_str TEXT NOT NULL,
    group_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'group' | 'global' | 'empty' | 'error'
    pushed_at INTEGER NOT NULL,
    PRIMARY KEY (date_str, group_id, kind)
);
"""


class Storage:
    """异步 SQLite 存储封装。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._initialized = False

    async def init(self) -> None:
        """初始化表结构(WAL 模式 + 串行写入友好)。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        self._initialized = True

    async def insert_message(
        self,
        *,
        date_str: str,
        group_id: str,
        group_name: str | None,
        user_id: str,
        user_name: str | None,
        message_id: str | None,
        raw_text: str,
        created_at: int,
    ) -> None:
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO messages
                   (date_str, group_id, group_name, user_id, user_name,
                    message_id, raw_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date_str,
                    group_id,
                    group_name,
                    user_id,
                    user_name,
                    message_id,
                    raw_text,
                    created_at,
                ),
            )
            await db.commit()

    async def get_messages_by_group(
        self, date_str: str
    ) -> dict[str, list[dict]]:
        """返回 {group_id: [msg_dict, ...]},每组内按 created_at 升序。"""
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT group_id, group_name, user_id, user_name,
                          message_id, raw_text, created_at
                   FROM messages
                   WHERE date_str = ?
                   ORDER BY group_id, created_at ASC""",
                (date_str,),
            )
            rows = await cur.fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["group_id"], []).append(
                {
                    "date_str": date_str,
                    "group_id": r["group_id"],
                    "group_name": r["group_name"],
                    "user_id": r["user_id"],
                    "user_name": r["user_name"],
                    "message_id": r["message_id"],
                    "raw_text": r["raw_text"],
                    "created_at": r["created_at"],
                }
            )
        return out

    async def get_all_messages(self, date_str: str) -> list[dict]:
        """跨群所有消息,按 created_at 升序。"""
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT group_id, group_name, user_id, user_name,
                          message_id, raw_text, created_at
                   FROM messages
                   WHERE date_str = ?
                   ORDER BY created_at ASC""",
                (date_str,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def save_analysis_cache(
        self, date_str: str, scope: str, payload: dict
    ) -> None:
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO analysis_cache
                   (date_str, scope, payload_json, generated_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    date_str,
                    scope,
                    json.dumps(payload, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            await db.commit()

    async def get_analysis_cache(
        self, date_str: str, scope: str
    ) -> dict | None:
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT payload_json FROM analysis_cache
                   WHERE date_str = ? AND scope = ?""",
                (date_str, scope),
            )
            row = await cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except Exception:
            return None

    async def has_pushed(
        self, date_str: str, group_id: str, kind: str
    ) -> bool:
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """SELECT 1 FROM push_log
                   WHERE date_str = ? AND group_id = ? AND kind = ?
                   LIMIT 1""",
                (date_str, group_id, kind),
            )
            row = await cur.fetchone()
        return row is not None

    async def mark_pushed(
        self, date_str: str, group_id: str, kind: str
    ) -> None:
        await self._ensure()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO push_log
                   (date_str, group_id, kind, pushed_at)
                   VALUES (?, ?, ?, ?)""",
                (date_str, group_id, kind, int(time.time())),
            )
            await db.commit()

    async def get_user_messages(
        self, user_id: str, date_str: str | None = None
    ) -> list[dict]:
        """查询某用户的发言记录,可选按日期过滤。"""
        await self._ensure()
        sql = """SELECT date_str, group_id, group_name, user_name,
                        raw_text, created_at
                 FROM messages
                 WHERE user_id = ?"""
        params: tuple = (user_id,)
        if date_str:
            sql += " AND date_str = ?"
            params = (user_id, date_str)
        sql += " ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _ensure(self) -> None:
        if not self._initialized:
            await self.init()
