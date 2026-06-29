"""配置读取与校验工具。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PluginConfig:
    """从 _conf_schema.json 加载并经过类型校验的运行时配置。"""

    enabled_groups: list[str] = field(default_factory=list)
    push_time: str = "23:00"
    top_n_users: int = 10
    top_n_works: int = 5
    top_n_global_users: int = 10
    top_n_global_works: int = 8
    include_global_in_group: bool = True
    push_on_empty: bool = True
    push_on_error: bool = True
    quiet_min_words: int = 2
    max_messages_per_llm_call: int = 300
    llm_temperature: float = 0.1
    summary_max_words: int = 60
    max_concurrent_llm: int = 3

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "PluginConfig":
        """从 AstrBotConfig(继承自 dict)构造,做基础类型校验与缺省。"""
        return cls(
            enabled_groups=list(raw.get("enabled_groups") or []),
            push_time=str(raw.get("push_time", "23:00")),
            top_n_users=int(raw.get("top_n_users", 10)),
            top_n_works=int(raw.get("top_n_works", 5)),
            top_n_global_users=int(raw.get("top_n_global_users", 10)),
            top_n_global_works=int(raw.get("top_n_global_works", 8)),
            include_global_in_group=bool(raw.get("include_global_in_group", True)),
            push_on_empty=bool(raw.get("push_on_empty", True)),
            push_on_error=bool(raw.get("push_on_error", True)),
            quiet_min_words=int(raw.get("quiet_min_words", 2)),
            max_messages_per_llm_call=int(raw.get("max_messages_per_llm_call", 300)),
            llm_temperature=float(raw.get("llm_temperature", 0.1)),
            summary_max_words=int(raw.get("summary_max_words", 60)),
            max_concurrent_llm=max(1, int(raw.get("max_concurrent_llm", 3))),
        )

    def is_group_enabled(self, group_id: str | None) -> bool:
        """白名单校验:空列表表示全部启用。None(非群消息)直接放行(由调用方前置判断)。"""
        if not self.enabled_groups:
            return True
        return group_id in self.enabled_groups

    def get_push_hour_minute(self) -> tuple[int, int]:
        """解析 HH:MM,失败时返回 (23, 0)。"""
        try:
            hh, mm = self.push_time.split(":", 1)
            return int(hh), int(mm)
        except Exception:
            return 23, 0
