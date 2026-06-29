"""配置读取与校验工具。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GroupListMode = Literal["whitelist", "blacklist", "none"]
ReportFormat = Literal["text", "html"]


@dataclass
class PluginConfig:
    """从 _conf_schema.json 加载并经过类型校验的运行时配置。"""

    enabled_groups: list[str] = field(default_factory=list)
    group_list_mode: GroupListMode = "whitelist"
    push_time: str = "23:00"
    top_n_users: int = 10
    top_n_works: int = 5
    top_n_global_users: int = 10
    top_n_global_works: int = 8
    include_global_in_group: bool = True
    report_format: ReportFormat = "text"
    html_send_as_file: bool = True
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
        mode = str(raw.get("group_list_mode", "whitelist")).lower()
        if mode not in ("whitelist", "blacklist", "none"):
            mode = "whitelist"
        fmt = str(raw.get("report_format", "text")).lower()
        if fmt not in ("text", "html"):
            fmt = "text"
        return cls(
            enabled_groups=list(raw.get("enabled_groups") or []),
            group_list_mode=mode,  # type: ignore[arg-type]
            push_time=str(raw.get("push_time", "23:00")),
            top_n_users=int(raw.get("top_n_users", 10)),
            top_n_works=int(raw.get("top_n_works", 5)),
            top_n_global_users=int(raw.get("top_n_global_users", 10)),
            top_n_global_works=int(raw.get("top_n_global_works", 8)),
            include_global_in_group=bool(raw.get("include_global_in_group", True)),
            report_format=fmt,  # type: ignore[arg-type]
            html_send_as_file=bool(raw.get("html_send_as_file", True)),
            push_on_empty=bool(raw.get("push_on_empty", True)),
            push_on_error=bool(raw.get("push_on_error", True)),
            quiet_min_words=int(raw.get("quiet_min_words", 2)),
            max_messages_per_llm_call=int(raw.get("max_messages_per_llm_call", 300)),
            llm_temperature=float(raw.get("llm_temperature", 0.1)),
            summary_max_words=int(raw.get("summary_max_words", 60)),
            max_concurrent_llm=max(1, int(raw.get("max_concurrent_llm", 3))),
        )

    def is_group_enabled(self, group_id: str | None) -> bool:
        """L1:基于 group_list_mode 判断。

        - whitelist: 只在 enabled_groups 内的群启用
        - blacklist: 不在 enabled_groups 内的群启用
        - none: 全部启用(忽略 enabled_groups)
        """
        if self.group_list_mode == "none":
            return True
        if not group_id:
            # 没有 group_id(私聊等)由调用方前置过滤,这里兜底拒绝
            return False
        in_list = group_id in self.enabled_groups
        if self.group_list_mode == "whitelist":
            return in_list
        if self.group_list_mode == "blacklist":
            return not in_list
        return True

    def get_push_hour_minute(self) -> tuple[int, int]:
        """解析 HH:MM,失败时返回 (23, 0)。"""
        try:
            hh, mm = self.push_time.split(":", 1)
            return int(hh), int(mm)
        except Exception:
            return 23, 0
