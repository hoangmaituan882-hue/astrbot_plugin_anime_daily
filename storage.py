"""SQLite 异步封装:消息存储、查询、分析缓存、推送日志。

设计要点(B7):
- 全部异步操作走**单例连接** + asyncio.Lock,避免每条消息开/关连接;
  WAL 模式下多读单写足够,串行化写不会拖慢日常场景(每日千条以内)。
- init() 之后所有方法通过 _lock 串行访问同一个连接,适合小并发场景。
"""
from __future__ import annotations

import asyncio
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
    umo TEXT,                                -- unified_msg_origin,用于推送(B1)
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_date_group ON messages(date_str, group_id);
CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, date_str);
CREATE INDEX IF NOT EXISTS idx_msg_group_umo ON messages(group_id, id DESC);

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
    """异步 SQLite 存储封装:单连接 + 写锁串行化。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """初始化表结构(WAL 模式 + 单连接 + 写锁)。"""
        async with self._lock:
            if self._conn is not None:
                return
            conn = await aiosqlite.connect(self.db_path)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()
            self._conn = conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.init()
        assert self._conn is not None
        return self._conn

    # ============== 写 ==============

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
        umo: str | None = None,
        created_at: int,
    ) -> None:
        conn = await self._ensure()
        async with self._lock:
            await conn.execute(
                """INSERT INTO messages
                   (date_str, group_id, group_name, user_id, user_name,
                    message_id, raw_text, umo, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date_str,
                    group_id,
                    group_name,
                    user_id,
                    user_name,
                    message_id,
                    raw_text,
                    umo,
                    created_at,
                ),
            )
            await conn.commit()

    async def save_analysis_cache(
        self, date_str: str, scope: str, payload: dict
    ) -> None:
        conn = await self._ensure()
        async with self._lock:
            await conn.execute(
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
            await conn.commit()

    async def mark_pushed(
        self, date_str: str, group_id: str, kind: str
    ) -> None:
        conn = await self._ensure()
        async with self._lock:
            await conn.execute(
                """INSERT OR IGNORE INTO push_log
                   (date_str, group_id, kind, pushed_at)
                   VALUES (?, ?, ?, ?)""",
                (date_str, group_id, kind, int(time.time())),
            )
            await conn.commit()

    async def commit_push(
        self,
        date_str: str,
        group_id: str,
        kind: str,
        analysis_payload: dict | None = None,
        scope: str | None = None,
    ) -> None:
        """B11:把 save_cache + mark_pushed 包到一个事务里。

        任意一步失败则整体回滚,避免出现"分析已落库但没记推送"或反之的不一致。
        """
        conn = await self._ensure()
        async with self._lock:
            try:
                if analysis_payload is not None and scope is not None:
                    await conn.execute(
                        """INSERT OR REPLACE INTO analysis_cache
                           (date_str, scope, payload_json, generated_at)
                           VALUES (?, ?, ?, ?)""",
                        (
                            date_str,
                            scope,
                            json.dumps(analysis_payload, ensure_ascii=False),
                            int(time.time()),
                        ),
                    )
                await conn.execute(
                    """INSERT OR IGNORE INTO push_log
                       (date_str, group_id, kind, pushed_at)
                       VALUES (?, ?, ?, ?)""",
                    (date_str, group_id, kind, int(time.time())),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    # ============== 读 ==============

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = await self._ensure()
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        conn = await self._ensure()
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_messages_by_group(
        self, date_str: str
    ) -> dict[str, list[dict]]:
        """返回 {group_id: [msg_dict, ...]},每组内按 created_at 升序。"""
        rows = await self._fetchall(
            """SELECT group_id, group_name, user_id, user_name,
                      message_id, raw_text, umo, created_at
               FROM messages
               WHERE date_str = ?
               ORDER BY group_id, created_at ASC""",
            (date_str,),
        )
        out: dict[str, list[dict]] = {}
        for r in rows:
            r["date_str"] = date_str
            out.setdefault(r["group_id"], []).append(r)
        return out

    async def get_all_messages(self, date_str: str) -> list[dict]:
        return await self._fetchall(
            """SELECT group_id, group_name, user_id, user_name,
                      message_id, raw_text, umo, created_at
               FROM messages
               WHERE date_str = ?
               ORDER BY created_at ASC""",
            (date_str,),
        )

    async def get_latest_umo(self, group_id: str) -> str | None:
        """B1:获取某群最近一条消息的 unified_msg_origin(用于推送)。"""
        conn = await self._ensure()
        cur = await conn.execute(
            """SELECT umo FROM messages
               WHERE group_id = ? AND umo IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (group_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def get_analysis_cache(
        self, date_str: str, scope: str
    ) -> dict | None:
        row = await self._fetchone(
            """SELECT payload_json FROM analysis_cache
               WHERE date_str = ? AND scope = ?""",
            (date_str, scope),
        )
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except Exception:
            return None

    async def has_pushed(
        self, date_str: str, group_id: str, kind: str
    ) -> bool:
        conn = await self._ensure()
        cur = await conn.execute(
            """SELECT 1 FROM push_log
               WHERE date_str = ? AND group_id = ? AND kind = ?
               LIMIT 1""",
            (date_str, group_id, kind),
        )
        return (await cur.fetchone()) is not None

    async def get_user_messages(
        self, user_id: str, date_str: str | None = None
    ) -> list[dict]:
        """查询某用户的发言记录,可选按日期过滤。"""
        if date_str:
            return await self._fetchall(
                """SELECT date_str, group_id, group_name, user_name,
                          raw_text, created_at
                   FROM messages
                   WHERE user_id = ? AND date_str = ?
                   ORDER BY created_at ASC""",
                (user_id, date_str),
            )
        return await self._fetchall(
            """SELECT date_str, group_id, group_name, user_name,
                      raw_text, created_at
               FROM messages
               WHERE user_id = ?
               ORDER BY created_at ASC""",
            (user_id,),
        )
