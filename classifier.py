"""LLM 分类器:单群单块分析 + 跨群汇总。

所有 LLM 调用都通过 self.context.get_using_provider().text_chat() 完成
(底层,无副作用;详见 AstrBot 开发指南第 12 章 方法 1)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 单元测试环境兜底
    logger = logging.getLogger("astrbot_plugin_anime_daily.classifier")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

GROUP_PROMPT_TEMPLATE = """你是群聊动画话题分析助手。下面是群「{group_name}」在 {date} 的**部分**发言 \
({chunk_start} ~ {chunk_end},共 {n} 条)。这是全天消息的一个分块,可能存在多轮上下文。

判定规则:
1. 「动画相关」包括:作品名/角色/声优/制作/二创/OPED/ED/BD/动画化/声优事件等
2. 跟聊也算:某用户在前一条动画消息后的合理回复(如「我也喜欢」「真不错」「同意」)
3. 只输出与动画话题相关的用户;无关用户不列入 anime_user_stats
4. 作品名归一化:中英文/日文别名视为同一作品
   - 「孤独摇滚」=「ぼっち・ざ・ろっく!」=「Bocchi the Rock」=「波奇」 → 统一为「孤独摇滚」
   - 输出 top_works[].work 使用最常见的中文译名;若仅有日文/英文,保留原文
5. 仅统计**本块内**的数据,不要假设其他块

输出严格 JSON,不要任何额外文字:
{{
  "is_anime_chunk": bool,
  "anime_user_stats": [
    {{
      "user_id": "...",
      "user_name": "...",
      "anime_msg_count": 0,
      "related_msg_count": 0,
      "best_quote": "..."
    }}
  ],
  "top_works": [{{"work": "...", "count": 0}}],
  "summary": "本块一句话小结(<=30字)"
}}

消息列表(JSON 数组,每条含 user_id/user_name/text/ts):
{msgs_json}
"""


GLOBAL_PROMPT_TEMPLATE = """你是全服动画话题汇总助手。下面是 {date} 各群的动画话题分析结果(每群已由前一阶段判定为「当日有动画话题」)。

请基于以下结果做跨群汇总(不要再读原始消息):

任务:
1. 跨群话痨榜:按 anime_msg_count 汇总同一 user_id 在不同群中的发言
2. 跨群热门作品:按 work 归一化键汇总 total_count(归一化已在上一阶段完成)
3. 全服一句话总结

输出严格 JSON,不要任何额外文字:
{{
  "global_user_top": [
    {{
      "user_id": "...",
      "user_name": "...",
      "group_id": "...",
      "group_name": "...",
      "anime_msg_count": 0,
      "best_quote": "..."
    }}
  ],
  "global_works_top": [{{"work": "...", "total_count": 0}}],
  "summary": "全服今日一句话总结(<=60字)"
}}

