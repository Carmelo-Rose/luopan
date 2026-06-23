"""
企微智能表格同步：把每轮异动事件写入企微智能表格。

通过 wecom-cli 子进程调用，认证由 WorkBuddy 企微连接器接管，
无需在企微后台手工配置 corpid / corpsecret / API 权限 / IP 白名单。

写入模式：
  - overwrite（默认）：删除旧子表 → 创建新子表 → 写入最新一轮。
    全程只用 add_sheet / delete_sheet / add_fields / add_records，不需要读权限。
  - append：不清空，直接追加，保留全部历史。

用法 —— 在 .env 中配置 docid + sheet_id 即自动启用：
  WECOM_SMARTSHEET_DOCID=dcMfIrptO...
  WECOM_SMARTSHEET_SHEET_ID=E0DFFN

首次使用可通过 wecom-cli 一键创建：
  python run.py --setup-smartsheet
"""
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import settings
from notify.templates import EVENT_LABELS

logger = logging.getLogger(__name__)

# ── .env 回写工具 ──────────────────────────────────────────────────────

_ENV_PATH = Path(settings.BASE_DIR) / ".env"


def _write_env(key: str, value: str) -> None:
    """将 key=value 写入 .env 文件（更新已存在的行，或追加到末尾）。"""
    if not _ENV_PATH.exists():
        logger.warning(".env 文件不存在: %s", _ENV_PATH)
        return
    text = _ENV_PATH.read_text(encoding="utf-8")
    if re.search(rf"^{key}=.*$", text, re.MULTILINE):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    _ENV_PATH.write_text(text, encoding="utf-8")
    logger.info("已写入 .env: %s=%s", key, value)


# ── 事件类型 → 中文标签 + 优先级（用于排序） ──────────────────────────

_EVENT_LABELS = EVENT_LABELS

# 排序优先级：数值越小越靠前
_EVENT_PRIORITY = {
    "RANK_UP_150": 0,
    "RANK_UP_100": 1,
    "RANK_UP_50": 2,
    "NEW_ENTRY": 3,
}

_CST = timezone(timedelta(hours=8))
_BATCH_SIZE = 30  # 单批写入上限（Windows cmd.exe 命令行长有限，30条约~6KB）

# 写入模式：
#   overwrite = 删除旧子表 → 新建子表 → 写入，每轮只保留最新数据
#   append    = 直接追加，保留全部历史
_WRITE_MODE = "overwrite"


# ── wecom-cli 子进程封装 ────────────────────────────────────────────────

_WECOM_CLI_EXE: str | None = None


def _find_wecom_cli() -> str:
    """查找 wecom-cli 可执行文件，缓存结果。"""
    global _WECOM_CLI_EXE
    if _WECOM_CLI_EXE:
        return _WECOM_CLI_EXE
    if os.name == "nt":
        home = os.path.expandvars("%USERPROFILE%")
        win_path = Path(home) / ".workbuddy" / "binaries" / "node" / "cli-connector-packages" / "wecom-cli.cmd"
        if win_path.exists():
            _WECOM_CLI_EXE = str(win_path)
            return _WECOM_CLI_EXE
    raise RuntimeError("wecom-cli 未找到！请确认企微连接器已连接。")


