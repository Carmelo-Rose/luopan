# Luopan · 抖音罗盘 TOP200 榜单监控

用 Python + Playwright 定期采集抖音罗盘榜单，保存商品排名快照，找出新进榜和排名快速上升的商品。

当前 V2 默认监控**短视频榜**，支持多类目大盘和服装配饰支线；旧商品卡榜的 `card_order` scope 保留兼容。采集依赖你有权访问的罗盘账号与独立浏览器登录态。

## 能做什么

- **多类目采集**：遍历目标一级类目下的二级榜单，每个榜单采集 TOP200。
- **排名变化识别**：按同一 scope 的前后两轮快照计算新进榜、上升 50 / 100 / 150 位事件。
- **服配定向监控**：从服装 L2 榜单中筛选配置的叶子类目，单独保存与比较。
- **本地留存与报告**：SQLite 保存快照和事件，大盘流程可生成 Excel 异动与采集覆盖报告。

> **当前通知状态**：大盘 `--multi` 和服配 `--acc` 的飞书 Base 同步、企微摘要已在代码中暂停；企微智能表格同步和详情页拓价也已停用。填写相关凭据不会自动恢复这些流程。单 scope 兼容入口仍保留通知路由，初次使用建议设置 `NOTIFY_CHANNEL=none`。

## 快速开始

### 1. 安装依赖

需要 Python 3.10+。以下命令均在仓库根目录运行，建议使用独立虚拟环境。

