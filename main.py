"""astrbot_plugin_anime_daily

每天 23:00 自动汇总当日群内动画话题,生成话痨榜与热门作品榜,并推送全服总榜。
详细设计见:docs.astrbot.app/dev/star/plugin-new.html(开发指南第 5~7、12、13 章)。

版本: 1.0.1 — AstrBot 框架版本兼容加固
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, register

from .aggregator import chunk_messages, merge_global_results
from .classifier import aggregate_global, analyze_group_today
from .config import PluginConfig
from .html_renderer import (
    render_global_html,
    render_group_html,
    save_html_to_file,
)
from .renderer import (
    render_empty,
    render_error,
    render_global_report,
    render_group_report,
    render_user_record,
)
from .scheduler import DailyScheduler
from .storage import Storage, get_db_path
from .test_harness import (
    FakeLLMProvider,
    ScenarioResult,
    make_chunk_invalid_payload,
    make_chunk_success_payload,
    make_global_success_payload,
    make_test_date,
    make_test_group_id,
    make_test_umo,
)

# 版本横幅:加载时打印,用户可在 AstrBot 日志确认当前加载的是哪个版本
PLUGIN_VERSION = "1.0.1"
logger.info(
    f"[anime_daily] loading plugin version {PLUGIN_VERSION} "
    f"(defensive filter compatibility)"
)


def _safe_filter(name: str):
    """鲁棒获取 filter 装饰器:框架版本不支持时降级为 identity(不装饰)。

    如果 AstrBot 框架版本缺少某个 filter 装饰器,插件加载会直接抛
    AttributeError,整个插件不可用。通过这个包装,任何缺失的装饰器都被
    替换为 no-op,插件可以正常加载,缺失的钩子功能只是不生效。
    """
    obj = getattr(filter, name, None)
    if obj is None:
        logger.warning(
            f"[anime_daily] filter.{name} not available in this AstrBot "
            f"version; using no-op decorator"
        )

        def _noop(*_args, **_kwargs):
            def _decorator(func):
                return func
            return _decorator

        return _noop
    return obj


def _resolve_html_output_dir(plugin: "AnimeDailyPlugin") -> Path:
    """L4:解析 HTML 报告输出目录。

    优先 <data_dir>/astrbot_plugin_anime_daily/html_reports,
    创建失败兜底到 <cwd>/html_reports。
    """
    try:
        base = Path(plugin._resolve_data_dir())  # type: ignore[attr-defined]
    except Exception:
        base = Path("data")
    out = base / "astrbot_plugin_anime_daily" / "html_reports"
    try:
        out.mkdir(parents=True, exist_ok=True)
        return out.resolve()
    except Exception:
        fallback = Path("html_reports").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


@register(
    "astrbot_plugin_anime_daily",
    "hoangmaituan882-hue",
    "每天 23:00 自动汇总群内动画话题,生成话痨榜与作品榜。",
    "1.0.1",
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
        # test_harness 模块引用(供 /anime test 指令使用)
        from . import test_harness

        self.test_harness = test_harness

    # ============== 初始化与生命周期(B8) ==============

    @_safe_filter("on_astrbot_loaded")()
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

    def _refresh_config(self) -> None:
        """B9:从 self.config 重新解析 cfg(不依赖任何 AstrBot 钩子)。

        设计依据:AstrBotConfig 继承自 dict,框架在插件重载/重初始化时
        会注入新的 config 实例,而 self.config 一直指向最新值。
        """
        try:
            self.cfg = PluginConfig.from_raw(dict(self.config))
        except Exception as e:
            logger.warning(f"[anime_daily] _refresh_config failed: {e}")

    def _is_ready(self) -> bool:
        """检查插件是否完全就绪(storage / llm_sem / scheduler 都建好)。"""
        return (
            self._initialized
            and self.storage is not None
            and self._llm_sem is not None
            and self.scheduler is not None
        )

    @staticmethod
    def _not_ready_text() -> str:
        """用户调用指令但插件未就绪时的友好提示。"""
        return (
            "⏳ 插件正在初始化,请稍候片刻再试。\n"
            "💡 初始化在 AstrBot 启动后立即开始,通常 1~3 秒内完成。\n"
            "如果长时间仍报此提示,请检查 AstrBot 主日志(关键字 anime_daily)。\n"
            "📋 也可使用 /anime sid 查看会话 ID 是否被识别。"
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

    @_safe_filter("event_message_type")(filter.EventMessageType.GROUP_MESSAGE)
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
        # B9:执行前刷一次 cfg(用户改完配置下次定时任务生效)
        self._refresh_config()
        if not self._is_ready():
            logger.warning(
                f"[anime_daily] daily job for {date_str}: not initialized yet"
            )
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
                # L4:html 模式生成 .html 文件并发送
                if (
                    self.cfg.report_format == "html"
                    and self.cfg.html_send_as_file
                ):
                    html_str = render_group_html(
                        date_str=date_str,
                        group_id=gid,
                        group_name=gname,
                        analysis=analysis,
                        top_n_users=self.cfg.top_n_users,
                        top_n_works=self.cfg.top_n_works,
                        summary_max_words=self.cfg.summary_max_words,
                    )
                    out_path = save_html_to_file(
                        html_str,
                        _resolve_html_output_dir(self),
                        prefix="anime_group",
                    )
                    await self._safe_push_html(gid, out_path)
                else:
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
            # L4:html 模式预生成一次 HTML 文件,所有群共享
            global_html_path: Path | None = None
            if self.cfg.report_format == "html":
                html_str = render_global_html(
                    date_str=date_str,
                    global_result=global_result,
                    top_n_users=self.cfg.top_n_global_users,
                    top_n_works=self.cfg.top_n_global_works,
                    summary_max_words=self.cfg.summary_max_words,
                )
                global_html_path = save_html_to_file(
                    html_str,
                    _resolve_html_output_dir(self),
                    prefix="anime_global",
                )
            for gid in all_enabled_gids:
                try:
                    if not backfill and await storage.has_pushed(
                        date_str, gid, "global"
                    ):
                        continue
                    if (
                        self.cfg.report_format == "html"
                        and self.cfg.html_send_as_file
                        and global_html_path is not None
                    ):
                        # 直接发文件
                        await self._safe_push_html(gid, global_html_path)
                    else:
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
        """B1:用该群最近一条消息的 umo 推送;umo 缺失时降级为日志告警。

        仅负责 text 模式推送。HTML 模式请用 _safe_push_html。
        """
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

    async def _safe_push_html(self, group_id: str, html_path: "Path") -> None:
        """L4:发 .html 文件给某群;失败降级为文本提示。"""
        assert self.storage is not None
        try:
            umo = await self.storage.get_latest_umo(group_id)
            if not umo:
                logger.warning(
                    f"[anime_daily] _safe_push_html({group_id}): no umo cached, "
                    f"skip (path={html_path})"
                )
                return
            try:
                from astrbot.core.message.components import File
                chain = MessageChain(
                    [File(name=html_path.name, file=str(html_path))]
                )
                await self.context.send_message(umo, chain)
                return
            except Exception as e:
                logger.warning(
                    f"[anime_daily] html File send failed: {e}; "
                    f"fallback to text"
                )
            # 降级
            chain = MessageChain().message(
                f"📊 今日动画话题报告已生成(html):\n{html_path}"
            )
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(
                f"[anime_daily] _safe_push_html({group_id}) failed: {e}; "
                f"path={html_path}"
            )

    # ============== 查询指令 ==============

    @_safe_filter("command")("anime")
    async def anime_command(
        self,
        event: Any,
        action: str = "today",
        target: str = "",
    ):
        """/anime today | group <日期> | user <user_id> [日期] | global [日期] | preview"""
        if not self._is_ready():
            yield event.plain_result(self._not_ready_text())
            return
        storage = self.storage
        try:
            group_id = event.get_group_id() or ""
            today = datetime.now().strftime("%Y-%m-%d")

            if action in ("test", "t"):
                # 测试场景:用 fake provider + 测试群 ID 跑一次端到端流程
                scenario = (target or "").strip() or "all"
                yield event.plain_result(
                    f"🧪 启动测试场景: {scenario}\n"
                    f"提示: 测试会临时插入假消息到 db,跑完自动清理。\n"
                    f"场景数据使用 {self.test_harness.TEST_GROUP_PREFIX} 前缀群 ID,"
                    f"不会污染真实数据。"
                )
                report = await self._run_test_scenario(event, scenario)
                yield event.plain_result(report)
                return

            if action in ("help", "h", "?"):
                yield event.plain_result(self._build_help_text())
                return

            if action in ("reload", "r"):
                # 主动热重载:刷 cfg + 重启 scheduler(应用 push_time 等)
                self._refresh_config()
                if self.scheduler:
                    try:
                        await self.scheduler.stop()
                    except Exception:
                        pass
                hh, mm = self.cfg.get_push_hour_minute()
                self.scheduler = DailyScheduler(
                    push_hour=hh,
                    push_minute=mm,
                    job=self._daily_job,
                )
                self.scheduler.start()
                self._llm_sem = asyncio.Semaphore(
                    max(1, self.cfg.max_concurrent_llm)
                )
                yield event.plain_result(
                    f"🔄 已重载配置。下次推送时间: {self.cfg.push_time}\n"
                    f"格式: {self.cfg.report_format}  名单: {self.cfg.group_list_mode}"
                )
                return

            if action in ("sid", "id"):
                # L2:返回当前会话的 unified_msg_origin / platform / group_id
                umo = getattr(event, "unified_msg_origin", None) or "(空)"
                platform = getattr(event, "platform", None) or event.get_platform_name() if hasattr(event, "get_platform_name") else "?"
                gid = event.get_group_id() or "(私聊/无群)"
                self_id = getattr(event, "self_id", "") or ""
                msg = (
                    "🔖 当前会话标识\n"
                    f"• platform: {platform}\n"
                    f"• group_id: {gid}\n"
                    f"• self_id: {self_id}\n"
                    f"• unified_msg_origin: {umo}\n\n"
                    "💡 把 unified_msg_origin 复制到插件配置 enabled_groups 即可加入白名单。"
                )
                yield event.plain_result(msg)
                return

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

            if action == "now":
                # 立即对当前群生成今日总结(正常推送到群)
                yield event.plain_result("⏳ 正在生成本群今日总结,请稍候...")
                asyncio.create_task(
                    self._run_now(today, group_id, event)
                )
                return

            if action == "preview":
                # 立刻跑一次今日分析,只发送给触发者
                yield event.plain_result("⏳ 正在分析今日消息,请稍候...")
                asyncio.create_task(self._run_preview(today, group_id, event))
                return

            yield event.plain_result(self._build_help_text())
        except Exception as e:
            logger.error(
                f"[anime_daily] /anime command failed: {e}", exc_info=True
            )
            yield event.plain_result(f"查询失败: {e}")

    async def _run_preview(
        self, date_str: str, group_id: str, event: Any
    ) -> None:
        try:
            if not self._is_ready():
                await event.send(self._not_ready_text())
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

    async def _run_now(
        self, date_str: str, group_id: str, event: Any
    ) -> None:
        """/anime now: 立即对当前群生成今日总结,正常推送到群。"""
        try:
            if not self._is_ready():
                await event.send(self._not_ready_text())
                return
            if not group_id:
                await event.send("❌ 请在群聊中使用 /anime now")
                return
            if self.storage is None or self._llm_sem is None:
                await event.send(self._not_ready_text())
                return

            # 查今日是否有消息
            msgs_by_group = await self.storage.get_messages_by_group(
                date_str
            )
            msgs = msgs_by_group.get(group_id, [])
            if not msgs:
                await event.send(
                    f"📭 今日({date_str})本群无消息可分析。"
                )
                return

            provider = self.context.get_using_provider()
            if provider is None:
                await event.send("❌ 无 LLM provider,无法生成总结。")
                return

            gname = msgs[0].get("group_name") or group_id
            chunks = chunk_messages(
                msgs, self.cfg.max_messages_per_llm_call
            )
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
                await event.send(
                    render_error(date_str, group_id, gname)
                )
                return
            if not analysis.get("is_anime_day"):
                await event.send(
                    render_empty(date_str, group_id, gname)
                )
                return

            # 推送到群(不走 push_log 标记,因为是手动触发的)
            text = render_group_report(
                date_str=date_str,
                group_id=group_id,
                group_name=gname,
                analysis=analysis,
                top_n_users=self.cfg.top_n_users,
                top_n_works=self.cfg.top_n_works,
                summary_max_words=self.cfg.summary_max_words,
            )
            if (
                self.cfg.report_format == "html"
                and self.cfg.html_send_as_file
            ):
                html_str = render_group_html(
                    date_str=date_str,
                    group_id=group_id,
                    group_name=gname,
                    analysis=analysis,
                    top_n_users=self.cfg.top_n_users,
                    top_n_works=self.cfg.top_n_works,
                    summary_max_words=self.cfg.summary_max_words,
                )
                out_path = save_html_to_file(
                    html_str,
                    _resolve_html_output_dir(self),
                    prefix="anime_now",
                )
                await self._safe_push_html(group_id, out_path)
            else:
                await self._safe_push(group_id, text)
            # 给触发者一个回执
            try:
                await event.send(
                    f"✅ 今日总结已推送到本群。\n预览:\n{text[:200]}..."
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[anime_daily] /anime now failed: {e}", exc_info=True)
            try:
                await event.send(f"生成失败: {e}")
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

    # ============== 测试场景 ==============

    async def _run_test_scenario(
        self, event: Any, scenario: str
    ) -> str:
        """端到端跑一个测试场景,用 fake provider + 测试群 ID,不污染真实数据。

        可用场景:
        - empty_day:     当日无消息 → 不推任何东西,push_log 不增
        - llm_failure:   有消息但 LLM 全失败 → 推 error
        - no_anime:      LLM 判定 is_anime=False → 推 empty
        - success:       完整成功 → 推 group + global
        - idempotent:    跑两次 success,第二次无新推送
        - all:           顺序跑以上 5 个
        """
        if not self._is_ready():
            return self._not_ready_text()
        assert self.storage is not None
        storage = self.storage

        scenarios = (
            [
                "empty_day",
                "llm_failure",
                "no_anime",
                "success",
                "idempotent",
            ]
            if scenario == "all"
            else [scenario]
        )

        results: list[ScenarioResult] = []
        for s in scenarios:
            try:
                if s == "empty_day":
                    r = await self._test_empty_day(storage)
                elif s == "llm_failure":
                    r = await self._test_llm_failure(storage)
                elif s == "no_anime":
                    r = await self._test_no_anime(storage)
                elif s == "success":
                    r = await self._test_success(storage)
                elif s == "idempotent":
                    r = await self._test_idempotent(storage)
                else:
                    r = ScenarioResult(
                        name=s, passed=False,
                        notes=[f"未知场景: {s}"],
                    )
            except Exception as e:
                logger.error(
                    f"[anime_daily] test scenario {s} crashed: {e}",
                    exc_info=True,
                )
                r = ScenarioResult(
                    name=s, passed=False, notes=[f"异常: {e}"]
                )
            results.append(r)

        # 收尾:清理所有测试群的数据
        try:
            await self._cleanup_test_data(storage)
        except Exception as e:
            logger.warning(f"[anime_daily] cleanup test data: {e}")

        # 输出报告
        lines = ["🧪 测试场景报告", ""]
        for r in results:
            lines.append(r.to_text())
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        lines.append("")
        lines.append(f"📊 总计: {passed}/{total} 通过")
        return "\n".join(lines)

    async def _run_test_with_fake(
        self,
        storage: Storage,
        *,
        scenario_name: str,
        chunk_outputs: list[str],
        global_outputs: list[str] | None = None,
        insert_messages: bool = True,
    ) -> ScenarioResult:
        """用 fake provider 跑一次 _run_daily,返回场景结果。"""
        # 准备测试数据
        group_id = make_test_group_id(scenario_name, offset_days=0)
        test_date = make_test_date(offset_days=0)
        test_umo = make_test_umo(group_id)

        # 清掉该 group 旧测试数据(幂等)
        await self._cleanup_group_test_data(storage, group_id, test_date)

        if insert_messages:
            now_ts = int(time.time())
            for i in range(3):
                await storage.insert_message(
                    date_str=test_date,
                    group_id=group_id,
                    group_name=f"测试群-{scenario_name}",
                    user_id=f"{self.test_harness.TEST_USER_PREFIX}_{i}",
                    user_name=f"测试用户{i}",
                    message_id=f"test-{scenario_name}-{i}",
                    raw_text=f"测试消息 {i}: 孤独摇滚第 {i} 集很棒",
                    umo=test_umo,
                    created_at=now_ts + i,
                )

        # 构造 fake provider
        fake_provider = FakeLLMProvider()
        fake_provider.set_scenario(scenario_name, list(chunk_outputs))

        # 临时替换 context 的 get_using_provider
        original_get_using_provider = self.context.get_using_provider
        original_send_message = self.context.send_message
        sent_messages: list[Any] = []

        async def fake_send(umo, chain):
            sent_messages.append({"umo": umo, "chain": chain})

        self.context.get_using_provider = lambda: fake_provider  # type: ignore[method-assign]
        self.context.send_message = fake_send  # type: ignore[method-assign]

        result = ScenarioResult(name=scenario_name, passed=True)

        try:
            # 调 _run_daily(backfill=True 跳过 push_log 去重,便于首次测试)
            await self._run_daily(test_date, backfill=True)

            # 收集结果
            result.actual = {
                "llm_calls": fake_provider.call_count,
                "sent_count": len(sent_messages),
                "sent_umos": [m["umo"] for m in sent_messages],
            }

            # 检查 push_log
            for kind in ("group", "global", "empty", "error"):
                pushed = await storage.has_pushed(test_date, group_id, kind)
                result.actual[f"push_log_{kind}"] = bool(pushed)

            # 检查 analysis_cache
            cached = await storage.get_analysis_cache(
                test_date, f"group:{group_id}"
            )
            result.actual["has_cache"] = cached is not None
            result.actual["cache_is_anime_day"] = (
                bool(cached.get("is_anime_day")) if cached else None
            )

        except Exception as e:
            result.passed = False
            result.notes.append(f"异常: {e}")
        finally:
            self.context.get_using_provider = original_get_using_provider  # type: ignore[method-assign]
            self.context.send_message = original_send_message  # type: ignore[method-assign]

        return result

    async def _test_empty_day(self, storage: Storage) -> ScenarioResult:
        """场景1: 当日该群无消息。"""
        r = ScenarioResult(name="empty_day", passed=True)
        # 直接跑 _run_daily,看是否跳过
        gid = make_test_group_id("empty_day")
        test_date = make_test_date()
        await self._cleanup_group_test_data(storage, gid, test_date)

        # 不插入任何消息
        fake_provider = FakeLLMProvider()
        sent: list[Any] = []
        original_get = self.context.get_using_provider
        original_send = self.context.send_message

        async def fake_send(umo, chain):
            sent.append({"umo": umo})

        self.context.get_using_provider = lambda: fake_provider  # type: ignore[method-assign]
        self.context.send_message = fake_send  # type: ignore[method-assign]
        try:
            await self._run_daily(test_date, backfill=True)
        finally:
            self.context.get_using_provider = original_get  # type: ignore[method-assign]
            self.context.send_message = original_send  # type: ignore[method-assign]

        r.actual = {
            "llm_calls": fake_provider.call_count,
            "sent_count": len(sent),
        }
        # 期望:LLM 不被调(因为没消息),无推送
        if fake_provider.call_count == 0:
            r.notes.append("✅ 无消息时 LLM 未被调用")
        else:
            r.passed = False
            r.notes.append(f"❌ 期望 0 次 LLM 调用,实际 {fake_provider.call_count}")
        if len(sent) == 0:
            r.notes.append("✅ 无消息时未发送任何推送")
        else:
            r.passed = False
            r.notes.append(f"❌ 期望 0 次推送,实际 {len(sent)}")
        return r

    async def _test_llm_failure(self, storage: Storage) -> ScenarioResult:
        """场景2: 有消息但 LLM 全失败 → 推 error。"""
        r = await self._run_test_with_fake(
            storage,
            scenario_name="llm_failure",
            chunk_outputs=[
                make_chunk_invalid_payload(),  # 第 1 次:无效
                make_chunk_invalid_payload(),  # 第 2 次:无效(重试)
                make_chunk_invalid_payload(),  # 第 3 次:无效(第二 chunk 失败)
            ],
        )
        # 期望:sent_count == 1(push_log 记录 error)
        if r.actual.get("sent_count", 0) >= 1:
            r.notes.append("✅ LLM 失败时推 error 提示")
        else:
            r.passed = False
            r.notes.append(
                f"❌ 期望至少 1 次 error 推送,实际 {r.actual.get('sent_count', 0)}"
            )
        if r.actual.get("push_log_error"):
            r.notes.append("✅ push_log 记录了 error")
        else:
            r.passed = False
            r.notes.append("❌ push_log 未记录 error")
        if not r.actual.get("has_cache"):
            r.notes.append("✅ LLM 失败时无 analysis_cache 写入")
        else:
            r.passed = False
            r.notes.append("❌ LLM 失败时不应有 cache")
        return r

    async def _test_no_anime(self, storage: Storage) -> ScenarioResult:
        """场景3: LLM 判定 is_anime_day=False → 推 empty。"""
        r = await self._run_test_with_fake(
            storage,
            scenario_name="no_anime",
            chunk_outputs=[
                make_chunk_success_payload(is_anime=False, summary=""),
            ],
        )
        if r.actual.get("sent_count", 0) >= 1:
            r.notes.append("✅ 推 empty 提示")
        else:
            r.passed = False
            r.notes.append(
                f"❌ 期望至少 1 次 empty 推送,实际 {r.actual.get('sent_count', 0)}"
            )
        if r.actual.get("push_log_empty"):
            r.notes.append("✅ push_log 记录了 empty")
        else:
            r.passed = False
            r.notes.append("❌ push_log 未记录 empty")
        if r.actual.get("has_cache") and r.actual.get("cache_is_anime_day") is False:
            r.notes.append("✅ cache 记录 is_anime_day=False")
        else:
            r.passed = False
            r.notes.append(
                f"❌ cache 状态异常: has_cache={r.actual.get('has_cache')}, "
                f"is_anime_day={r.actual.get('cache_is_anime_day')}"
            )
        return r

    async def _test_success(self, storage: Storage) -> ScenarioResult:
        """场景4: 完整成功 → 推 group + global。"""
        r = await self._run_test_with_fake(
            storage,
            scenario_name="success",
            chunk_outputs=[
                make_chunk_success_payload(
                    summary="今日讨论孤独摇滚。"
                ),
            ],
            global_outputs=[make_global_success_payload()],
        )
        # 期望:至少 1 次 group + 1 次 global 推送
        sent = r.actual.get("sent_count", 0)
        if sent >= 2:
            r.notes.append(f"✅ 推 group + global 共 {sent} 次")
        else:
            r.passed = False
            r.notes.append(f"❌ 期望 >=2 次推送,实际 {sent}")
        if r.actual.get("push_log_group"):
            r.notes.append("✅ push_log 记录了 group")
        else:
            r.passed = False
            r.notes.append("❌ push_log 未记录 group")
        if r.actual.get("push_log_global"):
            r.notes.append("✅ push_log 记录了 global")
        else:
            r.passed = False
            r.notes.append("❌ push_log 未记录 global")
        if r.actual.get("has_cache"):
            r.notes.append("✅ analysis_cache 已写入")
        else:
            r.passed = False
            r.notes.append("❌ analysis_cache 未写入")
        return r

    async def _test_idempotent(self, storage: Storage) -> ScenarioResult:
        """场景5: 跑两次完整流程,第二次不重推。"""
        r = ScenarioResult(name="idempotent", passed=True)
        # 第一次:有真实推送
        r1 = await self._test_success(storage)
        first_sent = r1.actual.get("sent_count", 0)
        if not r1.passed:
            r.passed = False
            r.notes.append(f"❌ 第一次失败: {r1.notes}")
            return r

        # 第二次:backfill=False,应跳过(已 push_log)
        test_date = make_test_date()
        fake_provider = FakeLLMProvider()
        fake_provider.set_default_scenario(
            [make_chunk_success_payload(summary="重跑测试。")]
        )
        sent: list[Any] = []
        original_get = self.context.get_using_provider
        original_send = self.context.send_message

        async def fake_send(umo, chain):
            sent.append({"umo": umo})

        self.context.get_using_provider = lambda: fake_provider  # type: ignore[method-assign]
        self.context.send_message = fake_send  # type: ignore[method-assign]
        try:
            await self._run_daily(test_date, backfill=False)
        finally:
            self.context.get_using_provider = original_get  # type: ignore[method-assign]
            self.context.send_message = original_send  # type: ignore[method-assign]

        if len(sent) == 0:
            r.notes.append("✅ 第二次跑时未重推")
        else:
            r.passed = False
            r.notes.append(
                f"❌ 期望第二次 0 次推送,实际 {len(sent)} (推送幂等失败)"
            )
        r.actual = {
            "first_sent": first_sent,
            "second_sent": len(sent),
        }
        return r

    async def _cleanup_group_test_data(
        self, storage: Storage, group_id: str, date_str: str
    ) -> None:
        """清理某测试群在某日期的数据。"""
        try:
            # SQLite 直接 delete
            assert storage._conn is not None
            async with storage._lock:
                await storage._conn.execute(
                    "DELETE FROM messages WHERE group_id = ? AND date_str = ?",
                    (group_id, date_str),
                )
                await storage._conn.execute(
                    "DELETE FROM analysis_cache WHERE date_str = ? AND scope = ?",
                    (date_str, f"group:{group_id}"),
                )
                await storage._conn.execute(
                    "DELETE FROM push_log WHERE date_str = ? AND group_id = ?",
                    (date_str, group_id),
                )
                await storage._conn.commit()
        except Exception as e:
            logger.warning(f"cleanup test data for {group_id} failed: {e}")

    async def _cleanup_test_data(self, storage: Storage) -> None:
        """清理所有测试群(__test_anime__ 前缀)的数据。"""
        from .test_harness import TEST_GROUP_PREFIX

        try:
            assert storage._conn is not None
            async with storage._lock:
                await storage._conn.execute(
                    "DELETE FROM messages WHERE group_id LIKE ?",
                    (f"{TEST_GROUP_PREFIX}%",),
                )
                # 清理 global cache(scope 不会是 test_*)
                await storage._conn.commit()
        except Exception as e:
            logger.warning(f"cleanup all test data failed: {e}")

    def _build_help_text(self) -> str:
        """L2:统一帮助文案。"""
        return (
            "📚 astrbot_plugin_anime_daily 指令帮助\n\n"
            "• /anime help       — 显示本帮助\n"
            "• /anime sid        — 显示当前会话 unified_msg_origin(用于黑/白名单配置)\n"
            "• /anime reload     — 重新加载配置(改 push_time 后必用)\n"
            "• /anime now        — 立即对当前群生成今日总结(推送到群)\n"
            "• /anime preview    — 立即跑一次今日分析(只发给你,不推群)\n"
            "• /anime test [场景] — 端到端跑测试场景(管理员调试用)\n"
            "                       场景: empty_day / llm_failure / no_anime /\n"
            "                             success / idempotent / all(默认)\n"
            "• /anime today      — 查看本群今日榜单\n"
            "• /anime group <日期 YYYY-MM-DD> — 查看本群某日榜单\n"
            "• /anime user <user_id> [日期]   — 查看某用户发言记录\n"
            "• /anime global [日期]          — 查看全服总榜\n\n"
            "推送模式: 每天 23:00 自动分析并推送\n"
            f"当前格式: {self.cfg.report_format}  "
            f"名单模式: {self.cfg.group_list_mode}\n"
            "💡 改 push_time / max_concurrent_llm 等配置后,需用 /anime reload 生效。\n"
            "更多配置请在 WebUI「astrbot_plugin_anime_daily」中调整。"
        )