def _wecom_cli(args: list[str]) -> dict:
    """调用 wecom-cli 子进程，解析 MCP JSON-RPC 响应，返回 data dict。遇错抛 RuntimeError。"""
    exe = _find_wecom_cli()
    if os.name == "nt" and exe.lower().endswith(".cmd"):
        # 直接调 node 绕过 cmd.exe 对特殊字符的解析问题
        node_dir = str(Path(exe).parent)
        wecom_js = Path(node_dir) / "node_modules" / "@wecom" / "cli" / "bin" / "wecom.js"
        node_exe = Path(node_dir) / "node.exe"
        cmd = [str(node_exe) if node_exe.exists() else "node", str(wecom_js)] + args
    else:
        cmd = [exe] + args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise RuntimeError("wecom-cli 未找到！请确认企微连接器已连接。")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"wecom-cli 超时: {' '.join(cmd)}")

    if result.returncode != 0:
        err = (result.stderr or "")[:500]
        raise RuntimeError(f"wecom-cli 返回非零退出码 {result.returncode}: {err}")

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError("wecom-cli 返回空响应")

    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"wecom-cli 返回非 JSON: {stdout[:500]}")

    content_list = outer.get("result", {}).get("content", [])
    if not content_list:
        raise RuntimeError(f"wecom-cli 响应无 content: {stdout[:500]}")

    inner_text = content_list[0].get("text", "")
    if not inner_text:
        raise RuntimeError(f"wecom-cli content 无 text: {stdout[:500]}")

    try:
        data = json.loads(inner_text)
    except json.JSONDecodeError:
        raise RuntimeError(f"wecom-cli inner text 非 JSON: {inner_text[:500]}")

    if data.get("errcode", 0) != 0:
        raise RuntimeError(
            f"wecom-cli API 错误: errcode={data.get('errcode')} errmsg={data.get('errmsg', '')}"
        )
    return data


# ── 数据转换 ───────────────────────────────────────────────────────────

def _round_label(run_id: str) -> str:
    """run_id(ISO, UTC) → 北京时间 'YYYY-MM-DD HH:MM'。"""
    try:
        dt = datetime.fromisoformat(run_id)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(run_id)[:16]


def _txt(val: str) -> list:
    """文本字段值 → 企微智能表格要求的数组格式。

    FIELD_TYPE_TEXT 必须传 [{"type": "text", "text": "内容"}]，
    直接传字符串会导致 API 返回成功但数据为空。
    """
    s = val if val is not None else ""
    return [{"type": "text", "text": str(s)}]


def _num(val) -> int:
    """数字字段值 → int（None→0）。"""
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _delta_label(val) -> list:
    """升幅数值 → 单选标签+颜色（按范围分级）。"""
    d = _num(val)
    for threshold, style, label in _DELTA_STYLE:
        if d >= threshold:
            return [{"text": label, "style": style}]
    return [{"text": "<5", "style": 7}]


def _event_to_values(ev: dict, round_label: str) -> dict:
    """单条事件 → 智能表格一行的 values dict。

    字段顺序（对标飞书多维表格）：
      商品标题 | 一级类目 | 二级类目 | 三级类目 | 叶子类目 | 当前排名 | 上轮排名 | 升幅 | 事件类型 | 采集轮次
    """
    return {
        "商品标题": _txt(ev.get("product_title", "") or ""),
        "一级类目": [{"text": ev.get("industry_name", "") or "", "style": _CATEGORY_STYLE.get(ev.get("industry_name", ""), 7)}],
        "二级类目": [{"text": ev.get("category_name", "") or "", "style": _CATEGORY_L2_STYLE.get(ev.get("category_name", ""), 7)}],
        "三级类目": _txt(ev.get("category_l3_name", "") or ""),
        "叶子类目": _txt(ev.get("leaf_category_name", "") or ""),
        "当前排名": _num(ev.get("rank_current")),
        "上轮排名": _num(ev.get("rank_previous")),
        "升幅": _delta_label(ev.get("rank_delta")),
        "事件类型": [{
            "text": _EVENT_LABELS.get(ev.get("event_type", ""), ev.get("event_type", "")),
            "style": _EVENT_STYLE.get(ev.get("event_type", ""), 7),
        }],
        "支付金额": _txt(ev.get("pay_amount", "") or ""),
        "价格": _txt(ev.get("price", "") or ""),
        "商品图": _txt(ev.get("image", "") or ""),
        "采集轮次": _txt(round_label),
    }


def _event_sort_key(ev: dict) -> tuple:
    """排序键：(事件优先级, 当前排名, 商品标题)。

    升150+ 排最前，然后按升幅档位递减，最后按排名。
    """
    etype = ev.get("event_type", "NEW_ENTRY")
    prio = _EVENT_PRIORITY.get(etype, 99)
    rank = ev.get("rank_current") or 9999
    title = (ev.get("product_title") or "").lower()
    return (prio, rank, title)


