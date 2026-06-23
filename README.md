# 抖音罗盘短视频榜 TOP200 监控系统

基于 Python + Playwright 的抖音罗盘 V2 监控工具。系统复用已登录的独立 Chrome Profile 采集「短视频榜」TOP200，按轮次写入 SQLite，与上一轮快照差分，识别新进榜和排名急升商品，并同步到企微/飞书表。

## 当前生产流程

| 流程 | 命令 | scope 前缀 | 输出 |
|---|---|---|---|
| 大盘主线 | `python run.py --multi` | `video_order` | 企微摘要、主飞书表、可选企微智能表格/Excel |
| 服配支线 | `python run.py --acc` | `video_acc` | 企微消息 + 服配飞书表 `LARK_ACC_TABLE_ID` |
| 单 scope 兼容 | `python run.py --scope video_order` | 用户指定 | 兼容调试入口 |

旧商品卡榜 `card_order` 不是 V2 默认流程，仅保留兼容。

## 快速上手

```bash
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
python run.py --login
```

首次登录会打开浏览器，把抖音罗盘登录态保存到 `BROWSER_USER_DATA_DIR`。该目录必须是 Playwright 专用 Profile，不要使用日常 Chrome Profile。

运行 V2 主线：

```bash
python run.py --multi
python run.py --multi --dry-run
```

运行服配支线：

```bash
python run.py --acc
python run.py --acc --dry-run
```

本机定时任务使用 Accio 预装 Python，它已包含 Playwright/openpyxl：

```powershell
& "C:\Users\Administrator.DESKTOP-GRHN4PA\AppData\Roaming\Accio\pre-install\e6550f7e00ff\python\python.exe" run.py --multi
```

## 配置

所有配置都在 `.env`，由 `config/settings.py` 统一读取。

| 变量 | 说明 |
|---|---|
| `BROWSER_USER_DATA_DIR` | Playwright 专用 Chrome Profile，必填 |
| `RANK_API_PATH` | 默认 `video_bring_good` |
| `RANK_TAB_TEXT` | 默认 `短视频榜` |
| `TARGET_L1_CATEGORIES` | `--multi` 采集的一级类目列表，`*` 表示账号可见全部 |
| `ACC_PATH` | 服配路径，默认 `服饰内衣,服装,服装配饰` |
| `ACC_LEAF_NAMES` | 服配目标叶子，默认 5 个 |
| `LARK_BASE_APP_TOKEN` / `LARK_TABLE_ID` | 大盘飞书表 |
| `LARK_ACC_TABLE_ID` | 服配独立飞书表 |
| `WECOM_WEBHOOK_URL` | 企微摘要 Webhook |

## 差分事件

排名数值越小越好，`delta = 上轮排名 - 本轮排名`。

| 事件类型 | 触发条件 |
|---|---|
| `NEW_ENTRY` | 本轮出现、上轮不在该 scope |
| `RANK_UP_50` | 排名上升 50-99 位 |
| `RANK_UP_100` | 排名上升 100-149 位 |
| `RANK_UP_150` | 排名上升 150 位及以上 |

事件互斥，按 `RANK_UP_150 > RANK_UP_100 > RANK_UP_50 > NEW_ENTRY` 匹配。首轮只建立 baseline，不推送。

## 服配支线说明

短视频榜接口只接受二级类目 `category_id`。把 `category_id` 换成 L3 或叶子 ID 会返回空，接口也会忽略额外的 `leaf_category_id` 参数。

因此 `--acc` 的真实流程是：

1. 从 `data/category_raw_dump.json` 解析 `服饰内衣 > 服装 > 服装配饰` 下的目标叶子 ID。
2. 只采一次 `服饰内衣 > 服装` L2 短视频榜 TOP200。
3. 按接口返回的 `product_info.leaf_category_id` 本地拆分到 `video_acc_帽子` 等 scope。
4. 有事件时推送企微消息并写 `LARK_ACC_TABLE_ID`，不写大盘表。

如果日志显示“当前 L2 TOP200 内无目标叶子商品”，服配表为空是正常结果，不是飞书写入失败。

## 数据库

核心表：

- `products_snapshot`: 每轮榜单快照，`UNIQUE (run_id, scope_key, product_id)`。
- `ranking_event`: 差分事件，`UNIQUE (run_id, scope_key, event_type, product_id)`。

关键字段包括 `image`、`pay_amount`、`price`、`category_l3_name`、`leaf_category_name`。`get_latest_two_run_ids` 按 `scope_key` 隔离比较；服配补发窗口按 `video_acc` 前缀隔离，避免被大盘轮次挤出。

## 常用命令

```bash
# 查看某个 scope 历史轮次
python run.py --list-runs --scope video_order

# mock 验证
python run.py --mock --dry-run
python run.py --acc --mock --dry-run

# 单元测试
python -m pytest tests/test_category_discovery.py tests/test_collector_url.py tests/test_diff.py -v

# 查看日志
type data\cron.log
```

## 定时任务

`run_cron.bat` 应执行 V2 主线：

```bat
call "C:\Users\Administrator.DESKTOP-GRHN4PA\AppData\Roaming\Accio\pre-install\e6550f7e00ff\python\python.exe" run.py --multi >> "D:\workspace\claude\code\luopan\data\cron.log" 2>&1
```

服配如需独立定时，另建一条任务执行：

```bat
call "C:\Users\Administrator.DESKTOP-GRHN4PA\AppData\Roaming\Accio\pre-install\e6550f7e00ff\python\python.exe" run.py --acc >> "D:\workspace\claude\code\luopan\data\cron_acc.log" 2>&1
```

不要并发运行多个采集实例；persistent Chrome Profile 是独占资源。

## 故障判断

- `ModuleNotFoundError: playwright`: 当前 Python 环境不对，改用 Accio 预装 Python 或安装依赖。
- 采集 0 条或跳登录页：Cookie 失效，运行 `python run.py --login`。
- DOM 降级只拿 10 条：API 没捕获成功，不算有效采集。
- 支付金额/价格为区间：罗盘平台脱敏；详情页拓价成功时才会回填真实到手价。
- 服配支线不生成 Excel，也不写企微智能表格；企微只发 Webhook 消息。
