"""LLM 分类器:单群单块分析 + 跨群汇总。

所有 LLM 调用都通过 provider.text_chat() 完成(底层,无副作用;详见开发指南第 12 章 方法 1)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from json import JSONDecoder
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 单元测试环境兜底
    logger = logging.getLogger("astrbot_plugin_anime_daily.classifier")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

# 容错:JSON 里偶发 `,\n}` 的尾逗号 / Markdown ```json 围栏 / 中文引号包裹 / 多余解释文字。
# 用 raw_decode 从任意位置起解码,失败再尝试清理。
_JSON_DECODER = JSONDecoder()


def _extract_json(text: str) -> dict | None:
    """B23:从 LLM 文本中尽力提取 JSON 对象。"""
    if not text:
        return None
    text = text.strip()

    # 去掉 Markdown 围栏
    if text.startswith("```"):
        text = text.strip("`")
        # 去掉可能的首行 "json"
        if "\n" in text:
            text = text.split("\n", 1)[1] if text.lower().startswith("json") else text
        text = text.strip().rstrip("`").strip()

    # 1) 整段解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 从任意 `{` 起尝试 raw_decode
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = _JSON_DECODER.raw_decode(text[i:])
                return obj
            except Exception:
                continue

    # 3) 尾部逗号容错
    cleaned = text.replace(",}", "}").replace(",]", "]")
    try:
        return json.loads(cleaned)
    except Exception:
        return None


async def _call_llm_once(
    provider: Any,
    prompt: str,
    temperature: float,
) -> tuple[str | None, str]:
    """B2:把"调 LLM"和"解析 JSON"拆开。

    返回 (parsed_dict_or_none, raw_text)。
    LLM 失败抛异常由上层捕获;解析失败由 _extract_json 返回 None。
    """
    resp = await provider.text_chat(
        prompt=prompt,
        session_id=None,
        contexts=[],
        image_urls=[],
        func_tool=None,
        system_prompt="你是一个严格的 JSON 输出助手,只输出 JSON,不要任何解释或 Markdown。",
    )
    if hasattr(resp, "completion_text") and resp.completion_text:
        return _extract_json(resp.completion_text), resp.completion_text
    return None, ""


async def _call_llm_with_retry(
    provider: Any,
    prompt: str,
    temperature: float,
    *,
    max_retries: int = 2,
) -> dict | None:
    """B2:LLM 调用最多 max_retries 次;解析失败也算失败。

    注意:此处不再做"换 prompt 扰动",保持 LLM 行为稳定(成本/准确性平衡)。
    """
    last_err: str = ""
    for attempt in range(max_retries):
        try:
            parsed, raw = await _call_llm_once(provider, prompt, temperature)
        except Exception as e:
            logger.warning(
                f"[anime_daily] LLM call attempt {attempt + 1} failed: {e}",
                exc_info=True,
            )
            last_err = str(e)
            continue
        if parsed is not None:
            return parsed
        # 解析失败:留 raw 用于排错
        last_err = (raw or "")[:200]
        logger.warning(
            f"[anime_daily] LLM JSON parse failed (attempt {attempt + 1}): {last_err!r}"
        )
    logger.error(
        f"[anime_daily] LLM give up after {max_retries} attempts: {last_err}"
    )
    return None


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
1. 跨群话痨榜:把同一 user_id 在不同群中的 anime_msg_count 合并为一行(总发言数),
   group_name 字段留空(汇总后不再属于单一群);若同一 user_id 跨群,best_quote 选最精炼的那条
2. 跨群热门作品:按 work 归一化键汇总 total_count(归一化已在上一阶段完成)
3. 全服一句话总结

输出严格 JSON,不要任何额外文字:
{{
  "global_user_top": [
    {{
      "user_id": "...",
      "user_name": "...",
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


async def analyze_one_chunk(
    provider: Any,
    *,
    group_name: str,
    date: str,
    chunk: list[dict],
    temperature: float,
    sem: asyncio.Semaphore,
) -> dict | None:
    """B12:对单块调用一次 LLM,通过信号量限流。

    返回该块分析结果;失败返回 None。
    """
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
    async with sem:
        result = await _call_llm_with_retry(provider, prompt, temperature)
    if result is None:
        return None
    return _normalize_chunk_result(result)


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


# 复用同一个信号量
_DEFAULT_SEM = asyncio.Semaphore(3)


async def analyze_group_today(
    provider: Any,
    *,
    group_id: str,
    group_name: str,
    date: str,
    chunks: list[list[dict]],
    temperature: float,
    sem: asyncio.Semaphore | None = None,
) -> dict | None:
    """B3 + B12 + B22:并发跑各 chunk,通过信号量限流。

    返回值:
      - 正常合并结果 dict(可能 is_anime_day=False 表示无动画)
      - None 表示"该群所有 chunk 全部失败"——上层应推 error 而非 empty
    """
    if not chunks:
        return None
    if sem is None:
        sem = _DEFAULT_SEM

    tasks = [
        analyze_one_chunk(
            provider,
            group_name=group_name,
            date=date,
            chunk=chunk,
            temperature=temperature,
            sem=sem,
        )
        for chunk in chunks
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    partials: list[dict] = []
    for i, r in enumerate(results):
        if r is not None:
            partials.append(r)
        else:
            logger.warning(
                f"[anime_daily] chunk {i + 1}/{len(chunks)} of group={group_id} failed"
            )

    if not partials:
        return None

    from .aggregator import merge_partial_results  # 避免循环

    return merge_partial_results(partials)


async def aggregate_global(
    provider: Any,
    *,
    date: str,
    successful_groups: list[dict],
    temperature: float,
    sem: asyncio.Semaphore | None = None,
) -> dict | None:
    """阶段二:基于每群已分析结果,做跨群汇总(LLM 一次)。

    B5:prompt 要求 LLM 跨群合并 user(输出不再带 group_id),与本地降级保持口径一致。
    """
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

    if sem is None:
        sem = _DEFAULT_SEM
    async with sem:
        raw = await _call_llm_with_retry(provider, prompt, temperature)
    if raw is None:
        return None
    return _normalize_global_result(raw)


def _normalize_global_result(raw: dict) -> dict:
    """B5:不再带 group_id/group_name(跨群合并后无单一群)。"""
    user_top = []
    for u in raw.get("global_user_top", []) or []:
        if not isinstance(u, dict):
            continue
        user_top.append(
            {
                "user_id": str(u.get("user_id", "")),
                "user_name": str(u.get("user_name", "") or u.get("user_id", "")),
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
