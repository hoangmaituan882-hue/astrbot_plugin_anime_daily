"""本地聚合工具:消息分块(时间窗) + 多块 LLM 结果合并 + 全服合并。"""
from __future__ import annotations

WINDOW_SECONDS = 30 * 60


def chunk_messages(
    msgs: list[dict], max_per_call: int
) -> list[list[dict]]:
    """按 30 分钟时间窗贪心装箱分块。

    1) 若 N <= max_per_call,直接返回 [msgs]。
    2) 否则按 30 分钟时间窗聚合:累加块内消息,加入新条会超阈值且已跨窗则开新块。
    3) 极端密集时段硬切:对任一超过 max_per_call 的块按 max_per_call 截断。

    msgs: 必须按 created_at 升序;若未排序,函数内部会排序。
    """
    if not msgs:
        return []
    sorted_msgs = sorted(msgs, key=lambda m: m["created_at"])
    if len(sorted_msgs) <= max_per_call:
        return [sorted_msgs]

    chunks: list[list[dict]] = []
    cur: list[dict] = []
    window_start: int | None = None

    for m in sorted_msgs:
        ts = m["created_at"]
        if window_start is None:
            window_start = ts
        # 块满 且 已跨出当前时间窗
        if len(cur) >= max_per_call and (ts - window_start) > WINDOW_SECONDS:
            chunks.append(cur)
            cur = []
            window_start = ts
        cur.append(m)

    if cur:
        chunks.append(cur)

    # 极端密集块硬切(同一时间窗内消息量 > max_per_call)
    final: list[list[dict]] = []
    for c in chunks:
        for i in range(0, len(c), max_per_call):
            final.append(c[i : i + max_per_call])
    return final


def _norm_work_key(work: str) -> str:
    """作品名归一化键:小写 + 去空白。"""
    return work.strip().lower().replace(" ", "")


def _pick_best_quote(quotes: list[str]) -> str:
    """B4:从一组 quote 中选最精炼的一条(<=20 字)优先,否则取最短。

    理由:金句通常是 4~12 字的短评;长 quote 反而像凑字数的口水话。
    """
    if not quotes:
        return ""
    # 去重(B19)
    seen: set[str] = set()
    unique = []
    for q in quotes:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    # 优先短且非空
    short_pool = [q for q in unique if 0 < len(q) <= 20]
    if short_pool:
        # 短池里再取最长(避免一个 2 字的"嗯"赢过一个 15 字的金句)
        return max(short_pool, key=len)
    # 否则取最短的(防止超长口水话)
    return min(unique, key=len)


def merge_partial_results(partials: list[dict]) -> dict:
    """合并多个 chunk 的 LLM 输出(纯本地,无 LLM 调用)。

    partials 形如:
      {
        "is_anime_chunk": bool,
        "anime_user_stats": [{"user_id","user_name","anime_msg_count",
                              "related_msg_count","best_quote"}, ...],
        "top_works": [{"work": str, "count": int}, ...],
        "summary": str
      }

    返回最终群级分析:
      {
        "is_anime_day": bool,
        "anime_user_stats": [...],
        "top_works": [...],
        "summary": str
      }
    """
    if not partials:
        return {
            "is_anime_day": False,
            "anime_user_stats": [],
            "top_works": [],
            "summary": "",
        }

    is_anime_day = any(bool(p.get("is_anime_chunk")) for p in partials)

    # 用户聚合
    user_agg: dict[str, dict] = {}
    for p in partials:
        for u in p.get("anime_user_stats", []) or []:
            uid = str(u.get("user_id", ""))
            if not uid:
                continue
            cur = user_agg.setdefault(
                uid,
                {
                    "user_id": uid,
                    "user_name": u.get("user_name") or uid,
                    "anime_msg_count": 0,
                    "related_msg_count": 0,
                    "_quotes": [],
                },
            )
            cur["anime_msg_count"] += int(u.get("anime_msg_count", 0) or 0)
            cur["related_msg_count"] += int(
                u.get("related_msg_count", 0) or 0
            )
            q = (u.get("best_quote") or "").strip()
            if q:
                cur["_quotes"].append(q)
            # 优先保留更新的昵称
            if u.get("user_name"):
                cur["user_name"] = u["user_name"]

    anime_user_stats: list[dict] = []
    for u in user_agg.values():
        quotes = u.pop("_quotes", [])
        u["best_quote"] = _pick_best_quote(quotes)
        anime_user_stats.append(u)

    anime_user_stats.sort(
        key=lambda x: (
            -int(x.get("anime_msg_count", 0) or 0),
            -int(x.get("related_msg_count", 0) or 0),
        )
    )

    # 作品聚合(按归一化键)
    work_agg: dict[str, dict] = {}
    for p in partials:
        for w in p.get("top_works", []) or []:
            name = str(w.get("work", "")).strip()
            if not name:
                continue
            key = _norm_work_key(name)
            cnt = int(w.get("count", 0) or 0)
            cur = work_agg.get(key)
            if cur is None:
                work_agg[key] = {"work": name, "count": cnt, "_aliases": {name}}
            else:
                cur["count"] += cnt
                cur["_aliases"].add(name)
                cur["work"] = _pick_preferred_name(cur["_aliases"])

    top_works = [
        {"work": v["work"], "count": v["count"]}
        for v in sorted(
            work_agg.values(), key=lambda x: -int(x["count"])
        )
    ]

    # 总结拼接
    summary_parts = [
        (p.get("summary") or "").strip()
        for p in partials
        if (p.get("summary") or "").strip()
    ]
    summary = " · ".join(summary_parts)

    return {
        "is_anime_day": is_anime_day,
        "anime_user_stats": anime_user_stats,
        "top_works": top_works,
        "summary": summary,
    }