各群分析结果(JSON 数组):
{groups_json}
"""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """从 LLM 文本中尽力提取 JSON 对象。"""
    if not text:
        return None
    # 1) 尝试整段解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) 提取首个 {...} 块
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    candidate = m.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # 3) 容忍尾部逗号等小问题:暴力替换
    cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


async def _call_llm_json(
    provider: Any,
    prompt: str,
    temperature: float,
) -> dict | None:
    """调一次 LLM,返回解析后的 JSON;失败返回 None。"""
    try:
        resp = await provider.text_chat(
            prompt=prompt,
            session_id=None,
            contexts=[],
            image_urls=[],
            func_tool=None,
            system_prompt="你是一个严格的 JSON 输出助手,只输出 JSON,不要任何解释或 Markdown。",
        )
    except Exception as e:
        logger.warning(f"LLM call failed: {e}", exc_info=True)
        return None

    # 文本响应
    text = ""
    if hasattr(resp, "completion_text") and resp.completion_text:
        text = resp.completion_text
    elif hasattr(resp, "role") and resp.role == "tool":
        # 工具调用型响应,本次不需要
        return None

    return _extract_json(text)


async def analyze_one_chunk(
    provider: Any,
    *,
    group_name: str,
    date: str,
    chunk: list[dict],
    temperature: float,
) -> dict | None:
    """对单块调用一次 LLM,返回该块分析结果;失败返回 None。"""
    if not chunk:
        return None
    msgs_compact = [
        {
            "user_id": m.get("user_id", ""),
            "user_name": m.get("user_name") or m.get("user_id", ""),
            "text": m.get("raw_text", ""),
            "ts": m.get("created_at", 0),
        }
        for m in chunk
    ]
    msgs_json = json.dumps(msgs_compact, ensure_ascii=False)
    prompt = GROUP_PROMPT_TEMPLATE.format(
        group_name=group_name or "未知群",
        date=date,
        chunk_start=_fmt_ts(chunk[0]["created_at"]),
        chunk_end=_fmt_ts(chunk[-1]["created_at"]),
        n=len(chunk),
        msgs_json=msgs_json,
    )
    # 重试一次
    for _ in range(2):
        result = await _call_llm_json(provider, prompt, temperature)
        if result is not None:
            return _normalize_chunk_result(result)
        logger.warning("LLM JSON parse failed, retrying once...")
    return None


def _normalize_chunk_result(raw: dict) -> dict:
    """对 LLM 输出做最小化字段规整,保证后续合并稳定。"""
    anime_user_stats = []
    for u in raw.get("anime_user_stats", []) or []:
        if not isinstance(u, dict):
            continue
        anime_user_stats.append(
            {
                "user_id": str(u.get("user_id", "")),
                "user_name": str(u.get("user_name", "") or u.get("user_id", "")),
                "anime_msg_count": int(u.get("anime_msg_count", 0) or 0),
                "related_msg_count": int(u.get("related_msg_count", 0) or 0),
                "best_quote": str(u.get("best_quote", "") or ""),
            }
        )
    top_works = []
    for w in raw.get("top_works", []) or []:
        if not isinstance(w, dict):
            continue
        name = str(w.get("work", "")).strip()
        if not name:
            continue
        top_works.append(
            {"work": name, "count": int(w.get("count", 0) or 0)}
        )
    return {
        "is_anime_chunk": bool(raw.get("is_anime_chunk", False)),
        "anime_user_stats": anime_user_stats,
        "top_works": top_works,
        "summary": str(raw.get("summary", "") or "").strip(),
    }


async def analyze_group_today(
    provider: Any,
    *,
    group_id: str,
    group_name: str,
    date: str,
    chunks: list[list[dict]],
    temperature: float,
) -> dict | None:
    """对单个群的所有 chunk 依次调 LLM,然后本地合并。

    返回 merge_partial_results 的输出;若所有 chunk 全部失败则返回 None。
    """
    if not chunks:
        return None
    partials: list[dict] = []
    for i, chunk in enumerate(chunks):
        logger.info(
            f"[anime_daily] analyze group={group_id} chunk={i + 1}/{len(chunks)} size={len(chunk)}"
        )
        r = await analyze_one_chunk(
            provider,
            group_name=group_name,
            date=date,
            chunk=chunk,
            temperature=temperature,
        )
        if r is not None:
            partials.append(r)
    if not partials:
        return None

    # 本地合并
    from .aggregator import merge_partial_results  # 避免循环

    return merge_partial_results(partials)


async def aggregate_global(
    provider: Any,
    *,
    date: str,
    successful_groups: list[dict],
    temperature: float,
) -> dict | None:
    """阶段二:基于每群已分析结果,做跨群汇总(LLM 一次)。"""
    if not successful_groups:
        return None
    groups_compact = [
        {
            "group_id": g.get("group_id", ""),
            "group_name": g.get("group_name", "") or g.get("group_id", ""),
            "anime_user_stats": g.get("anime_user_stats", []),
            "top_works": g.get("top_works", []),
            "summary": g.get("summary", ""),
        }
        for g in successful_groups
    ]
    groups_json = json.dumps(groups_compact, ensure_ascii=False)
    prompt = GLOBAL_PROMPT_TEMPLATE.format(date=date, groups_json=groups_json)

    for _ in range(2):
        raw = await _call_llm_json(provider, prompt, temperature)
        if raw is None:
            logger.warning("Global LLM JSON parse failed, retrying once...")
            continue
        return _normalize_global_result(raw)
    return None


def _normalize_global_result(raw: dict) -> dict:
    user_top = []
    for u in raw.get("global_user_top", []) or []:
        if not isinstance(u, dict):
            continue
        user_top.append(
            {
                "user_id": str(u.get("user_id", "")),
                "user_name": str(u.get("user_name", "") or u.get("user_id", "")),
                "group_id": str(u.get("group_id", "")),
                "group_name": str(u.get("group_name", "") or u.get("group_id", "")),
                "anime_msg_count": int(u.get("anime_msg_count", 0) or 0),
                "best_quote": str(u.get("best_quote", "") or ""),
            }
        )
    works_top = []
    for w in raw.get("global_works_top", []) or []:
        if not isinstance(w, dict):
            continue
        name = str(w.get("work", "")).strip()
        if not name:
            continue
        works_top.append(
            {
                "work": name,
                "total_count": int(w.get("total_count", 0) or 0),
            }
        )
    return {
        "global_user_top": user_top,
        "global_works_top": works_top,
        "summary": str(raw.get("summary", "") or "").strip(),
    }


def _fmt_ts(ts: int) -> str:
    """把 unix ts 格式化为 HH:MM。"""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except Exception:
        return str(ts)
