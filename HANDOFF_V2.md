# V2 交接文档（运营需求 + 服配需求）

> 面向接手者（Codex）。记录截至 **2026-06-23** 两条并行需求的全部开发点、问题点、未解决点。
> 读完本文 + `CLAUDE.md` + `README.md` 即可无缝接手。
> ⚠️ 当前所有改动**均未提交**（`git status` 全是 `M`/`??`），在 master 工作区。提交前请通读。

---

## 0. 一句话背景

抖音罗盘榜单监控系统做了一次 V2 升级，拆成两条并行需求：

- **V2 运营需求**（主线 / 大盘）：采集源从「商品卡榜」换成「短视频榜」；监控事件类型从 7 类简化为 4 类；输出表新增 3 列（支付金额 / 价格 / 商品图）。走 `python run.py --multi`，写**大盘飞书表**。
- **V2 服配需求**（支线 / 服饰配饰叶子类目）：在主线基础上，单独监控「服饰内衣 > 服装 > 服装配饰」下 5 个叶子类目，写**独立的服配飞书表**。走 `python run.py --acc`。

两条线共用同一套采集 / 差分 / 拓价代码，只是 scope 前缀和目标飞书表不同。

---

## 1. 三个并行工作流的来源（重要：理解代码为何长这样）

V2 的改动其实是**三股代码合并**到同一个 master 工作区的结果：

| 工作流 | 内容 | 开发方 | 状态 |
|---|---|---|---|
| A. 短视频榜 + 3 新列 | 换采集源、加 商品图/支付金额/价格 | 本会话（Claude） | 已合并 |
| B. 事件类型重构 | 7 类 → 4 类（新进榜/升50+/升100+/升150+） | mimo（worktree） | 已合并 |
| C. 服配支线 | `--acc` 叶子类目 + 独立飞书表 | 更早的会话 | 已合并 |

- B 的源 worktree 仍在：`.claude/worktrees/event-type-refactor/`（git worktree，分支 `worktree-event-type-refactor`）。**那里是旧快照，别再从它同步**，master 才是合并后的真相源。worktree 里还残留 `--scope card_order` 默认值等旧物，不要被它误导。
- 合并过程踩过的坑：CRLF/LF 行尾导致 `git merge-file` 整文件误判冲突；mimo 的 `cp` 一度把 C（服配）的改动冲掉。**当前 master 已是三方都在的正确状态**，但因此每个文件都同时带 A/B/C 三方逻辑，改动时注意别只看一处。

---

## 2. V2 运营需求（主线 / 大盘）

### 2.1 需求三件事

1. **换采集源**：商品卡榜（`product_card_hot_v2`）→ 短视频榜（`video_bring_good`）。
2. **事件类型 7→4**：`NEW_ENTRY`（新进榜）/ `RANK_UP_50`（升50+）/ `RANK_UP_100`（升100+）/ `RANK_UP_150`（升150+）。
3. **新增 3 列**：短视频用户支付金额、商品实际价格（详情页到手价，如 `¥59.9起`）、商品图。

### 2.2 已完成的开发点（含 file:line 锚点）

**采集源切换**
- `config/settings.py:45` `RANK_API_PATH`（默认 `video_bring_good`，可 .env 覆盖）。
- `config/settings.py:48` `RANK_TAB_TEXT`（默认 `短视频榜`，导航后要点的 tab；tab 是纯前端状态，不在 URL 里）。
- `collector/douyin_compass.py`：`_API_PATH = settings.RANK_API_PATH`；新增 `_extract_cards(payload)` 同时兼容两种列表 key（商品卡榜 `data.card_list` / 短视频榜 `data.data_result`）；新增 `_select_rank_tab(page)`，导航后点击「短视频榜」+「实时」。
- 短视频榜接口实测样本：`data/probe_video.txt`、`data/probe_video_card.json`（Phase 0 抓包产物）。探查脚本 `probe_video.py` / `probe_video2.py`。