def _pick_preferred_name(aliases: set[str]) -> str:
    """从一组别名中挑选首选名:含中文的最长名,否则返回任一。"""
    if not aliases:
        return ""
    cjk = [a for a in aliases if any("\u4e00" <= ch <= "\u9fff" for ch in a)]
    pool = cjk if cjk else list(aliases)
    pool.sort(key=len, reverse=True)
    return pool[0]


def truncate_summary(text: str, max_words: int) -> str:
    """按字符数裁剪总结(中文按字计)。"""
    if not text:
        return ""
    if len(text) <= max_words:
        return text
    return text[: max(0, max_words - 1)] + "…"


def merge_global_results(per_group: list[dict]) -> dict:
    """B5:全服榜跨群合并 user(去掉 group_id/group_name 字段,口径与 LLM 一致)。

    输入 per_group: 每个元素是 merge_partial_results 的输出 + group_id/group_name。
    输出:
      {
        "global_user_top": [
          {"user_id","user_name","anime_msg_count","best_quote"}, ...
        ],
        "global_works_top": [{"work","total_count"}, ...],
        "summary": str   -- 由各群 summary 拼接
      }
    """
    user_agg: dict[str, dict] = {}
    work_agg: dict[str, dict] = {}
    summaries: list[str] = []

    for g in per_group:
        gid = g.get("group_id") or ""
        gname = g.get("group_name") or gid
        s = (g.get("summary") or "").strip()
        if s:
            summaries.append(f"[{gname}] {s}")

        for u in g.get("anime_user_stats", []) or []:
            uid = str(u.get("user_id", ""))
            if not uid:
                continue
            # B5 关键:按 user_id 跨群合并(不再用 (uid, gid) 复合键)
            cur = user_agg.setdefault(
                uid,
                {
                    "user_id": uid,
                    "user_name": u.get("user_name") or uid,
                    "anime_msg_count": 0,
                    "_quotes": [],
                    "_groups": set(),
                },
            )
            cur["anime_msg_count"] += int(u.get("anime_msg_count", 0) or 0)
            q = (u.get("best_quote") or "").strip()
            if q:
                cur["_quotes"].append(q)
            if u.get("user_name"):
                cur["user_name"] = u["user_name"]
            if gname:
                cur["_groups"].add(gname)

        for w in g.get("top_works", []) or []:
            name = str(w.get("work", "")).strip()
            if not name:
                continue
            key = _norm_work_key(name)
            cnt = int(w.get("count", 0) or 0)
            cur = work_agg.get(key)
            if cur is None:
                work_agg[key] = {
                    "work": name,
                    "total_count": cnt,
                    "_aliases": {name},
                }
            else:
                cur["total_count"] += cnt
                cur["_aliases"].add(name)
                cur["work"] = _pick_preferred_name(cur["_aliases"])

    global_user_top: list[dict] = []
    for v in user_agg.values():
        quotes = v.pop("_quotes", [])
        groups = v.pop("_groups", set())
        v["best_quote"] = _pick_best_quote(quotes)
        v["group_count"] = len(groups)  # 跨了几个群(信息保留,渲染可选)
        global_user_top.append(v)
    global_user_top.sort(key=lambda x: -int(x.get("anime_msg_count", 0) or 0))

    global_works_top = [
        {"work": v["work"], "total_count": v["total_count"]}
        for v in sorted(
            work_agg.values(), key=lambda x: -int(x["total_count"])
        )
    ]

    return {
        "global_user_top": global_user_top,
        "global_works_top": global_works_top,
        "summary": " · ".join(summaries),
    }
