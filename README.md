# astrbot_plugin_anime_daily

> 每天 23:00 自动汇总当日群内动画话题,生成**话痨榜**与**热门作品榜**,并在群内推送**全服总榜**。

## 功能

- 静默监听群聊消息,落库(SQLite,WAL)
- 每天 23:00 自动对当日所有发言进行 LLM 分析归类(单群一次,跨群再汇总一次)
- 30 分钟时间窗分块,超过 max_messages_per_llm_call 自动切
- 输出本群话痨榜 + 热门作品榜;跨群再发全服总榜
- 当日无动画话题时推送 今日无动画话题
- 历史可查:`/anime today|group|user|global|preview|sid|help`
- 配置可视化:WebUI 直接改 `推送时间 / 启用群 / TOP N / 分批阈值 / 黑白名单模式 / 报告格式` 等
- 支持 **text**(纯文本)和 **html**(精美 Jinja2 报告)两种报告格式
- 三种名单模式: **whitelist** / **blacklist** / **none**(全放行)

## 配置文件

详见 `_conf_schema.json` 各字段说明。

## 指令

| 指令 | 说明 |
|------|------|
| `/anime help` | 显示帮助 |
| `/anime sid` | 查看当前会话 unified_msg_origin(用于白名单配置) |
| `/anime today` | 本群当日榜 |
| `/anime group <日期 YYYY-MM-DD>` | 本群指定日榜 |
| `/anime user <user_id> [日期]` | 某用户发言统计 |
| `/anime global [日期]` | 全服总榜 |
| `/anime preview` | 立刻跑一次今日分析(发给你,不推群) |

## 名单模式

`group_list_mode` 三选一:

- **whitelist**(默认):只在 `enabled_groups` 列表里的群才会被分析/推送。
- **blacklist**:`enabled_groups` 列表里的群**排除**;其他都启用。
- **none**:忽略 `enabled_groups`,所有群都启用。

获取群 ID:在群里发 `/anime sid`,把输出里的 `unified_msg_origin` 粘到配置里。

## 报告格式

`report_format` 二选一:

- **text**(默认):纯文本,所有平台通用。
- **html**:生成内联 CSS 的 Jinja2 报告,作为 `.html` 文件发送(可关闭改用文本提示)。
  - 文件存放在 `<data>/astrbot_plugin_anime_daily/html_reports/`
  - 可自行用 Nginx / Cloudflare Tunnel 暴露为外链

## 数据存储

SQLite 数据库位于 `<AstrBot>/data/astrbot_plugin_anime_daily/anime.db`,符合开发原则。
- `messages`: 当日(及历史)全部消息,带 `date_str` 索引,便于按日查询;含 `umo` 字段用于推送。
- `analysis_cache`: LLM 分析结果缓存(避免重复计算)。
- `push_log`: 推送日志(启动补推去重)。

## 依赖

- `aiosqlite`: 异步 SQLite 客户端。

## 借鉴与致谢

设计参考了 [SXP-Simon/astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis) 的黑白名单、会话标识、T2I 报告等思路。
本插件定位更轻量:聚焦"动画话题",不做全量群分析,只跑 LLM 一次(单群) + 一次(跨群),渲染走轻量 Jinja2(无 Playwright 依赖)。

## 未来计划

- [ ] 增量分析(滑动窗口,大群友好) — 参考 SXP-Simon 实现
- [ ] 接入 T2I 服务生成图片报告(可选)
- [ ] 飞书/钉钉平台适配(目前主要为 QQ / Telegram)
- [ ] 关键发言人画像(LLM 给活跃用户打标签)

## 开发与发布

```bash
pip install -r requirements.txt
ruff format .
ruff check .
python tests/test_local.py
```
