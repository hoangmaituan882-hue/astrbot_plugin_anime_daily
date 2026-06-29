"""排行榜文本渲染(纯文本,起步)。"""
from __future__ import annotations

from .aggregator import truncate_summary


def _group_display_name(group_id: str, group_name: str | None) -> str:
    name = (group_name or "").strip() or group_id or "未知群"
    if group_id and group_name and group_name != group_id:
        return f"{name}({group_id})"
    return name


def _pad_num(n: int) -> str:
    return f"{n} 条"


def render_empty(date_str: str, group_id: str, group_name: str | None) -> str:
    return f"📭 {_group_display_name(group_id, group_name)} 今日({date_str})无动画话题"


def render_error(date_str: str, group_id: str, group_name: str | None) -> str:
    return (
        f"⚠️ {_group_display_name(group_id, group_name)} 今日({date_str})"
        f"动画分析失败,详情请查看插件日志。"
    )


def render_group_report(
    *,
    date_str: str,
    group_id: str,
    group_name: str | None,
    analysis: dict,
    top_n_users: int,
    top_n_works: int,
    summary_max_words: int,
) -> str:
    title = f"📊 {date_str} 动画话题日报"
    subtitle = f"🏷️ {_group_display_name(group_id, group_name)}"
    lines: list[str] = [title, subtitle, ""]

    users = (analysis.get("anime_user_stats") or [])[: max(0, top_n_users)]
    if users:
        lines.append(f"🏆 话痨榜 TOP {len(users)}")
        for i, u in enumerate(users, 1):
            name = u.get("user_name") or u.get("user_id", "?")
            cnt = int(u.get("anime_msg_count", 0) or 0)
            quote = (u.get("best_quote") or "").strip()
            line = f"{i}. @{name} — {_pad_num(cnt)}"
            if quote:
                line += f"  「{_short(quote, 40)}」"
            lines.append(line)
        lines.append("")

    works = (analysis.get("top_works") or [])[: max(0, top_n_works)]
    if works:
        lines.append(f"🔥 热门作品榜 TOP {len(works)}")
        for i, w in enumerate(works, 1):
            name = w.get("work", "?")
            cnt = int(w.get("count", 0) or 0)
            lines.append(f"{i}. {name} — {cnt} 次")
        lines.append("")

    summary = truncate_summary(
        analysis.get("summary", "") or "", summary_max_words
    )
    if summary:
        lines.append(f"💬 本日总结: {summary}")

    return "\n".join(lines).rstrip() + "\n"


def render_global_report(
    *,
    date_str: str,
    global_result: dict,
    top_n_users: int,
    top_n_works: int,
    summary_max_words: int,
) -> str:
    title = f"🌐 全服总榜 · {date_str}"
    lines: list[str] = [title, ""]

    users = (global_result.get("global_user_top") or [])[
        : max(0, top_n_users)
    ]
    if users:
        lines.append(f"🏆 全服话痨 TOP {len(users)}")
        for i, u in enumerate(users, 1):
            name = u.get("user_name") or u.get("user_id", "?")
            gname = u.get("group_name") or u.get("group_id", "?")
            cnt = int(u.get("anime_msg_count", 0) or 0)
            quote = (u.get("best_quote") or "").strip()
            line = f"{i}. @{name}({gname}) — {_pad_num(cnt)}"
            if quote:
                line += f"  「{_short(quote, 40)}」"
            lines.append(line)
        lines.append("")

    works = (global_result.get("global_works_top") or [])[
        : max(0, top_n_works)
    ]
    if works:
        lines.append(f"🔥 全服热门作品 TOP {len(works)}")
        for i, w in enumerate(works, 1):
            name = w.get("work", "?")
            cnt = int(w.get("total_count", 0) or 0)
            lines.append(f"{i}. {name} — {cnt} 次")
        lines.append("")

    summary = truncate_summary(
        global_result.get("summary", "") or "", summary_max_words
    )
    if summary:
        lines.append(f"💬 全服总结: {summary}")

    return "\n".join(lines).rstrip() + "\n"


def render_user_record(
    user_id: str,
    msgs: list[dict],
    *,
    date_str: str | None = None,
    limit: int = 20,
) -> str:
    if not msgs:
        return f"未找到用户 {user_id} 的发言记录。"
    title = f"🗂️ 用户 {user_id} 发言记录"
    if date_str:
        title += f" ({date_str})"
    lines = [title, ""]
    total = len(msgs)
    for m in msgs[:limit]:
        ts = _fmt_ts_local(m.get("created_at", 0))
        text = (m.get("raw_text") or "").strip()
        gname = m.get("group_name") or m.get("group_id") or "?"
        lines.append(f"[{ts}] [{gname}] {text}")
    if total > limit:
        lines.append(f"... (共 {total} 条,仅显示前 {limit} 条)")
    return "\n".join(lines)


def _short(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


def _fmt_ts_local(ts: int) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)
