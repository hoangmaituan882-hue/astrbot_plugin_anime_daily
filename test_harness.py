"""测试用 fixture 与 fake provider。

供 /anime test 指令和 tests/test_e2e.py 集成测试共用。

设计:
- TEST_GROUP_PREFIX: 测试群 ID 前缀(永远不会被真实消息撞)
- TEST_DATE_OFFSET_DAYS: 用相对当前日期 +N 天的日期,避免污染真实数据
- FakeLLMProvider: 通过 text_chat() 返回预设 JSON,模拟不同 LLM 场景
- FakeContext: 替换 self.context 的 send_message / get_using_provider
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

TEST_GROUP_PREFIX = "__test_anime__"
TEST_USER_PREFIX = "__test_user__"


def make_test_date(offset_days: int = 0) -> str:
    """生成测试用日期字符串(YYYY-MM-DD)。offset_days=0 表示今天,+1 明天。"""
    from datetime import datetime, timedelta

    dt = datetime.now() + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%d")


def make_test_group_id(scenario: str, offset_days: int = 0) -> str:
    """生成测试群 ID(带场景名 + 日期偏移,便于区分)。"""
    return f"{TEST_GROUP_PREFIX}_{scenario}_d{offset_days}"


def make_test_umo(group_id: str) -> str:
    """生成测试用 unified_msg_origin。"""
    return f"test:GroupMessage:{group_id}"


@dataclass
class FakeMessageResult:
    """模拟 LLM 调用的 completion_text 容器。"""

    completion_text: str = ""
    role: str = "assistant"


class FakeLLMProvider:
    """模拟 LLM provider,按 group_id 路由到不同预设输出。

    路由规则(在 set_scenario 中预设):
    - "success":    返回完整 is_anime_chunk=True 的分析
    - "no_anime":   返回 is_anime_chunk=False
    - "fail":       返回非 JSON 字符串(模拟 LLM 抽风)
    - "fail_partial": 前 N 次 OK,后 fail(模拟分块部分失败)
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self._by_scenario: dict[str, list[str]] = {}
        self._default_scenario: str = "success"
        self._calls: list[dict] = []  # 记录每次调用(便于测试断言)

    def set_scenario(self, group_id: str, outputs: list[str]) -> None:
        """为指定 group 预设一组 LLM 输出(按调用顺序消费)。"""
        self._by_scenario[group_id] = list(outputs)

    def set_default_scenario(self, outputs: list[str]) -> None:
        """为所有 group 设置默认输出。"""
        self._default_scenario = "default"
        self._by_scenario["default"] = list(outputs)
        self._default_scenario = "default"

    async def text_chat(
        self,
        *,
        prompt: str,
        session_id: Any = None,
        contexts: Any = None,
        image_urls: Any = None,
        func_tool: Any = None,
        system_prompt: str = "",
    ) -> FakeMessageResult:
        self.call_count += 1
        self._calls.append({"prompt_len": len(prompt), "ts": time.time()})
        # 路由:按 group_id(从 prompt 中识别)
        gid = _extract_group_id_from_prompt(prompt)
        outputs = self._by_scenario.get(gid) or self._by_scenario.get(
            "default", []
        )
        if not outputs:
            return FakeMessageResult(
                completion_text=json.dumps(
                    {
                        "is_anime_chunk": False,
                        "anime_user_stats": [],
                        "top_works": [],
                        "summary": "",
                    },
                    ensure_ascii=False,
                )
            )
        # 弹第一个
        out = outputs.pop(0) if outputs else "{}"
        return FakeMessageResult(completion_text=out)


def _extract_group_id_from_prompt(prompt: str) -> str:
    """从 prompt 文本中提取 group_id(测试群路由用)。

    classifier 的 GROUP_PROMPT 模板里包含 `群「{group_name}」`,
    我们用 group_name 作为路由 key(因为 FakeProvider 不直接看到 group_id,
    只看到 prompt 内容)。
    """
    import re

    m = re.search(r"群「([^」]+)」", prompt)
    if m:
        return m.group(1)
    return ""


def make_chunk_success_payload(
    group_id: str = "测试群",
    *,
    is_anime: bool = True,
    summary: str = "今日讨论了孤独摇滚 BD。",
) -> str:
    """构造一个分块级 LLM 成功响应。"""
    payload = {
        "is_anime_chunk": is_anime,
        "anime_user_stats": [
            {
                "user_id": f"{TEST_USER_PREFIX}_1",
                "user_name": "小明",
                "anime_msg_count": 5,
                "related_msg_count": 8,
                "best_quote": "我也觉得孤独摇滚第十集封神",
            }
        ]
        if is_anime
        else [],
        "top_works": [{"work": "孤独摇滚", "count": 6}]
        if is_anime
        else [],
        "summary": summary if is_anime else "",
    }
    return json.dumps(payload, ensure_ascii=False)


def make_chunk_invalid_payload() -> str:
    """构造一个 LLM 失败(非 JSON)响应。"""
    return "抱歉,我无法理解这个请求,请重新组织您的输入。"


def make_global_success_payload() -> str:
    """构造阶段二成功响应。"""
    payload = {
        "global_user_top": [
            {
                "user_id": f"{TEST_USER_PREFIX}_1",
                "user_name": "小明",
                "anime_msg_count": 5,
                "best_quote": "封神",
            }
        ],
        "global_works_top": [{"work": "孤独摇滚", "total_count": 6}],
        "summary": "全服讨论热度上升。",
    }
    return json.dumps(payload, ensure_ascii=False)


@dataclass
class SentMessage:
    """记录一次成功 send_message 调用。"""

    umo: str
    chain: Any
    kind: str = "text"  # text / file / unknown


class FakeContext:
    """替换 plugin.context,记录推送的消息而不真发到平台。"""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self._provider: FakeLLMProvider | None = None

    def set_provider(self, provider: FakeLLMProvider) -> None:
        self._provider = provider

    async def send_message(self, umo: str, chain: Any) -> None:
        # 简化判断 chain 类型
        kind = "text"
        if hasattr(chain, "chain") and chain.chain:
            for comp in chain.chain:
                cls = comp.__class__.__name__
                if cls == "File":
                    kind = "file"
                    break
                if cls == "Plain":
                    kind = "text"
        self.sent.append(SentMessage(umo=umo, chain=chain, kind=kind))

    def get_using_provider(self) -> FakeLLMProvider | None:
        return self._provider


@dataclass
class ScenarioResult:
    """单场景测试结果汇总。"""

    name: str
    passed: bool
    notes: list[str] = field(default_factory=list)
    actual: dict = field(default_factory=dict)

    def to_text(self) -> str:
        emoji = "✅" if self.passed else "❌"
        lines = [f"{emoji} 场景: {self.name}"]
        for n in self.notes:
            lines.append(f"  • {n}")
        if self.actual:
            lines.append(f"  📊 实际: {self.actual}")
        return "\n".join(lines)
