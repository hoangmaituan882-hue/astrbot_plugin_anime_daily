"""调度器:23:00 触发每日分析+推送;启动时检测昨日未推则补推。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

try:
    from astrbot.api import logger
except Exception:  # 单元测试环境兜底
    logger = logging.getLogger("astrbot_plugin_anime_daily.scheduler")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

# 启动后检查昨日推送的等待秒数(给 AstrBot 自身初始化留时间)
STARTUP_BACKFILL_DELAY = 30


def _now() -> datetime:
    return datetime.now()


def _seconds_until_next(hour: int, minute: int) -> float:
    """计算距离下一次指定时刻的秒数(若已过则取明天的)。"""
    now = _now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _yesterday_str() -> str:
    return (_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


class DailyScheduler:
    """异步定时器:每日推送 + 启动补推。"""

    def __init__(
        self,
        *,
        push_hour: int,
        push_minute: int,
        job: Callable[[str], Awaitable[None]],
    ) -> None:
        self.push_hour = push_hour
        self.push_minute = push_minute
        self._job = job
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"[anime_daily] scheduler started, daily push at "
            f"{self.push_hour:02d}:{self.push_minute:02d}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        try:
            # 启动补推(给 AstrBot 一点时间初始化)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=STARTUP_BACKFILL_DELAY
                )
                return  # 被要求停止
            except asyncio.TimeoutError:
                pass

            await self._safe_run_backfill()

            # 主循环
            while not self._stop_event.is_set():
                wait_sec = _seconds_until_next(
                    self.push_hour, self.push_minute
                )
                # 分段 sleep 以便快速响应 stop
                sleep_chunk = 30.0
                slept = 0.0
                while slept < wait_sec and not self._stop_event.is_set():
                    step = min(sleep_chunk, wait_sec - slept)
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=step
                        )
                        return  # 停止
                    except asyncio.TimeoutError:
                        slept += step

                if self._stop_event.is_set():
                    return

                date_str = _yesterday_str()
                logger.info(
                    f"[anime_daily] daily push triggered for {date_str}"
                )
                try:
                    await self._job(date_str)
                except Exception as e:
                    logger.error(
                        f"[anime_daily] daily job failed: {e}", exc_info=True
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[anime_daily] scheduler crashed: {e}", exc_info=True)

    async def _safe_run_backfill(self) -> None:
        """启动时检测昨日是否推送过,若没推则补推一次。"""
        date_str = _yesterday_str()
        try:
            await self._job(date_str, backfill=True)
        except Exception as e:
            logger.error(
                f"[anime_daily] startup backfill failed: {e}", exc_info=True
            )