**事件类型 4 类**（mimo）
- `monitor/diff.py`：`_classify_event(delta)`，阈值从高到低 150 / 100 / 50 / NEW_ENTRY（互斥）。**rank 数值越小越优，`delta = rank_previous - rank_current`，正数=上升**。
- `notify/templates.py`：`EVENT_LABELS = {NEW_ENTRY:新进榜, RANK_UP_50:升50+, RANK_UP_100:升100+, RANK_UP_150:升150+}`，是中文标签的唯一真相源，各输出模块都 import 它。
- 已删除原「高强度异动」子表。

**3 个新列（采集 → 落库 → 输出）**
- 采集：`collector/douyin_compass.py:_parse_card` 取 `image`（`image_url`/`image`）；`pay_amount` 沿用。
- Schema：`db/schema.sql` —— `products_snapshot.image`；`ranking_event.image/pay_amount/price`（均 `TEXT DEFAULT ''`）。
- 迁移：`db/database.py` `_COLUMN_MIGRATIONS` 增这几列（老库自动 ALTER 补列，幂等）。`insert_snapshot`/`insert_events` 列清单已扩展。
- 事件携带：`monitor/diff.py:140-144` `_make_event` 写入 `product_url/image/pay_amount/price`（price 默认回退脱敏价格带 `price_range`）。
- 输出三处都加了「支付金额 / 价格 / 商品图」列：`notify/excel.py`、`notify/lark.py`（`_BASE_FIELDS`+`_event_to_row`）、`notify/wecom_smartsheet.py`。

**详情页拓价（仅异动商品）**——见 §4，这里有刚修的 bug。
- `collector/product_price.py`（新文件）：只对本轮异动商品逐个开详情页抓真实到手价。
- 编排：`main.py:84 _enrich_event_prices`；`run_once` 在 `insert_events` 前调用（`main.py:168`）；`run_multi` 在采集后统一拓价并 `database.update_event_prices` 回填 DB（`main.py:398-406`）。

### 2.3 运行方式

