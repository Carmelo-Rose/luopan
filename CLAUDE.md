# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

抖音罗盘「商品卡榜」TOP200 监控系统。每小时整点用 Playwright 复用已登录的 Chrome Profile 采集榜单，与上一轮快照差分，把新进榜 / 排名急升的商品推送到企微（飞书为存根）。`README.md`（中文）是最完整的功能与运维文档。

## Commands

```bash
# 安装
pip install -r requirements.txt
playwright install chromium

# 首次登录，保存抖音登录态到 BROWSER_USER_DATA_DIR（打开浏览器，180s 内手动登录）
python run.py --login

# 正常采集：采集 → 写快照 → 差分 → 推送
python run.py --scope card_order

# 试运行：采集 + 差分但不推送、不写 notified
python run.py --scope card_order --dry-run

# 用 mock 数据验证差分逻辑（不启动浏览器）
python run.py --mock --dry-run

# 查看历史采集轮次
python run.py --list-runs

# 单元测试（差分引擎，纯 mock，无 I/O）
python -m pytest tests/test_diff.py -v
python -m pytest tests/test_diff.py::<test_name> -v   # 单个 case

# 端到端三轮 mock 验证
python tests/verify_mock.py
```

**始终用 `python run.py`，不要直接 `python main.py`** — `run.py` 把项目根插入 `sys.path` 以解决 Windows 下的模块路径问题。`conftest.py` 为 pytest 做同样的事。

## Architecture

单向数据流，各阶段通过 SQLite 解耦，无跨阶段内存状态：

```
collector → db.insert_snapshot → db.get_latest_two_run_ids → monitor.diff.compute_diff → db.insert_events → notify.dispatcher
```

`main.run_once()` 是编排者，串起整条链路。关键设计点（多文件协作，单看一处看不出来）：

- **轮次身份是 ISO 时间戳 `run_id`**。`get_latest_two_run_ids` 靠 `ORDER BY run_id DESC` 取最近两轮，依赖时间戳字典序 == 时间序。差分永远是「刚写入的 latest」对比「previous」。
- **首轮 baseline 保护**：`previous_run is None` 时只建快照、直接返回，不差分不推送。`main.py:114`。
- **差分是纯函数**（`monitor/diff.py`，无 I/O、无 DB），所以可被 `test_diff.py` 完全 mock 测试。排名语义：**rank 数值越小越优**，`delta = rank_previous - rank_current`，正数=上升。事件类型互斥，按 `_classify_rank_delta` 的阈值从高到低匹配（50+ > 30-50 > 20 > 10 > NEW_ENTRY）。
- **去重在 DB 层**，不在应用层：两张表都有 `UNIQUE` 约束 + `INSERT OR IGNORE`。`ranking_event` 的去重键是 `(run_id, scope_key, event_type, product_id)`。`insert_events` 用 `conn.total_changes` 差值统计真实写入数 —— 注释解释了为什么不能用 `SELECT changes()`（见 `db/database.py:103`）。
- **推送幂等**：事件落库时 `notified=0`，推送成功后才 `mark_events_notified`。推送失败则保留待下轮重试。`main.py` 只推送 `run_id == 当前轮` 的 pending 事件。
- **渠道路由**：`notify/dispatcher.py` 按 `NOTIFY_CHANNEL`（`wecom`/`lark`/`none`）分发。`wecom.py` 已接入并支持超长消息分块；`lark.py` 是存根。

### Collector（最易碎的部分，`collector/douyin_compass.py`）

采集器是反爬敏感、依赖罗盘私有接口的部分，改动前务必理解：

- 用 `launch_persistent_context` 复用 `BROWSER_USER_DATA_DIR` 里的登录态（**不是**用户日常 Chrome Profile，要隔离，否则 `ProcessSingleton` 冲突）。`headless=False`。
- **翻页策略不点按钮**：第 1 页靠监听 `response` 事件拦截 `product_card_hot_v2` 接口拿到真实 URL；第 2–20 页在页面内 `page.evaluate(fetch(...))` 直接改分页参数请求，借页面上下文自动带 Cookie/Token。每页 `sleep(1.5)` 礼貌间隔。
- **分页参数名动态探测**：`_detect_page_param` 从真实 URL 里按候选列表（`page_no`/`page`/`pageNo`…）识别，避免硬编码与实际不符导致只采到第 1 页。识别不到回退 `page_no`。
- **DOM 降级**：API 拦截失败时 `_parse_dom_rows` 解析表格行（仅第 1 页，字段不全）。
- 数据字段多为**脱敏区间**（如 `10000~25000`），`_range_str` 负责把罗盘的 `value_range` 结构转成可读字符串，`ratio` 单位转百分比。这是平台策略，不是采集 bug。

## Config

全部配置经 `config/settings.py` 从 `.env` 读取（`load_dotenv`），其它模块只 import `settings`，不直接读环境变量。必填项：`BROWSER_USER_DATA_DIR`、推送时 `WECOM_WEBHOOK_URL`。`.env.example` 是模板。`.env` 不提交。

## Gotchas

- 无 git 仓库（本目录未初始化）。
- Cookie 失效表现为采集到 0 条或跳登录页 → 重跑 `python run.py --login`。
- 不要并发跑多个采集实例（persistent_context 独占 Profile 目录）。
- 数据库默认 `data/compass.db`，WAL 模式。`data/` 还存 `browser_profile/` 和 `cron.log`。
- 根目录下 `explore_compass.py` / `inspect_html.py` / `probe_page.py` 及 `compass_*.txt/png`、`captured_requests.json` 等是调试探查产物，非主流程代码。
