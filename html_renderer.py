"""L3:HTML 报告渲染器(内联 Jinja2 模板)。

优点:
- 模板与代码同包,不需要外部资源文件
- 单文件 0 依赖(只用 jinja2,而 AstrBot 已自带)
- 输出 .html 文件,用户可浏览器打开,或配 Nginx 做外链
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

try:
    from jinja2 import Template
except Exception:  # 单元测试环境兜底
    Template = None  # type: ignore[assignment]

# --- 模板片段(直接拼接成完整 HTML)---

PAGE_STYLE = """
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
                 "Helvetica Neue", Arial, sans-serif;
    background: linear-gradient(135deg, #f6f8fb 0%, #e9eef5 100%);
    color: #1f2937;
    margin: 0;
    padding: 24px;
    min-height: 100vh;
  }
  .container { max-width: 720px; margin: 0 auto; }
  .card {
    background: #fff;
    border-radius: 12px;
    padding: 24px 28px;
    margin-bottom: 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.06);
  }
  h1 { font-size: 22px; margin: 0 0 4px; color: #111827; }
  h2 { font-size: 17px; margin: 0 0 14px; color: #374151;
       border-left: 4px solid #6366f1; padding-left: 10px; }
  .meta { color: #6b7280; font-size: 13px; margin-bottom: 4px; }
  .summary {
    background: #f3f4f6;
    border-radius: 8px;
    padding: 12px 16px;
    margin: 12px 0 0;
    color: #4b5563;
    font-size: 14px;
    line-height: 1.6;
  }
  ol { padding-left: 22px; margin: 0; }
  li { padding: 6px 0; font-size: 14px; line-height: 1.5; }
  .rank { display: inline-block; min-width: 22px;
          color: #9ca3af; font-weight: 600; }
  .top1 .rank { color: #f59e0b; }
  .top2 .rank { color: #94a3b8; }
  .top3 .rank { color: #b45309; }
  .quote {
    color: #6b7280;
    font-size: 12px;
    margin-left: 8px;
  }
  .works { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; margin: 0;
           list-style: none; }
  .works li {
    background: linear-gradient(135deg, #818cf8 0%, #6366f1 100%);
    color: #fff;
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 13px;
  }
  .global-card {
    background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
  }
  .global-card h2 { border-left-color: #d97706; }
  .badge {
    display: inline-block;
    background: #e0e7ff;
    color: #4338ca;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    margin-left: 6px;
  }
  .footer { text-align: center; color: #9ca3af; font-size: 11px;
            margin-top: 24px; }
  .empty { color: #9ca3af; font-size: 14px; padding: 12px; }
</style>
"""

PAGE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
""" + PAGE_STYLE + """
</head>
<body>
<div class="container">
"""


def _esc(text: str) -> str:
    """HTML 转义。"""
    if text is None:
        return ""
    return html.escape(str(text))


def _build_group_section(
    date_str: str,
    group_id: str,
    group_name: str,
    analysis: dict,
    top_n_users: int,
    top_n_works: int,
) -> str:
    """构造一个群日报的 HTML 片段。"""
    users = (analysis.get("anime_user_stats") or [])[: max(0, top_n_users)]
    works = (analysis.get("top_works") or [])[: max(0, top_n_works)]
    summary = analysis.get("summary") or ""

    parts: list[str] = []
    parts.append('<div class="card">')
    parts.append(
        f'<h1>📊 {_esc(date_str)} 动画话题日报</h1>'
    )
    parts.append(
        f'<div class="meta">🏷️ {_esc(group_name or group_id)}</div>'
    )

    if users:
        parts.append('<h2>🏆 话痨榜</h2>')
        parts.append('<ol>')
        for i, u in enumerate(users, 1):
            name = u.get("user_name") or u.get("user_id", "?")
            cnt = int(u.get("anime_msg_count", 0) or 0)
            quote = (u.get("best_quote") or "").strip()
            top_cls = {1: "top1", 2: "top2", 3: "top3"}.get(i, "")
            parts.append(f'<li class="{top_cls}">')
            parts.append(
                f'<span class="rank">#{i}</span> '
                f'<strong>@{_esc(name)}</strong> — {_esc(cnt)} 条'
            )
            if quote:
                parts.append(
                    f'<span class="quote">「{_esc(quote[:40])}」</span>'
                )
            parts.append('</li>')
        parts.append('</ol>')

    if works:
        parts.append('<h2>🔥 热门作品</h2>')
        parts.append('<ul class="works">')
        for w in works:
            name = w.get("work", "?")
            cnt = int(w.get("count", 0) or 0)
            parts.append(
                f'<li>{_esc(name)} · {_esc(cnt)} 次</li>'
            )
        parts.append('</ul>')

    if summary:
        parts.append(
            f'<div class="summary">💬 {_esc(summary)}</div>'
        )

    if not users and not works:
        parts.append('<div class="empty">今日无动画话题</div>')

    parts.append('</div>')
    return "".join(parts)


def _build_global_section(
    date_str: str,
    global_result: dict,
    top_n_users: int,
    top_n_works: int,
) -> str:
    """构造全服总榜 HTML 片段。"""
    users = (global_result.get("global_user_top") or [])[
        : max(0, top_n_users)
    ]
    works = (global_result.get("global_works_top") or [])[
        : max(0, top_n_works)
    ]
    summary = global_result.get("summary") or ""

    parts: list[str] = []
    parts.append('<div class="card global-card">')
    parts.append(f'<h1>🌐 全服总榜 · {_esc(date_str)}</h1>')

    if users:
        parts.append('<h2>🏆 全服话痨</h2>')
        parts.append('<ol>')
        for i, u in enumerate(users, 1):
            name = u.get("user_name") or u.get("user_id", "?")
            cnt = int(u.get("anime_msg_count", 0) or 0)
            quote = (u.get("best_quote") or "").strip()
            gc = int(u.get("group_count", 0) or 0)
            top_cls = {1: "top1", 2: "top2", 3: "top3"}.get(i, "")
            parts.append(f'<li class="{top_cls}">')
            parts.append(
                f'<span class="rank">#{i}</span> '
                f'<strong>@{_esc(name)}</strong> — {_esc(cnt)} 条'
            )
            if gc > 1:
                parts.append(
                    f'<span class="badge">跨 {_esc(gc)} 群</span>'
                )
            if quote:
                parts.append(
                    f'<span class="quote">「{_esc(quote[:40])}」</span>'
                )
            parts.append('</li>')
        parts.append('</ol>')

    if works:
        parts.append('<h2>🔥 全服热门作品</h2>')
        parts.append('<ul class="works">')
        for w in works:
            name = w.get("work", "?")
            cnt = int(w.get("total_count", 0) or 0)
            parts.append(
                f'<li>{_esc(name)} · {_esc(cnt)} 次</li>'
            )
        parts.append('</ul>')

    if summary:
        parts.append(
            f'<div class="summary">💬 {_esc(summary)}</div>'
        )

    if not users and not works:
        parts.append('<div class="empty">全服今日无动画话题</div>')

    parts.append('</div>')
    return "".join(parts)


def render_group_html(
    *,
    date_str: str,
    group_id: str,
    group_name: str,
    analysis: dict,
    top_n_users: int,
    top_n_works: int,
    summary_max_words: int,
) -> str:
    """渲染本群报告 HTML(单群)。"""
    if not summary_max_words:
        summary_max_words = 60
    # summary 截断(简单字符数截断)
    s = analysis.get("summary", "") or ""
    if len(s) > summary_max_words:
        s = s[: max(0, summary_max_words - 1)] + "…"
        analysis = {**analysis, "summary": s}
    body = _build_group_section(
        date_str, group_id, group_name, analysis, top_n_users, top_n_works
    )
    title = f"{date_str} 动画话题日报 - {group_name or group_id}"
    footer = (
        '<div class="footer">由 astrbot_plugin_anime_daily 生成 · '
        f'{_esc(datetime.now().strftime("%Y-%m-%d %H:%M"))}</div>'
    )
    return PAGE_HEAD.replace("{{ title }}", _esc(title)) + body + footer + "</div></body></html>\n"


def render_global_html(
    *,
    date_str: str,
    global_result: dict,
    top_n_users: int,
    top_n_works: int,
    summary_max_words: int,
) -> str:
    """渲染全服总榜 HTML。"""
    if not summary_max_words:
        summary_max_words = 60
    s = global_result.get("summary", "") or ""
    if len(s) > summary_max_words:
        s = s[: max(0, summary_max_words - 1)] + "…"
        global_result = {**global_result, "summary": s}
    body = _build_global_section(
        date_str, global_result, top_n_users, top_n_works
    )
    title = f"全服总榜 - {date_str}"
    footer = (
        '<div class="footer">由 astrbot_plugin_anime_daily 生成 · '
        f'{_esc(datetime.now().strftime("%Y-%m-%d %H:%M"))}</div>'
    )
    return PAGE_HEAD.replace("{{ title }}", _esc(title)) + body + footer + "</div></body></html>\n"


def save_html_to_file(html_str: str, output_dir: str | Path, prefix: str) -> Path:
    """保存 HTML 到指定目录,文件名带时间戳,返回绝对路径。"""
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    fname = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.html"
    out = p / fname
    out.write_text(html_str, encoding="utf-8")
    return out.resolve()
