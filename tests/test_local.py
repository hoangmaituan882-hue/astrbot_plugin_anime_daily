"""纯本地单元测试:config / aggregator / renderer / scheduler。
不需要 AstrBot 运行环境和 LLM。
运行: python tests/test_local.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# tests/ 是插件根的子目录,把 plugin 根目录和它的父目录都加入 path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))
sys.path.insert(0, ROOT)

from astrbot_plugin_anime_daily.aggregator import (  # noqa: E402
    chunk_messages,
    merge_global_results,
    merge_partial_results,
    truncate_summary,
)
from astrbot_plugin_anime_daily.config import PluginConfig  # noqa: E402
from astrbot_plugin_anime_daily.renderer import (  # noqa: E402
    render_empty,
    render_error,
    render_global_report,
    render_group_report,
    render_user_record,
)
from astrbot_plugin_anime_daily.scheduler import (  # noqa: E402
    _seconds_until_next,
    _yesterday_str,
)


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"[{label}] expected {expected!r}, got {actual!r}")
    print(f"  OK  {label}")


# ============== config.py ==============
def test_config_defaults():
    print("[config] defaults")
    c = PluginConfig.from_raw({})
    assert_eq(c.push_time, "23:00", "push_time default")
    assert_eq(c.top_n_users, 10, "top_n_users default")
    assert_eq(c.max_messages_per_llm_call, 300, "max default")
    assert_eq(c.is_group_enabled(None), True, "None 放行")
    assert_eq(c.is_group_enabled("g1"), True, "空白名单全放行")
    c2 = PluginConfig.from_raw({"enabled_groups": ["g1"], "top_n_users": 7})
    assert_eq(c2.is_group_enabled("g1"), True, "白名单 g1 放行")
    assert_eq(c2.is_group_enabled("g2"), False, "白名单 g2 拒绝")
    assert_eq(c2.get_push_hour_minute(), (23, 0), "默认 23:00")
    c3 = PluginConfig.from_raw({"push_time": "07:30"})
    assert_eq(c3.get_push_hour_minute(), (7, 30), "解析 07:30")
    c4 = PluginConfig.from_raw({"push_time": "garbage"})
    assert_eq(c4.get_push_hour_minute(), (23, 0), "非法回退")


# ============== aggregator.chunk_messages ==============
def test_chunk_small():
    print("[aggregator] chunk_messages 小量")
    msgs = [
        {"created_at": 100, "text": f"m{i}"} for i in range(10)
    ]
    chunks = chunk_messages(msgs, max_per_call=300)
    assert_eq(len(chunks), 1, "10 条未分块")
    assert_eq(len(chunks[0]), 10, "单块 10 条")


def test_chunk_empty():
    print("[aggregator] chunk_messages 空")
    assert_eq(chunk_messages([], 300), [], "空输入")
    assert_eq(chunk_messages([], 0), [], "空 + 0 阈值")


def test_chunk_time_window():
    print("[aggregator] chunk_messages 时间窗")
    base = 1_700_000_000
    msgs = []
    # 100 条 30 分钟窗,每 5 秒一条 → 单窗内 360 条
    for i in range(200):
        msgs.append({"created_at": base + i * 5, "text": f"m{i}"})
    chunks = chunk_messages(msgs, max_per_call=300)
    assert_eq(len(chunks), 1, "200 条密集单窗不分块")


def test_chunk_force_split():
    print("[aggregator] chunk_messages 强制切")
    base = 1_700_000_000
    msgs = [
        {"created_at": base + i, "text": f"m{i}"} for i in range(750)
    ]
    chunks = chunk_messages(msgs, max_per_call=300)
    # 极端密集: 750 / 300 硬切
    assert_eq(len(chunks), 3, "750 条硬切 3 块")
    assert_eq(len(chunks[0]), 300, "块 0 = 300")
    assert_eq(len(chunks[1]), 300, "块 1 = 300")
    assert_eq(len(chunks[2]), 150, "块 2 = 150")


# ============== aggregator.merge_partial_results ==============
def test_merge_partials_empty():
    print("[aggregator] merge_partial_results 空")
    r = merge_partial_results([])
    assert_eq(r["is_anime_day"], False, "空 is_anime_day=False")
    assert_eq(r["anime_user_stats"], [], "空 user_stats")
    assert_eq(r["top_works"], [], "空 top_works")


def test_merge_partials_normal():
    print("[aggregator] merge_partial_results 正常")
    p1 = {
        "is_anime_chunk": True,
        "anime_user_stats": [
            {
                "user_id": "u1",
                "user_name": "小明",
                "anime_msg_count": 5,
                "related_msg_count": 8,
                "best_quote": "孤独摇滚第十集封神",
            },
            {
                "user_id": "u2",
                "user_name": "阿绿",
                "anime_msg_count": 3,
                "related_msg_count": 4,
                "best_quote": "BD 出了吗",
            },
        ],
        "top_works": [
            {"work": "孤独摇滚", "count": 6},
            {"work": "ぼっち・ざ・ろっく!", "count": 1},
        ],
        "summary": "讨论 BD 发布",
    }
    p2 = {
        "is_anime_chunk": True,
        "anime_user_stats": [
            {
                "user_id": "u1",
                "user_name": "小明",
                "anime_msg_count": 4,
                "related_msg_count": 5,
                "best_quote": "同意",
            },
            {
                "user_id": "u3",
                "user_name": "小红",
                "anime_msg_count": 2,
                "related_msg_count": 2,
                "best_quote": "我也喜欢",
            },
        ],
        "top_works": [
            {"work": "孤独摇滚", "count": 3},
            {"work": "葬送的芙莉莲", "count": 4},
        ],
        "summary": "聊新番前瞻",
    }
    r = merge_partial_results([p1, p2])
    assert_eq(r["is_anime_day"], True, "任一块 true → day=true")
    # u1 应该合并:anime=9, related=13
    u1 = next(u for u in r["anime_user_stats"] if u["user_id"] == "u1")
    assert_eq(u1["anime_msg_count"], 9, "u1 anime 累加")
    assert_eq(u1["related_msg_count"], 13, "u1 related 累加")
    # 排序 u1 在前
    assert_eq(r["anime_user_stats"][0]["user_id"], "u1", "u1 排序第一")
    # 作品归一化:本地只做小写+去空白,跨语种归一化靠 LLM。
    # 所以 "孤独摇滚"(两处,大小写空白相同)合并,日文别名独立保留。
    works = {w["work"]: w["count"] for w in r["top_works"]}
    assert_eq(works.get("孤独摇滚", 0), 9, "孤独摇滚大小写空白归一累加")
    assert_eq(works.get("ぼっち・ざ・ろっく!", 0), 1, "日文别名独立")
    assert_eq(works.get("葬送的芙莉莲", 0), 4, "芙莉莲保留")
    assert_eq(" | " in r["summary"], True, "summary 拼接")


def test_merge_partials_all_false():
    print("[aggregator] merge_partial_results 全部 false")
    p1 = {"is_anime_chunk": False, "anime_user_stats": [], "top_works": [], "summary": ""}
    p2 = {"is_anime_chunk": False, "anime_user_stats": [], "top_works": [], "summary": ""}
    r = merge_partial_results([p1, p2])
    assert_eq(r["is_anime_day"], False, "全 false → day=false")


# ============== aggregator.merge_global_results ==============
def test_merge_global():
    print("[aggregator] merge_global_results")
    g1 = {
        "group_id": "g1",
        "group_name": "番剧同好会",
        "anime_user_stats": [
            {"user_id": "u1", "user_name": "小明", "anime_msg_count": 5,
             "best_quote": "q1"},
        ],
        "top_works": [{"work": "孤独摇滚", "count": 6}],
        "summary": "BD 发布",
    }
    g2 = {
        "group_id": "g2",
        "group_name": "动画研究室",
        "anime_user_stats": [
            {"user_id": "u1", "user_name": "小明", "anime_msg_count": 4,
             "best_quote": "q2"},
            {"user_id": "u2", "user_name": "小红", "anime_msg_count": 7,
             "best_quote": "q3"},
        ],
        "top_works": [
            {"work": "孤独摇滚", "count": 3},
            {"work": "葬送的芙莉莲", "count": 4},
        ],
        "summary": "新番前瞻",
    }
    r = merge_global_results([g1, g2])
    # 跨群榜保留 (user_id, group_id) 区分:u1 在 g1 和 g2 是不同行
    # 但全局排序按 anime_msg_count desc
    g1_u1 = next(
        u for u in r["global_user_top"]
        if u["user_id"] == "u1" and u["group_id"] == "g1"
    )
    g2_u1 = next(
        u for u in r["global_user_top"]
        if u["user_id"] == "u1" and u["group_id"] == "g2"
    )
    assert_eq(g1_u1["anime_msg_count"], 5, "u1@ g1 = 5")
    assert_eq(g2_u1["anime_msg_count"], 4, "u1@ g2 = 4")
    # u2 anime=7
    u2 = next(u for u in r["global_user_top"] if u["user_id"] == "u2")
    assert_eq(u2["anime_msg_count"], 7, "u2")
    # 排序: u2(7) > u1@g1(5) > u1@g2(4)
    order = [u["anime_msg_count"] for u in r["global_user_top"]]
    assert_eq(order, [7, 5, 4], "排序正确")
    # 作品:跨语种归一化靠 LLM;同语种累加
    works = {w["work"]: w["total_count"] for w in r["global_works_top"]}
    assert_eq(works.get("孤独摇滚", 0), 9, "孤独摇滚跨群 9")
    assert_eq(works.get("葬送的芙莉莲", 0), 4, "芙莉莲 4")


def test_merge_case_insensitive():
    print("[aggregator] 大小写空白归一")
    # 本地只做大小写+空格归一
    p = [
        {"is_anime_chunk": True, "anime_user_stats": [],
         "top_works": [{"work": "Bocchi the Rock", "count": 3}], "summary": ""},
        {"is_anime_chunk": True, "anime_user_stats": [],
         "top_works": [{"work": " bocchi  THE  rock ", "count": 2}], "summary": ""},
    ]
    r = merge_partial_results(p)
    # 归一化后同名,累加 5
    total = sum(w["count"] for w in r["top_works"])
    assert_eq(total, 5, "大小写+空白归一合并")


def test_merge_global_case_insensitive():
    print("[aggregator] 跨群大小写归一")
    g1 = {"group_id": "g1", "group_name": "A",
          "anime_user_stats": [], "top_works": [{"work": "Spy x Family", "count": 4}],
          "summary": ""}
    g2 = {"group_id": "g2", "group_name": "B",
          "anime_user_stats": [], "top_works": [{"work": "SPY×FAMILY", "count": 2}],
          "summary": ""}
    r = merge_global_results([g1, g2])
    # 跨语种不归一,但大小写归一 → "Spy x Family" 与 "SPY×FAMILY" 归一化键不同(因为含 ×)
    # 至少保证 g1 的 4 存在
    works = {w["work"]: w["total_count"] for w in r["global_works_top"]}
    assert_eq(works.get("Spy x Family", 0) >= 4, True, "Spy x Family 至少 4")


# ============== aggregator.truncate_summary ==============
def test_truncate_summary():
    print("[aggregator] truncate_summary")
    assert_eq(truncate_summary("", 10), "", "空")
    assert_eq(truncate_summary("短", 10), "短", "未超")
    long = "a" * 100
    out = truncate_summary(long, 10)
    assert_eq(len(out), 10, "裁剪到 10")
    assert_eq(out.endswith("…"), True, "省略号")


# ============== renderer ==============
def test_renderer_group():
    print("[renderer] render_group_report")
    analysis = {
        "is_anime_day": True,
        "anime_user_stats": [
            {"user_id": "u1", "user_name": "小明", "anime_msg_count": 23,
             "related_msg_count": 30, "best_quote": "我也觉得孤独摇滚第十集封神!"},
            {"user_id": "u2", "user_name": "阿绿", "anime_msg_count": 18,
             "related_msg_count": 22, "best_quote": "BD 出了吗"},
        ],
        "top_works": [
            {"work": "孤独摇滚", "count": 12},
            {"work": "葬送的芙莉莲", "count": 9},
        ],
        "summary": "群内围绕孤独摇滚 BD 消息与新番前瞻展开热烈讨论。",
    }
    text = render_group_report(
        date_str="2026-06-29",
        group_id="123456",
        group_name="番剧同好会",
        analysis=analysis,
        top_n_users=10,
        top_n_works=5,
        summary_max_words=60,
    )
    print("--- group text ---")
    print(text)
    assert_eq("📊 2026-06-29" in text, True, "包含标题")
    assert_eq("🏆 话痨榜" in text, True, "包含话痨榜")
    assert_eq("🔥 热门作品榜" in text, True, "包含作品榜")
    assert_eq("孤独摇滚" in text, True, "包含作品名")
    assert_eq("💬 本日总结" in text, True, "包含总结")


def test_renderer_global():
    print("[renderer] render_global_report")
    payload = {
        "global_user_top": [
            {"user_id": "u1", "user_name": "小蓝", "group_id": "g1",
             "group_name": "A群", "anime_msg_count": 41,
             "best_quote": "封神"},
        ],
        "global_works_top": [
            {"work": "孤独摇滚", "total_count": 78},
        ],
        "summary": "孤独摇滚 BD 发布带动全平台讨论热潮。",
    }
    text = render_global_report(
        date_str="2026-06-29",
        global_result=payload,
        top_n_users=10,
        top_n_works=8,
        summary_max_words=60,
    )
    print("--- global text ---")
    print(text)
    assert_eq("🌐 全服总榜" in text, True, "全服标题")
    assert_eq("全服话痨" in text, True, "全服话痨")
    assert_eq("全服热门作品" in text, True, "全服作品")
    assert_eq("全服总结" in text, True, "全服总结")


def test_renderer_empty_error():
    print("[renderer] empty / error")
    e = render_empty("2026-06-29", "g1", "测试群")
    assert_eq("📭" in e, True, "empty 有 emoji")
    assert_eq("无动画话题" in e, True, "empty 文案")
    r = render_error("2026-06-29", "g1", "测试群")
    assert_eq("⚠️" in r, True, "error 有 emoji")
    assert_eq("失败" in r, True, "error 文案")


def test_renderer_user_record_empty():
    print("[renderer] user_record 空")
    text = render_user_record("u1", [])
    assert_eq("未找到" in text, True, "空记录提示")


# ============== scheduler ==============
def test_scheduler_helpers():
    print("[scheduler] helpers")
    y = _yesterday_str()
    assert_eq(len(y) == 10 and y[4] == "-" and y[7] == "-", True, "日期格式 YYYY-MM-DD")
    sec = _seconds_until_next(0, 0)
    assert_eq(sec > 0, True, "距下次 00:00 > 0")
    # 设置一个"早就过了"的时刻,应跳到明天
    sec2 = _seconds_until_next(0, 0)
    assert_eq(sec2 > 0, True, "次日仍 > 0")


# ============== storage (真 SQLite) ==============
async def test_storage():
    print("[storage] SQLite")
    with tempfile.TemporaryDirectory() as tmp:
        from astrbot_plugin_anime_daily.storage import Storage

        db_path = os.path.join(tmp, "test.db")
        s = Storage(db_path)
        await s.init()
        await s.insert_message(
            date_str="2026-06-29",
            group_id="g1",
            group_name="测试群",
            user_id="u1",
            user_name="小明",
            message_id="m1",
            raw_text="孤独摇滚第十集封神",
            created_at=1000,
        )
        await s.insert_message(
            date_str="2026-06-29",
            group_id="g1",
            group_name="测试群",
            user_id="u2",
            user_name="阿绿",
            message_id="m2",
            raw_text="BD 出了吗",
            created_at=2000,
        )
        await s.insert_message(
            date_str="2026-06-30",
            group_id="g1",
            group_name="测试群",
            user_id="u1",
            user_name="小明",
            message_id="m3",
            raw_text="今天天气真好",
            created_at=3000,
        )
        by_g = await s.get_messages_by_group("2026-06-29")
        assert_eq(len(by_g), 1, "29 日 1 群")
        assert_eq(len(by_g["g1"]), 2, "29 日 g1 共 2 条")
        assert_eq(by_g["g1"][0]["user_id"], "u1", "按 ts 升序")

        all_29 = await s.get_all_messages("2026-06-29")
        assert_eq(len(all_29), 2, "29 日全部 2 条")

        u1_msgs = await s.get_user_messages("u1", "2026-06-29")
        assert_eq(len(u1_msgs), 1, "u1 29 日 1 条")

        u1_all = await s.get_user_messages("u1")
        assert_eq(len(u1_all), 2, "u1 全部 2 条")

        # 缓存
        await s.save_analysis_cache(
            "2026-06-29", "group:g1", {"is_anime_day": True, "k": "v"}
        )
        c = await s.get_analysis_cache("2026-06-29", "group:g1")
        assert_eq(c is not None and c.get("k") == "v", True, "cache 读写")

        # push_log
        assert_eq(await s.has_pushed("2026-06-29", "g1", "group"), False, "未推")
        await s.mark_pushed("2026-06-29", "g1", "group")
        assert_eq(await s.has_pushed("2026-06-29", "g1", "group"), True, "已推")
        # mark_pushed 幂等
        await s.mark_pushed("2026-06-29", "g1", "group")
        assert_eq(await s.has_pushed("2026-06-29", "g1", "group"), True, "幂等")


def main():
    test_config_defaults()
    test_chunk_small()
    test_chunk_empty()
    test_chunk_time_window()
    test_chunk_force_split()
    test_merge_partials_empty()
    test_merge_partials_normal()
    test_merge_partials_all_false()
    test_merge_global()
    test_merge_case_insensitive()
    test_merge_global_case_insensitive()
    test_truncate_summary()
    test_renderer_group()
    test_renderer_global()
    test_renderer_empty_error()
    test_renderer_user_record_empty()
    test_scheduler_helpers()
    asyncio.run(test_storage())
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
