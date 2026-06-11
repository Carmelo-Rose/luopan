# 抖音罗盘商品榜 TOP200 监控系统

基于 Python + Playwright 的抖音罗盘「商品卡榜」自动监控工具。每小时整点采集 TOP200，与上轮快照做差分，新进榜和排名急升的商品自动推送企微/飞书预警。

---

## 目录

- [功能概述](#功能概述)
- [项目结构](#项目结构)
- [运行流程](#运行流程)
- [快速上手](#快速上手)
- [配置说明](#配置说明)
- [预警事件类型](#预警事件类型)
- [数据库结构](#数据库结构)
- [常用命令](#常用命令)
- [定时任务](#定时任务)
- [注意事项](#注意事项)

---

## 功能概述

| 功能 | 说明 |
|------|------|
| 自动采集 | Playwright 复用已登录 Chrome Profile，无需重复扫码 |
| 翻页策略 | 首页拦截 XHR 接口 URL，后续 19 页通过 `page.evaluate fetch` 直接调用，每页间隔 1.5s |
| 数据持久化 | SQLite 保存每轮完整快照，支持历史回溯 |
| 差分引擎 | 纯函数设计，对比相邻两轮 rank 变化，精确识别 5 类事件 |
| 去重保护 | DB 层 `UNIQUE` 约束 + `INSERT OR IGNORE`，同一商品同一轮不会重复预警 |
| 推送渠道 | 企业微信 Webhook（已接入）、飞书 Webhook（备用存根） |
| 首轮保护 | 第一次运行只建立 baseline，不发送任何预警 |

---

## 项目结构

```
luopan/
├── run.py                      # 启动入口（解决 Windows 路径问题）
├── main.py                     # 主流程：采集 → 存快照 → 差分 → 推送
├── requirements.txt
├── conftest.py                 # pytest 路径配置
├── .env                        # 本地配置（不提交 Git）
├── .env.example                # 配置模板
│
├── config/
│   └── settings.py             # 统一读取 .env，提供类型化配置项
│
├── collector/
│   └── douyin_compass.py       # Playwright 采集器
│                               #   - persistent_context 复用登录态
│                               #   - XHR 拦截 product_card_hot_v2 接口
│                               #   - DOM 降级解析（备用）
│
├── db/
│   ├── schema.sql              # 建表 DDL
│   └── database.py             # CRUD 封装（连接/建表/快照读写/事件读写）
│
├── monitor/
│   └── diff.py                 # 差分引擎（纯函数，无 I/O 依赖）
│
├── notify/
│   ├── wecom.py                # 企微 Webhook 推送（超长消息自动分块）
│   ├── lark.py                 # 飞书 Webhook 推送（存根）
│   └── dispatcher.py           # 按 NOTIFY_CHANNEL 路由到对应渠道
│
├── tests/
│   ├── test_diff.py            # 差分引擎单元测试（20 个 case，纯 mock）
│   └── verify_mock.py          # 端到端三轮 mock 验证脚本
│
├── probe_page.py               # 页面结构探查工具（调试用）
│
└── data/
    ├── compass.db              # SQLite 数据库
    ├── browser_profile/        # Playwright 专用 Chrome Profile
    └── cron.log                # 定时任务运行日志
```

---

## 运行流程

```
┌─────────────────────────────────────────────────────────┐
│                      每小时整点触发                        │
└───────────────────────────┬─────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │  Playwright 采集 │
                    │  复用 Chrome    │
                    │  Profile 登录态 │
                    └───────┬────────┘
                            │ 20 页 × 10 条 = 200 条
                    ┌───────▼────────┐
                    │  写入快照 DB    │
                    │  products_     │
                    │  snapshot 表   │
                    └───────┬────────┘
                            │
              ┌─────────────▼──────────────┐
              │       是否为第一轮？          │
              └──────┬──────────┬───────────┘
                   是 │          │ 否
                      │          │
              ┌───────▼──┐  ┌────▼────────────┐
              │  仅建立    │  │  差分引擎         │
              │ baseline  │  │  当前轮 vs 上一轮  │
              │  不推送    │  └────┬────────────┘
              └───────────┘       │ 生成事件列表
                               ┌──▼──────────────┐
                               │  写入事件 DB      │
                               │  ranking_event 表 │
                               │  DB 层去重         │
                               └──┬──────────────┘
                                  │
                           ┌──────▼──────┐
                           │  企微 Webhook │
                           │  推送预警消息  │
                           │  标记已推送    │
                           └─────────────┘
```

---

## 快速上手

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

```bash
copy .env.example .env
```

编辑 `.env`，填入以下关键项：

```env
# Playwright 专用 Profile 目录（与正在运行的 Chrome 隔离）
BROWSER_USER_DATA_DIR=D:\workspace\claude\code\luopan\data\browser_profile

# 企微 Webhook URL
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY
```

### 3. 首次登录（保存抖音登录态）

```bash
python run.py --login
```

程序会打开 Chrome 浏览器，**在 180 秒内完成抖音罗盘登录**（账密或扫码均可），登录态自动保存到 `data/browser_profile`，后续运行不再需要重新登录。

### 4. 第一轮采集（建立 baseline）

```bash
python run.py --scope card_order
```

首次运行只采集数据建立基准，**不发送任何预警**。

### 5. 第二轮起自动差分推送

再次运行同一命令，系统自动与上轮对比并推送有变化的商品：

```bash
python run.py --scope card_order
```

---

## 配置说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BROWSER_USER_DATA_DIR` | Playwright 专用 Chrome Profile 路径 | 无，必填 |
| `BROWSER_CHANNEL` | 浏览器渠道，`chrome` 或 `chromium` | `chrome` |
| `COMPASS_URL` | 罗盘榜单入口 URL | 内置常量 |
| `DB_PATH` | SQLite 数据库路径 | `./data/compass.db` |
| `NOTIFY_CHANNEL` | 推送渠道：`wecom` / `lark` / `none` | `wecom` |
| `WECOM_WEBHOOK_URL` | 企微群机器人 Webhook | 无，推送时必填 |
| `LARK_WEBHOOK_URL` | 飞书群机器人 Webhook | 无，飞书时必填 |
| `PAGE_SIZE` | 每页条数（固定 10） | `10` |
| `TOTAL_PAGES` | 采集页数（20 页 = 200 条） | `20` |

---

## 预警事件类型

差分引擎比较相邻两轮快照，识别以下事件（**排名数值越小越优**）：

| 事件类型 | 触发条件 | 企微标签 |
|---------|---------|---------|
| `NEW_ENTRY` | 本轮出现、上轮不在 TOP200 | 🆕 新进 TOP200 |
| `RANK_UP_10` | 排名上升 10 ~ 19 位 | 📈 排名上升 10+ |
| `RANK_UP_20` | 排名上升 20 ~ 29 位 | 📈 排名上升 20+ |
| `RANK_UP_30_50_WARNING` | 排名上升 30 ~ 50 位 | ⚠️ 排名急升 30-50 |
| `RANK_UP_50_PLUS_WARNING` | 排名上升超过 50 位 | 🚨 排名暴升 50+ |

> 排名下降、小幅波动（delta < 10）不触发任何事件。

**事件优先级**（互斥，高优先级优先匹配）：

```
RANK_UP_50_PLUS_WARNING > RANK_UP_30_50_WARNING > RANK_UP_20 > RANK_UP_10 > NEW_ENTRY
```

---

## 数据库结构

### products_snapshot（榜单快照）

每次采集写入一轮，`run_id` 为 ISO 时间戳字符串。

| 字段 | 类型 | 说明 |
|------|------|------|
| `run_id` | TEXT | 本轮唯一标识（ISO datetime） |
| `scope_key` | TEXT | 榜单维度，如 `card_order` |
| `rank` | INTEGER | 当前排名（1 = 第一） |
| `product_id` | TEXT | 商品 ID |
| `product_title` | TEXT | 商品标题 |
| `product_url` | TEXT | 商品详情页链接 |
| `price_range` | TEXT | 价格区间，如 `¥20.9-¥480` |
| `pay_amount` | TEXT | 支付金额区间（罗盘脱敏） |
| `clicks` | TEXT | 点击次数区间 |
| `conversion_rate` | TEXT | 点击成交转化率区间 |
| `card_order_count` | TEXT | 商品卡成交件数区间 |
| `captured_at` | TEXT | 采集时间（ISO datetime UTC） |

### ranking_event（预警事件）

| 字段 | 类型 | 说明 |
|------|------|------|
| `run_id` | TEXT | 产生事件的轮次 |
| `scope_key` | TEXT | 榜单维度 |
| `event_type` | TEXT | 事件类型（见上表） |
| `product_id` | TEXT | 商品 ID |
| `product_title` | TEXT | 商品标题 |
| `rank_current` | INTEGER | 本轮排名 |
| `rank_previous` | INTEGER | 上轮排名（NEW_ENTRY 为空） |
| `rank_delta` | INTEGER | 上升幅度（正数 = 上升） |
| `notified` | INTEGER | 0 = 待推送，1 = 已推送 |

> 去重键：`UNIQUE (run_id, scope_key, event_type, product_id)`，同一商品同一轮同一事件类型只写入一次。

---

## 常用命令

```bash
# 首次登录，保存抖音登录态
python run.py --login

# 正常采集（自动差分 + 推送）
python run.py --scope card_order

# 试运行：只采集差分，不推送，日志打印事件详情
python run.py --scope card_order --dry-run

# 用 mock 数据验证差分逻辑（不启动浏览器）
python run.py --mock --dry-run

# 查看历史采集轮次
python run.py --list-runs

# 运行单元测试（20 个 case）
python -m pytest tests/test_diff.py -v

# 实时查看定时任务日志
type data\cron.log
```

---

## 定时任务

系统已配置 **每小时整点** 自动运行，日志追加到 `data/cron.log`。

cron 表达式：`0 * * * *`（Asia/Shanghai）

**为什么每小时安全：**
- 每次采集耗时约 40 秒，翻页间隔 1.5 秒，不是高频轰炸
- 使用真实 Chrome Profile + 账号 Cookie，服务器识别为正常商家访问
- 罗盘本身面向商家高频查看，不对正常翻页行为反爬

---

## 注意事项

**1. 数据显示为区间而非精确值**

罗盘对非付费会员展示脱敏区间（如 `10000000~25000000`），这是平台策略，非采集问题。

**2. Cookie 有效期**

登录态一般可持续数天至数周。若采集时出现 `0 条商品` 或程序跳转到登录页，重新执行登录命令：

```bash
python run.py --login
```

**3. 第一轮不推送是正常的**

系统设计上首轮只建立 baseline，从第二轮起才开始差分推送。

**4. 企微消息超长自动分块**

单次预警事件超过 4000 字符时，自动分批发送多条消息。

**5. 不要同时运行多个采集实例**

Playwright persistent_context 独占 Profile 目录，并发运行会报 `ProcessSingleton` 错误。
