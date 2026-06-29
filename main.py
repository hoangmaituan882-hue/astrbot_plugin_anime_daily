"""astrbot_plugin_anime_daily

每天 23:00 自动汇总当日群内动画话题,生成话痨榜与热门作品榜,并推送全服总榜。
详细设计见:docs.astrbot.app/dev/star/plugin-new.html(开发指南第 5~7、12、13 章)。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, register

from .aggregator import chunk_messages, merge_global_results
from .classifier import aggregate_global, analyze_group_today
from .config import PluginConfig
from .renderer import (
    render_empty,
    render_error,
    render_global_report,
    render_group_report,
    render_user_record,
)
from .scheduler import DailyScheduler
from .storage import Storage, get_db_path


@register(
    "astrbot_plugin_anime_daily",
    "hoangmaituan882-hue",
    "每天 23:00 自动汇总群内动画话题,生成话痨榜与作品榜。",
    "1.0.0",
    "https://github.com/hoangmaituan882-hue/astrbot_plugin_anime_daily",
)
class AnimeDailyPlugin(Star):
    """每日动画话题总结插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig.from_raw(dict(config))
        self._llm_sem: asyncio.Semaphore | None = None
        self._analyzing_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.storage: Storage | None = None
        self.scheduler: DailyScheduler | None = None

    # ============== 初始化与生命周期(B8) ==============

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """B8:统一初始化路径。仅在 AstrBot 启动完成时跑一次。"""
        async with self._init_lock:
            if self._initialized:
                return
            try:
                db_path = get_db_path(self._resolve_data_dir())
                self.storage = Storage(db_path)
                await self.storage.init()
                # B12:LLM 并发上限
                self._llm_sem = asyncio.Semaphore(
                    max(1, self.cfg.max_concurrent_llm)
                )
                hh, mm = self.cfg.get_push_hour_minute()
                self.scheduler = DailyScheduler(
                    push_hour=hh,
                    push_minute=mm,
                    job=self._daily_job,
                )
                self.scheduler.start()
                self._initialized = True
            except Exception as e:
                logger.error(
                    f"[anime_daily] on_astrbot_loaded failed: {e}",
                    exc_info=True,
                )

    @filter.on_config_updated()
    async def on_config_updated(self) -> None:
        """B9:配置变更时热重载。

        - 重新解析 cfg
        - 重启 scheduler(应用新的 push_time)
        - 重新构造 LLM 信号量
        """
        try:
            # 重新从当前 config 构造
            self.cfg = PluginConfig.from_raw(dict(self.config))
            self._llm_sem = asyncio.Semaphore(
                max(1, self.cfg.max_concurrent_llm)
            )
            if self.scheduler:
                await self.scheduler.stop()
            hh, mm = self.cfg.get_push_hour_minute()
            self.scheduler = DailyScheduler(
                push_hour=hh,
                push_minute=mm,
                job=self._daily_job,
            )
            self.scheduler.start()
            logger.info("[anime_daily] config reloaded")
        except Exception as e:
            logger.error(
                f"[anime_daily] on_config_updated failed: {e}", exc_info=True
            )

    async def terminate(self) -> None:
        if self.scheduler:
            await self.scheduler.stop()
        if self.storage:
            await self.storage.close()

    def _resolve_data_dir(self) -> str:
        """B14:解析本插件数据目录。

        优先使用 Star 提供的 get_data_dir(若可用),否则用 AstrBot 全局 data_dir,
        兜底用当前工作目录下的 data。
        """
        # Star 基类(新版本 AstrBot)提供 get_data_dir
        try:
            data_dir = self.get_data_dir()  # type: ignore[attr-defined]
            if data_dir:
                return str(data_dir)
        except Exception:
            pass
        try:
            data_dir = self.context.get_config().get("data_dir")
            if data_dir:
                return str(data_dir)
        except Exception:
            pass
        return "data"

    # ============== 消息采集 ==============

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: Any) -> None:
        """静默监听群消息,落库。不发送任何消息。"""
        if not self._initialized or self.storage is None:
            return  # 还没初始化完,直接丢弃
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            if not self.cfg.is_group_enabled(group_id):
                return
            text = (event.message_str or "").strip()
            if len(text) < self.cfg.quiet_min_words:
                return
            now_ts = int(datetime.now().timestamp())
            # B1:同时存 umo,后续推送用
            umo = getattr(event, "unified_msg_origin", None)
            await self.storage.insert_message(
                date_str=datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d"),
                group_id=group_id,
                group_name=getattr(event.message_obj, "group_id", None)
                or group_id,
                user_id=event.get_sender_id() or "",
                user_name=event.get_sender_name(),
                message_id=event.message_obj.message_id
                if event.message_obj
                else None,
                raw_text=text,
                umo=umo,
                created_at=now_ts,
            )
        except Exception as e:
            logger.error(
                f"[anime_daily] on_group_message failed: {e}", exc_info=True
            )

    # ============== 每日任务 ==============

    async def _daily_job(self, date_str: str, backfill: bool = False) -> None:
        """每日推送主流程:阶段一(每群) + 阶段二(跨群汇总)。"""
        if self.storage is None or self._llm_sem is None:
            logger.warning("[anime_daily] daily job: not initialized yet")
            return
        if self._analyzing_lock.locked():
            logger.warning(
                f"[anime_daily] daily job for {date_str} skipped: previous still running"
            )
            return
        async with self._analyzing_lock:
            try:
                await self._run_daily(date_str, backfill=backfill)
            except Exception as e:
                logger.error(
                    f"[anime_daily] _daily_job({date_str}) crashed: {e}",
                    exc_info=True,
                )

    async def _run_daily(self, date_str: str, backfill: bool) -> None:
        provider = self.context.get_using_provider()
        if provider is None:
            logger.error(
                "[anime_daily] no LLM provider configured, skip daily push"
            )
            return
        assert self.storage is not None  # type narrowing
        storage = self.storage
        sem = self._llm_sem
        assert sem is not None

        # 拉取当日所有消息
        msgs_by_group = await storage.get_messages_by_group(date_str)
        all_msgs = await storage.get_all_messages(date_str)

        # 阶段一:逐群分析
        group_analyses: dict[str, dict | None] = {}
        for gid, msgs in msgs_by_group.items():
            # 推过则跳过(backfill 模式除外)
            if not backfill and await storage.has_pushed(
                date_str, gid, "group"
            ):
                cached = await storage.get_analysis_cache(
                    date_str, f"group:{gid}"
                )
                group_analyses[gid] = cached if cached else None
                continue

            if not msgs:
                continue
            gname = msgs[0].get("group_name") or gid
            chunks = chunk_messages(msgs, self.cfg.max_messages_per_llm_call)
            analysis = await analyze_group_today(
                provider,
                group_id=gid,
                group_name=gname,
                date=date_str,
                chunks=chunks,
                temperature=self.cfg.llm_temperature,
                sem=sem,
            )
            group_analyses[gid] = analysis
            if analysis is not None:
                payload = {
                    "group_id": gid,
                    "group_name": gname,
                    **analysis,
                }
                await storage.save_analysis_cache(
                    date_str, f"group:{gid}", payload
                )

        # 推送各群(B10:拆 error / empty 两种语义)
        all_enabled_gids = set(self.cfg.enabled_groups) or set(
            msgs_by_group.keys()
        )
        successful_groups: list[dict] = []

        for gid, analysis in group_analyses.items():
            gname = (
                msgs_by_group[gid][0].get("group_name")
                if msgs_by_group.get(gid)
                else gid
            )
            try:
                if analysis is None:
                    # B22:None 语义 = 所有 chunk 全部失败 → 推 error(独立开关)
                    if self.cfg.push_on_error and msgs_by_group.get(gid):
                        await self._safe_push(
                            gid, render_error(date_str, gid, gname)
                        )
                        if not backfill:
                            await storage.mark_pushed(
                                date_str, gid, "error"
                            )
                    continue
                if not analysis.get("is_anime_day"):
                    if self.cfg.push_on_empty:
                        await self._safe_push(
                            gid, render_empty(date_str, gid, gname)
                        )
                        if not backfill:
                            await storage.mark_pushed(
                                date_str, gid, "empty"
                            )
                    continue
                text = render_group_report(
                    date_str=date_str,
                    group_id=gid,
                    group_name=gname,
                    analysis=analysis,
                    top_n_users=self.cfg.top_n_users,
                    top_n_works=self.cfg.top_n_works,
                    summary_max_words=self.cfg.summary_max_words,
                )
                await self._safe_push(gid, text)
                if not backfill:
                    # B11:把"落缓存 + 记推送"放到一个事务里
                    payload = {
                        "group_id": gid,
                        "group_name": gname,
                        **analysis,
                    }
                    await storage.commit_push(
                        date_str=date_str,
                        group_id=gid,
                        kind="group",
                        analysis_payload=payload,
                        scope=f"group:{gid}",
                    )
                successful_groups.append(
                    {"group_id": gid, "group_name": gname, **analysis}
                )
            except Exception as e:
                logger.error(
                    f"[anime_daily] push to group {gid} failed: {e}",
                    exc_info=True,
                )

        # 阶段二:跨群汇总
        if (
            self.cfg.include_global_in_group
            and successful_groups
            and all_msgs
        ):
            try:
                global_result = await aggregate_global(
                    provider,
                    date=date_str,
                    successful_groups=successful_groups,
                    temperature=self.cfg.llm_temperature,
                    sem=sem,
                )
            except Exception as e:
                logger.error(
                    f"[anime_daily] aggregate_global LLM failed: {e}",
                    exc_info=True,
                )
                global_result = None

            if global_result is None:
                # 降级:本地合并(同样跨群合并 user,口径与 LLM 一致)
                logger.info(
                    "[anime_daily] global LLM failed, fallback to local merge"
                )
                global_result = merge_global_results(successful_groups)

            await storage.save_analysis_cache(
                date_str, "global", global_result
            )
            text = render_global_report(
                date_str=date_str,
                global_result=global_result,
                top_n_users=self.cfg.top_n_global_users,
                top_n_works=self.cfg.top_n_global_works,
                summary_max_words=self.cfg.summary_max_words,
            )
            for gid in all_enabled_gids:
                try:
                    if not backfill and await storage.has_pushed(
                        date_str, gid, "global"
                    ):
                        continue
                    await self._safe_push(gid, text)
                    if not backfill:
                        await storage.mark_pushed(
                            date_str, gid, "global"
                        )
                except Exception as e:
                    logger.error(
                        f"[anime_daily] global push to {gid} failed: {e}",
                        exc_info=True,
                    )

    async def _safe_push(self, group_id: str, text: str) -> None:
        """B1:用该群最近一条消息的 umo 推送;umo 缺失时降级为日志告警。"""
        assert self.storage is not None
        try:
            umo = await self.storage.get_latest_umo(group_id)
            if not umo:
                logger.warning(
                    f"[anime_daily] _safe_push({group_id}): no umo cached, "
                    f"skip (text length={len(text)})"
                )
                return
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(
                f"[anime_daily] send_message({group_id}) failed: {e}; "
                f"text length={len(text)}"
            )

    # ============== 查询指令 ==============

    @filter.command("anime")
    async def anime_command(
        self,
        event: Any,
        action: str = "today",
        target: str = "",
    ):
        """/anime today | group <日期> | user <user_id> [日期] | global [日期] | preview"""
        if self.storage is None:
            yield event.plain_result("插件尚未初始化完成,请稍后再试。")
            return
        storage = self.storage
        try:
            group_id = event.get_group_id() or ""
            today = datetime.now().strftime("%Y-%m-%d")

            if action == "today":
                payload = await storage.get_analysis_cache(
                    today, f"group:{group_id}"
                )
                if not payload:
                    yield event.plain_result("今日暂无分析结果(可能还没到 23:00)。")
                    return
                if not payload.get("is_anime_day"):
                    yield event.plain_result(
                        render_empty(today, group_id, payload.get("group_name"))
                    )
                    return
                yield event.plain_result(
                    render_group_report(
                        date_str=today,
                        group_id=group_id,
                        group_name=payload.get("group_name") or group_id,
                        analysis=payload,
                        top_n_users=self.cfg.top_n_users,
                        top_n_works=self.cfg.top_n_works,
                        summary_max_words=self.cfg.summary_max_words,
                    )
                )
                return

            if action == "group":
                date_str = self._parse_date(target) or today
                payload = await storage.get_analysis_cache(
                    date_str, f"group:{group_id}"
                )
                if not payload:
                    yield event.plain_result(f"{date_str} 本群无分析结果。")
                    return
                if not payload.get("is_anime_day"):
                    yield event.plain_result(
                        render_empty(
                            date_str, group_id, payload.get("group_name")
                        )
                    )
                    return
                yield event.plain_result(
                    render_group_report(
                        date_str=date_str,
                        group_id=group_id,
                        group_name=payload.get("group_name") or group_id,
                        analysis=payload,
                        top_n_users=self.cfg.top_n_users,
                        top_n_works=self.cfg.top_n_works,
                        summary_max_words=self.cfg.summary_max_words,
                    )
                )
                return

            if action == "user":
                m = re.match(r"^(\S+)(?:\s+(\S+))?$", target.strip())
                if not m:
                    yield event.plain_result(
                        "用法: /anime user <user_id> [日期 YYYY-MM-DD]"
                    )
                    return
                uid = m.group(1)
                date_str = self._parse_date(m.group(2)) if m.group(2) else None
                rows = await storage.get_user_messages(uid, date_str)
                yield event.plain_result(
                    render_user_record(uid, rows, date_str=date_str)
                )
                return

            if action == "global":
                date_str = self._parse_date(target) or today
                payload = await storage.get_analysis_cache(
                    date_str, "global"
                )
                if not payload:
                    yield event.plain_result(f"{date_str} 无全服总榜。")
                    return
                yield event.plain_result(
                    render_global_report(
                        date_str=date_str,
                        global_result=payload,
                        top_n_users=self.cfg.top_n_global_users,
                        top_n_works=self.cfg.top_n_global_works,
                        summary_max_words=self.cfg.summary_max_words,
                    )
                )
                return

            if action == "preview":
                # 立刻跑一次今日分析,只发送给触发者
                yield event.plain_result("⏳ 正在分析今日消息,请稍候...")
                asyncio.create_task(self._run_preview(today, group_id, event))
                return

            yield event.plain_result(
                "用法:\n"
                "/anime today\n"
                "/anime group <日期>\n"
                "/anime user <user_id> [日期]\n"
                "/anime global [日期]\n"
                "/anime preview"
            )
        except Exception as e:
            logger.error(
                f"[anime_daily] /anime command failed: {e}", exc_info=True
            )
            yield event.plain_result(f"查询失败: {e}")

    async def _run_preview(
        self, date_str: str, group_id: str, event: Any
    ) -> None:
        try:
            if self.storage is None or self._llm_sem is None:
                await event.send("插件尚未初始化完成。")
                return
            provider = self.context.get_using_provider()
            if provider is None:
                await event.send("无 LLM provider,无法 preview。")
                return
            msgs_by_group = await self.storage.get_messages_by_group(date_str)
            msgs = msgs_by_group.get(group_id, [])
            if not msgs:
                await event.send("今日该群无消息可分析。")
                return
            gname = msgs[0].get("group_name") or group_id
            chunks = chunk_messages(msgs, self.cfg.max_messages_per_llm_call)
            analysis = await analyze_group_today(
                provider,
                group_id=group_id,
                group_name=gname,
                date=date_str,
                chunks=chunks,
                temperature=self.cfg.llm_temperature,
                sem=self._llm_sem,
            )
            if analysis is None:
                await event.send(render_error(date_str, group_id, gname))
                return
            if not analysis.get("is_anime_day"):
                await event.send(render_empty(date_str, group_id, gname))
                return
            text = render_group_report(
                date_str=date_str,
                group_id=group_id,
                group_name=gname,
                analysis=analysis,
                top_n_users=self.cfg.top_n_users,
                top_n_works=self.cfg.top_n_works,
                summary_max_words=self.cfg.summary_max_words,
            )
            await event.send(text)
        except Exception as e:
            logger.error(f"[anime_daily] preview failed: {e}", exc_info=True)
            try:
                await event.send(f"Preview 失败: {e}")
            except Exception:
                pass

    @staticmethod
    def _parse_date(s: str) -> str | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            return None