```bash
git clone https://github.com/Carmelo-Rose/luopan.git
cd luopan
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

安装 Python 依赖及 Playwright Chromium：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 2. 配置独立浏览器目录

复制 [.env.example](.env.example) 为 `.env`：

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

然后打开 `.env`，**替换示例中的浏览器配置**，并先关闭通知：

```dotenv
BROWSER_USER_DATA_DIR=./data/browser_profile
BROWSER_CHANNEL=chromium
DB_PATH=./data/compass.db
NOTIFY_CHANNEL=none
```

`browser_profile` 是本项目专用目录，首次启动时会创建。不要使用日常 Chrome 的 `User Data` 目录，也不要让多个采集进程共享并同时打开该目录。

以上使用刚安装的 Chromium；如果选择 `BROWSER_CHANNEL=chrome`，需另行安装 Google Chrome，仍须使用独立 Profile。

### 3. 登录并采集

```bash
python run.py --login
```

浏览器打开后，手动登录抖音罗盘；完成后回到终端按 Enter，保存登录态并关闭浏览器。真实采集需要可用的图形界面和相应榜单访问权限。

运行大盘主线：

```bash
python run.py --multi
```

默认数据库为 `data/compass.db`，大盘 Excel 报告保存在 `data/reports/`。大盘每个 scope 的首轮建立基线，下一轮开始与历史排名比较。

运行服配支线前，先建立类目缓存：

```bash
python run.py --discover
python run.py --acc
```

## 常用命令

| 用途 | 命令 |
|---|---|
| 大盘多类目采集 | `python run.py --multi` |
| 服配支线采集 | `python run.py --acc` |
| 单 scope 兼容入口 | `python run.py --scope video_order` |
| 仅发现并缓存类目树 | `python run.py --discover` |
| 查看某个 scope 的历史轮次 | `python run.py --list-runs --scope video_acc_帽子` |
| 查看参数说明 | `python run.py --help` |
| 运行单元测试 | `python -m pytest -q` |

多类目数据分别存入 `video_order_一级类目_二级类目`；服配使用 `video_acc_叶子名称`。查询历史时需要完整 scope_key，仅查询 `video_order` 不会汇总所有子类目。

**测试与运行参数的边界：**

- `--dry-run` 会跳过推送，但仍会写入快照；单 scope 入口还会写入事件，不是只读预览。
- `--mock` 跳过真实浏览器采集，但合成数据仍会入库。使用前应将 `DB_PATH` 指向独立测试数据库，避免混入真实排名历史。
- 想先了解差分逻辑，无需登录即可运行 `python -m pytest tests/test_diff.py -v`；该测试使用临时数据库。
- `--no-push` / `--flush` 用于分离采集与后续处理，不会重新启用当前暂停的通知集成。

## 配置速查

配置由 [config/settings.py](config/settings.py) 从环境变量和 `.env` 统一读取。

| 变量 | 作用 |
|---|---|
| `BROWSER_USER_DATA_DIR` | 本项目专用浏览器 Profile 目录 |
| `BROWSER_CHANNEL` | 浏览器类型；本 README 使用 `chromium` |
| `DB_PATH` | SQLite 路径，默认 `data/compass.db` |
| `RANK_API_PATH` / `RANK_TAB_TEXT` | 当前默认 `video_bring_good` / `短视频榜` |
| `TARGET_L1_CATEGORIES` | 大盘目标一级类目，逗号分隔；`*` 表示账号可见全部 |
| `EXCLUDE_L2_CATEGORIES` | 排除的二级类目名称，逗号分隔，精确匹配 |
| `ACC_PATH` | 服配路径，默认 `服饰内衣,服装,服装配饰` |
| `ACC_LEAF_NAMES` | 服配目标叶子名称，逗号分隔 |
| `NOTIFY_CHANNEL` | 单 scope 通知路由：`wecom` / `lark` / `none` |

企微 Webhook、飞书表和智能表格的配置项仍保留在 [.env.example](.env.example)，其配置示例不代表大盘与服配同步当前已启用。只设置 `--scope card_order` 也不会切换浏览器中的榜单，旧商品卡榜还需匹配相应入口、接口关键字与 tab 配置。

## 差分规则

排名数字越小越好，`rank_delta = 上轮排名 - 本轮排名`。同一商品每轮只产生一个事件。

| 事件 | 条件 |
|---|---|
| `NEW_ENTRY` | 本轮出现，上轮不在同一 scope |
| `RANK_UP_50` | 排名上升 50–99 位 |
| `RANK_UP_100` | 排名上升 100–149 位 |
| `RANK_UP_150` | 排名上升至少 150 位 |

不同 scope 独立建立基线、比较快照；数据库通过唯一约束去重。真实服配支线会在首轮把已采到的目标商品记为 `NEW_ENTRY`，与大盘首轮仅建基线的行为不同。

### 服配覆盖范围

`--acc` 从 `data/category_raw_dump.json` 解析目标叶子，采集「服饰内衣 > 服装」L2 短视频榜 TOP200，再按返回的 `leaf_category_id` 拆分。

因此，它覆盖的是**出现在该 L2 TOP200 中的目标商品**，并非每个叶子类目各自的 TOP200。部分叶子没有结果可能只是未进入上级榜单；若全部为空，应结合日志检查登录态、限流和类目配置。服配支线不生成 Excel。

## 项目结构

| 目录 / 文件 | 职责 |
|---|---|
| [run.py](run.py) / [main.py](main.py) | CLI 入口、采集与差分流程 |
| [collector/](collector/) | Playwright 采集、类目发现 |
| [monitor/diff.py](monitor/diff.py) | 排名差分与事件分类 |
| [db/](db/) | SQLite schema、快照与事件读写 |
| [notify/](notify/) | Excel、企微与飞书相关实现 |
| [config/settings.py](config/settings.py) | 配置读取 |
| [tests/](tests/) | 单元与回归测试 |

## 定时运行与常见问题

**定时任务能直接用吗？**  
仓库中的 `.bat` / `.ps1` / `.vbs` 脚本包含原部署机器的路径和外部运维依赖，使用前需要逐项调整。先手动确认采集流程正常，再配置自己的定时任务；不要并发运行共用 Profile 的采集实例。

**提示缺少 Playwright 或浏览器？**  
确认当前 `python` 来自安装依赖的虚拟环境，再执行 `python -m pip install -r requirements.txt` 和 `python -m playwright install chromium`。同时检查 `BROWSER_CHANNEL` 与安装的浏览器是否一致。

**采集 0 条、跳转登录页或出现限流？**  
先检查日志。登录态失效时重新运行 `python run.py --login`；若是请求过频或验证页，暂停采集并按平台提示处理，避免反复启动。

**DOM 降级只拿到 10 条？**  
这是首页兜底数据，不代表完整 TOP200 采集成功，应排查榜单 API 响应与登录状态。

**价格、支付金额为什么是区间？**  
来源平台会对部分字段脱敏。当前详情页拓价已停用，报告中的价格带不应当作精确成交价。

**没有收到企微消息或飞书表没有更新？**  
先看本文开头的通知状态。大盘与服配的相关外发已暂停，不是单靠补填凭据就能恢复。

反馈问题时，请附上运行命令、Python 版本和脱敏后的错误日志。不要提交 `.env`、Cookie、浏览器 Profile、数据库或带凭据的请求内容。
