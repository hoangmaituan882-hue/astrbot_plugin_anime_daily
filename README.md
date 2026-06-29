# astrbot_plugin_anime_daily

> 每天 23:00 自动汇总当日群内动画话题,生成**话痨榜**与**热门作品榜**,并在群内推送**全服总榜**。

## 功能

- 静默监听群聊消息(不打扰,不消耗 LLM)。
- 每天 23:00 自动对当日所有发言进行 LLM 分析归类(单群一次,跨群再汇总一次)。
- 自动识别动画相关话题(作品/角色/声优/制作/二创/OPED/BD 等),作品名自动归一化。
- 双榜输出:本群话痨榜 + 热门作品榜;全服总榜同步推送。
- 当日无动画话题时,发 `📭 今日无动画话题`。
- 历史可查:`/anime today|group|user|global|preview`。
- 配置可视化:WebUI 直接改 `推送时间 / 启用群 / TOP N / 分批阈值` 等。

## 配置文件

详见 `_conf_schema.json` 各字段说明。

## 指令

| 指令 | 说明 |
|------|------|
| `/anime today` | 本群当日榜 |
| `/anime group <日期 YYYY-MM-DD>` | 本群指定日榜 |
| `/anime user <user_id> [日期]` | 某用户发言统计 |
| `/anime global [日期]` | 全服总榜 |
| `/anime preview` | 立刻跑一次今日分析(发给你,不推群) |

## 数据存储

SQLite 数据库位于 `<AstrBot>/data/astrbot_plugin_anime_daily/anime.db`,符合开发原则。
- `messages`: 当日(及历史)全部消息,带 `date_str` 索引,便于按日查询。
- `analysis_cache`: LLM 分析结果缓存(避免重复计算)。
- `push_log`: 推送日志(启动补推去重)。

## 依赖

- `aiosqlite`: 异步 SQLite 客户端。

## 开发与发布

```bash
pip install -r requirements.txt
ruff format .
ruff check .
```