# ── 智能表格字段定义（对标飞书多维表格布局，10列全保留） ──────────────

_SHEET_FIELD_DEFS = [
    {"field_title": "商品标题",  "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "一级类目",  "field_type": "FIELD_TYPE_SINGLE_SELECT"},
    {"field_title": "二级类目",  "field_type": "FIELD_TYPE_SINGLE_SELECT"},
    {"field_title": "三级类目",  "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "叶子类目",  "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "当前排名",  "field_type": "FIELD_TYPE_NUMBER"},
    {"field_title": "上轮排名",  "field_type": "FIELD_TYPE_NUMBER"},
    {"field_title": "升幅",       "field_type": "FIELD_TYPE_SINGLE_SELECT"},
    {"field_title": "事件类型",  "field_type": "FIELD_TYPE_SINGLE_SELECT"},
    {"field_title": "支付金额",  "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "价格",       "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "商品图",    "field_type": "FIELD_TYPE_TEXT"},
    {"field_title": "采集轮次",  "field_type": "FIELD_TYPE_TEXT"},
]

# 事件类型 → 单选标签颜色 style（企微智能表格 style 1-27）
# 颜色越深 = 优先级越高，红>橙>黄>蓝>绿>灰>紫
_EVENT_STYLE = {
    "RANK_UP_150":    18,  # 红 — 升150+
    "RANK_UP_100":    20,  # 橙 — 升100+
    "RANK_UP_50":     23,  # 黄 — 升50+
    "NEW_ENTRY":       5,  # 浅紫 — 新进榜
}

# 一级类目 → 单选标签颜色（对标飞书配色）
_CATEGORY_STYLE = {
    "智能家居":    4,   # 浅绿1
    "运动户外":    10,  # 浅蓝1
    "个护家清":    5,   # 浅紫1
    "服饰内衣":    2,   # 浅橙1
    "图书音像":    7,   # 浅灰1
    "3C数码":     18,  # 红
    "食品饮料":   23,  # 黄
    "家装建材":   14,  # 天蓝
    "汽车用品":   20,  # 橙
}

# 二级类目 → 单选标签颜色
_CATEGORY_L2_STYLE = {
    "服装":         17,  # 浅红2
    "女装":         1,   # 浅红1
    "男装":         12,  # 蓝
    "美妆护肤":     27,  # 粉红
    "家居":         4,   # 浅绿1
    "家装建材":     16,  # 绿
    "家电厨房电器": 14,  # 天蓝
    "个人护理":     24,  # 浅紫2
    "运动户外用品": 10,  # 浅蓝1
    "文化办公用品": 9,   # 灰
    "服装鞋帽供货": 2,   # 浅橙1
    "电子/电工":    3,   # 浅天蓝1
    "箱包":         20,  # 橙
    "鞋/靴":       19,  # 浅橙2
    "摩托车/电动车/燃油车": 25,  # 紫
}

# 升幅 → 单选标签颜色（按范围分级）
_DELTA_STYLE = [
    (100, 18, "100+"),   # 红
    (50,  20, "50+"),    # 橙
    (30,  23, "30-49"),  # 黄
    (20,  10, "20-29"),  # 浅蓝
    (10,  15, "10-19"),  # 浅绿
    (5,   7,  "5-9"),    # 浅灰
    (0,   7,  "<5"),     # 浅灰
]


def setup_smartsheet(doc_name: str = "抖音罗盘榜单异动") -> bool:
    """
    通过 wecom-cli 一键创建企微智能表格文档 + 子表 + 字段定义，
    并回写 docid/sheet_id/url 到 .env。

    仅在 .env 中 WECOM_SMARTSHEET_DOCID 为空时执行（幂等）。
    """
    if settings.WECOM_SMARTSHEET_DOCID and settings.WECOM_SMARTSHEET_SHEET_ID:
        logger.info("智能表格已配置: docid=%s sheet_id=%s，跳过创建",
                     settings.WECOM_SMARTSHEET_DOCID, settings.WECOM_SMARTSHEET_SHEET_ID)
        return True

    logger.info("=== 开始创建企微智能表格 === ")

    try:
        # 1. 创建智能表格文档
        data = _wecom_cli([
            "doc", "create_doc",
            json.dumps({"doc_type": 10, "doc_name": doc_name}),
        ])
        docid = data.get("docid", "")
        doc_url = data.get("url", "")
        if not docid:
            logger.error("创建文档未返回 docid: %s", data)
            return False
        logger.info("文档已创建: docid=%s", docid)

        # 2. 获取自动创建的子表 sheet_id
        data = _wecom_cli([
            "doc", "smartsheet_get_sheet",
            json.dumps({"docid": docid}),
        ])
        sheets = data.get("sheet_list", [])
        if not sheets:
            logger.error("未找到子表")
            return False
        sheet_id = sheets[0].get("sheet_id", "")

        # 3. 重命名子表
        _wecom_cli([
            "doc", "smartsheet_update_sheet",
            json.dumps({"docid": docid, "properties": {"sheet_id": sheet_id, "title": "异动事件"}}),
        ])

        # 4. 获取默认字段并重命名为「商品标题」
        data = _wecom_cli([
            "doc", "smartsheet_get_fields",
            json.dumps({"docid": docid, "sheet_id": sheet_id}),
        ])
        fields = data.get("fields", [])
        if fields:
            first = fields[0]
            _wecom_cli([
                "doc", "smartsheet_update_fields",
                json.dumps({
                    "docid": docid, "sheet_id": sheet_id,
                    "fields": [{"field_id": first["field_id"], "field_title": "商品标题", "field_type": first["field_type"]}],
                }),
            ])

        # 5. 添加剩余 9 个字段
        _wecom_cli([
            "doc", "smartsheet_add_fields",
            json.dumps({"docid": docid, "sheet_id": sheet_id, "fields": _SHEET_FIELD_DEFS[1:]}),
        ])

        # 6. 回写 .env + 运行时生效
        _write_env("WECOM_SMARTSHEET_DOCID", docid)
        _write_env("WECOM_SMARTSHEET_SHEET_ID", sheet_id)
        _write_env("WECOM_SMARTSHEET_URL", doc_url)
        os.environ["WECOM_SMARTSHEET_DOCID"] = docid
        os.environ["WECOM_SMARTSHEET_SHEET_ID"] = sheet_id
        settings.WECOM_SMARTSHEET_DOCID = docid
        settings.WECOM_SMARTSHEET_SHEET_ID = sheet_id

        logger.info("=== 智能表格创建完成 ===")
        logger.info("链接: %s", doc_url)
        return True

    except RuntimeError as e:
        logger.error("创建失败: %s", e)
        return False


# ── 覆盖模式：删旧子表 + 建新子表 ──────────────────────────────────────

def _recreate_sheet() -> str:
    """
    删除旧子表 → 创建新子表 → 定义全部字段 → 回写新 sheet_id。

    优化：新建子表会自带一个默认「文本」字段，
    将其重命名为第一个业务字段，再 add_fields 添加剩余字段，避免空列。
    返回新 sheet_id。全程不需要读权限。
    """
    docid = settings.WECOM_SMARTSHEET_DOCID
    old_sheet_id = settings.WECOM_SMARTSHEET_SHEET_ID

    # 1. 删除旧子表
    try:
        _wecom_cli([
            "doc", "smartsheet_delete_sheet",
            json.dumps({"docid": docid, "sheet_id": old_sheet_id}),
        ])
        logger.info("旧子表已删除: %s", old_sheet_id)
    except RuntimeError as e:
        logger.warning("删除旧子表失败（忽略继续）: %s", e)

    # 2. 创建新子表
    data = _wecom_cli([
        "doc", "smartsheet_add_sheet",
        json.dumps({"docid": docid, "properties": {"title": "异动事件", "index": 0}}),
    ])
    new_sheet_id = data.get("properties", {}).get("sheet_id", "")
    if not new_sheet_id:
        raise RuntimeError(f"add_sheet 未返回 sheet_id: {data}")
    logger.info("新子表已创建: %s", new_sheet_id)

    # 3. 将默认的「文本」字段重命名为我们的第一个业务字段
    first_field = _SHEET_FIELD_DEFS[0]
    data = _wecom_cli([
        "doc", "smartsheet_get_fields",
        json.dumps({"docid": docid, "sheet_id": new_sheet_id}),
    ])
    default_fields = data.get("fields", [])
    if default_fields:
        fid = default_fields[0]["field_id"]
        ftype = default_fields[0]["field_type"]
        _wecom_cli(["doc", "smartsheet_update_fields", json.dumps({
            "docid": docid, "sheet_id": new_sheet_id,
            "fields": [{"field_id": fid, "field_title": first_field["field_title"], "field_type": ftype}],
        })])
        logger.info("默认字段已重命名为: %s", first_field["field_title"])

    # 4. 添加剩余字段（跳过第一个，已用默认字段替代）
    remaining = _SHEET_FIELD_DEFS[1:]
    if remaining:
        _wecom_cli([
            "doc", "smartsheet_add_fields",
            json.dumps({"docid": docid, "sheet_id": new_sheet_id, "fields": remaining}),
        ])
        logger.info("已添加 %d 个额外字段", len(remaining))

    # 5. 回写新 sheet_id
    _write_env("WECOM_SMARTSHEET_SHEET_ID", new_sheet_id)
    os.environ["WECOM_SMARTSHEET_SHEET_ID"] = new_sheet_id
    settings.WECOM_SMARTSHEET_SHEET_ID = new_sheet_id

    return new_sheet_id


# ── 批量写入 ───────────────────────────────────────────────────────────

def _batch_create(rows: list[dict], docid: str, sheet_id: str) -> bool:
    """批量写入。成功返回 True。

    注意：smartsheet_add_records 只支持字段标题（field_title）作为 key，
    不支持 key_type 参数（那是 update_records 才有的）。
    """
    body = {
        "docid": docid,
        "sheet_id": sheet_id,
        "records": [{"values": row} for row in rows],
    }
    try:
        _wecom_cli(["doc", "smartsheet_add_records", json.dumps(body)])
        return True
    except RuntimeError as e:
        logger.error("写入批次失败: %s", e)
        return False


# ── 公开入口 ───────────────────────────────────────────────────────────

def sync_to_smartsheet(events: list[dict], run_id: str) -> int:
    """
    把一轮全部异动事件写入企微智能表格。

    写入模式由 _WRITE_MODE 决定：
      - overwrite（默认）：删除旧子表 → 新建子表 → 写入最新一轮。
        全程只用 add_sheet / delete_sheet / add_fields / add_records，零读权限。
      - append：直接追加，不清空。

    返回成功写入的事件条数。
    """
    if not events:
        return 0

    if not (settings.WECOM_SMARTSHEET_DOCID and settings.WECOM_SMARTSHEET_SHEET_ID):
        logger.info("未配置企微智能表格 docid/sheet_id，跳过同步")
        return 0

    # 按事件重要性排序：升150+ > 升100+ > 升50+ > 新进榜
    sorted_events = sorted(events, key=_event_sort_key)

    rows = [
        _event_to_values(e, _round_label(e.get("run_id") or run_id))
        for e in sorted_events
    ]

    # ── 覆盖模式：删旧建新 ────────────────────────────────────────────
    if _WRITE_MODE == "overwrite":
        try:
            sheet_id = _recreate_sheet()
        except RuntimeError as e:
            logger.error("重建子表失败，本轮跳过写入: %s", e)
            return 0
    else:
        sheet_id = settings.WECOM_SMARTSHEET_SHEET_ID

    docid = settings.WECOM_SMARTSHEET_DOCID

    written = 0
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i:i + _BATCH_SIZE]
        if not _batch_create(batch, docid, sheet_id):
            logger.warning("第 %d 批写入失败，已写入 %d 条后停止", i // _BATCH_SIZE + 1, written)
            break
        written += len(batch)
        logger.info("进度: %d/%d 条", written, len(rows))

    logger.info("企微智能表格同步完成: %d/%d 条（轮次 %s）", written, len(rows), _round_label(run_id))
    return written