```bash
python run.py --multi              # 正式：采集→差分→拓价→落库→推企微→写大盘飞书表
python run.py --multi --dry-run    # 不推送
python run.py --mock --dry-run     # 纯 mock 验证差分
```
- `--scope` 默认已改为 `video_order`（`main.py:853`）。`run_multi` scope_prefix=`video_order`。
- 后台启动（不随对话退出）：
  ```powershell
  Start-Process -FilePath "D:\workspace\claude\code\luopan\.venv\Scripts\python.exe" `
    -ArgumentList "run.py","--multi" -WorkingDirectory "D:\workspace\claude\code\luopan"
  ```
- ⚠️ 必须用 `.venv\Scripts\python.exe`（Python 3.13 + playwright 1.60），base Python 没装 playwright。

---

## 3. V2 服配需求（支线 / --acc）

### 3.1 需求

监控「服饰内衣 > 服装 > 服装配饰」下 5 个叶子类目：**帽子 / 丝巾、披肩、头巾 / 面罩 / 防晒口罩 / 防晒袖套、冰袖**，写入**独立的服配飞书表**，与大盘表分开。

### 3.2 已完成

- 配置：`config/settings.py:72-77` `ACC_PATH`（默认 `服饰内衣,服装,服装配饰`）、`ACC_LEAF_NAMES`（5 个叶子名）；`settings.py:106` `LARK_ACC_TABLE_ID`。`.env` 已写 `LARK_ACC_TABLE_ID=tbllW7yLiCQu606X`。
- 编排：`main.py` `run_acc`（scope_prefix=`video_acc`），同样走短视频榜 + `_enrich_event_prices` 拓价 + 3 新列，写专属表：`sync_events_to_base(..., table_id=settings.LARK_ACC_TABLE_ID)`（`main.py:824` 附近）。
- 多表透传：`notify/lark.py` 的 `table_id` 参数已贯穿 `_lark_batch_create`/`_lark_list_all_record_ids`/`_lark_delete_records`/`_clear_base_table`/`sync_events_to_base`（默认回退 `settings.LARK_TABLE_ID`）。
- 叶子级解析：`db/database.py` `get_latest_run_ids_for_prefix`；`resolve_leaf_targets`/`_process_category` 在 `main.py`。
- **服配飞书表 `tbllW7yLiCQu606X` 已对齐**（2026-06-23）：13 列，含新 3 列；「事件类型」选项已是新 taxonomy（新进榜/升50+/升100+/升150+）。
- `python run.py --acc --mock --dry-run` 跑通（5 叶子 baseline，无崩溃）。

### 3.3 服配未解决点

- **真实单叶子抓包未验证**：叶子级 API 过滤是否真的只返回该叶子类目商品，还没用真实数据跑一轮 `python run.py --acc` 确认。`--mock` 通过不代表真实接口的叶子过滤参数正确。**这是服配线唯一的待验证项。**

---

## 4. 🔴 当前最关键的两个问题点（运营线）

### 4.1 详情页拓价 bug —— 已修复（待真实回归）

**现象**：首轮 multi 跑完，5 条异动的「价格」列全是脱敏区间（如 `¥1.9-¥109.8`），不是要的到手价 `¥59.9起`。

**根因**（`collector/product_price.py`，已改）：
1. **等待太短**：H5 详情页 `domcontentloaded` 后价格要 JS 水合，原来固定 `sleep(1.2s)` 时 body 还是「打开抖音APP」加载壳 → 抓到空 → 回退价格带。**已改为轮询整页文本直到出现 `¥数字`，最长 8s（`_RENDER_BUDGET`）。**
2. **取错价**：原正则取第一处 `¥` = 吊牌原价（`¥79.9`），不是用户要的「券后到手价」。**已新增 `_COUPON_RE` 优先取「券后」价。**

**已验证**：修复后对 5 条真实 URL 实测全部拿到到手价，其中目标商品正是 `¥59.9起`。本轮 DB 这 5 条已 `update_event_prices` 回填修正。

**遗留**：还没在「完整 multi 真实跑」里端到端验证拓价（只单测了 5 个 URL）。下次真实跑一轮确认拓价命中率。

### 4.2 ✅ 大盘飞书表「一条都没有」—— **已解决（2026-06-23）**

**现象**：用户报大盘表 `tblUw8qeOtOQfrV3`（base `QQMobZzHYaBjkHsrewpcR7RvnDe`）跑完后 0 条记录。

> **结果（2026-06-23 已修复）**：大盘表已加 3 列、事件类型选项已换新 taxonomy，本轮 5 条异动已重新同步进表（含修正到手价）。当前 12 列、5 条记录。下方保留根因与操作记录备查。
> ⚠️ 注意第 3 步的坑：那 5 条事件在首次失败推送时已被标 `notified=1`，常规 `sync_events_to_base`（走 `get_pending_events`）**不会再捡起它们**。是用脚本直接从 `ranking_event` 取这 5 条、显式调 `sync_events_to_base(events, run_id)` 覆盖写入才补上的。以后遇到「表结构修好但历史事件不回填」都是同一原因。

**根因（已确诊）**：
- `notify/lark.py` 是**覆盖模式**（`_BASE_WRITE_MODE="overwrite"`，`lark.py:41`）：每轮先清空整表再写本轮。
- `_BASE_FIELDS`（`lark.py:29`）现在含「支付金额 / 价格 / 商品图」3 个新字段名。
- 但大盘表 `tblUw8qeOtOQfrV3` **实际只有 11 列，缺这 3 列** → `record-batch-create` 报「字段不存在」失败 → **表被清空了、新数据没写进去 → 0 条**。
- 另外大盘表「事件类型」单选**仍是旧 7 项**（冲进TOP10/暴升50+/急升30-50/上升20-29/上升10-19/上升5-9/新进榜），未换成新 taxonomy。
- 服配表当初对齐过所以正常；**大盘表是漏掉没对齐的那张**。

**修复方案（3 步，全是写大盘表，需用户授权）**：
1. 给 `tblUw8qeOtOQfrV3` 加 3 个文本列：支付金额、价格、商品图。
2. 把「事件类型」单选选项改为：新进榜 / 升50+ / 升100+ / 升150+（移除旧 7 项）。字段 id = `fldlV2kQDW`。
3. 重新同步本轮 5 条异动（`sync_events_to_base` 覆盖写回，含修正后的到手价）。
   - 注意：5 条事件 `notified=1` 不会自动重推；要重写飞书表需手动调 `sync_events_to_base(to_send, run_id)` 或重跑一轮。

> 用 lark-base skill（`lark-cli base +field-create` / `+field-update`）操作，环境变量带 `LARK_CLI_NO_PROXY=1`，`--as user`。`--json @file` 只接受 cwd 内相对路径。
> 本会话尝试 `+field-create` 时被 Claude Code auto 模式分类器拦下（理由：写用户未明确指定的外部共享表）——**接手后需先取得用户明确同意再执行这 3 步写操作**（lark-shared SKILL 也要求写/删前确认意图）。

---

## 5. 关键常量速查

| 项 | 值 |
|---|---|
| Base APP_TOKEN | `QQMobZzHYaBjkHsrewpcR7RvnDe` |
| 大盘飞书表 | `tblUw8qeOtOQfrV3`（运营线，已对齐：12 列 + 新事件类型选项） |
| 服配飞书表 | `tbllW7yLiCQu606X`（已对齐） |
| 大盘表「事件类型」字段 id | `fldlV2kQDW` |
| 短视频榜接口关键字 | `video_bring_good`，列表 key `data.data_result` |
| 商品卡榜接口关键字 | `product_card_hot_v2`，列表 key `data.card_list`（旧） |
| scope 前缀 | 运营=`video_order`，服配=`video_acc`，旧=`card_order` |
| 事件类型 | `NEW_ENTRY/RANK_UP_50/RANK_UP_100/RANK_UP_150` |
| 详情页到手价正则 | `_COUPON_RE`（优先券后）/ `_PRICE_RE`（兜底第一处 ¥） |
| Python | `.venv\Scripts\python.exe`（3.13, playwright 1.60） |

---

## 6. 其它未解决 / 待办

- [x] ~~**大盘表 3 步修复**（§4.2）~~ —— 已完成（2026-06-23），表已 12 列 + 新选项 + 5 条记录。
- [ ] **拓价端到端真实回归**（§4.1）—— 完整 multi 跑一轮看拓价命中率。
- [ ] **服配真实单叶子抓包验证**（§3.3）。
- [ ] **cron 命令**：确认线上定时任务已从 `--scope card_order` 改成 `--multi`（master 主流程已无 card_order 残留，仅 worktree 里还有，不影响线上；检查实际 crontab/计划任务）。
- [ ] **改动未提交**：18 个 `M` + `collector/product_price.py`/`probe_video*.py` 新增。确认无误后再 commit（仓库此前无提交习惯说明，按用户节奏）。
- [x] ~~商品图渲染~~ —— 已采「可点击超链接」方案（2026-06-23）。飞书「商品图」字段为 text/url 类型：大盘表 `tblUw8qeOtOQfrV3`(fld8798xGR) 已由 plain 改为 url，服配表 `tbllW7yLiCQu606X`(fldQG9XLvm) 本就是 url。写入值仍是图片 URL 字符串，lark-cli 自动渲染成可点击链接（`[url](url)`），无需附件上传。已实测建记录→渲染为链接→删除验证通过。未采「附件/真缩略图」方案（每轮下载+逐条上传，成本高）。
- [x] ~~企微智能表格同步~~ —— 已停用（2026-06-23）。运营决定异动统一落飞书表，企微只留 Webhook 摘要推送。`main.py` run_multi 中 `sync_to_smartsheet` 调用块已注释保留（恢复取消注释即可）。`--setup-smartsheet` 命令入口未动。
- [ ] 换榜首轮注意：短视频榜与旧商品卡榜的 scope_key 不同（`video_order` vs `card_order`），不会跨榜误差分；首轮各 scope 自动作 baseline（正常，无事件）。

---

## 7. 接手第一步建议

1. 读 `CLAUDE.md`（架构 / 数据流 / collector 注意事项）+ 本文。
2. `python -m pytest tests/test_diff.py -v` 应全绿（差分纯函数，21 个 case）。
3. 跟用户确认 §4.2 大盘表 3 步修复的授权，做完即可让大盘飞书表恢复有数据。
4. 再排 §6 其余待办。
