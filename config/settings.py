"""
集中管理所有配置，从 .env 读取并提供类型化属性。
"""
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)


def _safe_int(name: str, default: int) -> int:
    """安全解析整数环境变量，无效值回退到默认值并记录警告。"""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning("环境变量 %s=%r 无法解析为整数，使用默认值 %d", name, raw, default)
        return default


# ── 路径 ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "data" / "compass.db"))

# ── 浏览器 ────────────────────────────────────────────────────────────
BROWSER_USER_DATA_DIR: str = os.getenv("BROWSER_USER_DATA_DIR", "")
BROWSER_CHANNEL: str = os.getenv("BROWSER_CHANNEL", "chrome")

# ── 采集参数 ──────────────────────────────────────────────────────────
COMPASS_URL: str = os.getenv(
    "COMPASS_URL",
    "https://compass.jinritemai.com/screen/rank/product/card",
)
RANK_ENTRY_URL: str = os.getenv(
    "RANK_ENTRY_URL",
    "https://compass.jinritemai.com/shop/chance/merchandise-product-rank?rank_type=3",
)
# 榜单数据接口的路径关键字（采集器据此拦截 XHR 响应）。
# 商品卡榜 = product_card_hot_v2；短视频榜 = video_bring_good。换榜时改这里 / .env。
RANK_API_PATH: str = os.getenv("RANK_API_PATH", "video_bring_good")
# 榜单 tab 名称：导航后需点击的 tab 文本（tab 是纯前端状态，不在 URL 里）。
# 短视频榜 = "短视频榜"；留空则不点击（用页面默认 tab，如商品卡榜）。
RANK_TAB_TEXT: str = os.getenv("RANK_TAB_TEXT", "短视频榜")
PAGE_SIZE: int = _safe_int("PAGE_SIZE", 10)
TOTAL_PAGES: int = _safe_int("TOTAL_PAGES", 20)
# 固定行业类目（单类目模式，向后兼容）；留空可恢复为跟随罗盘页面当前选择。
INDUSTRY_ID: str = os.getenv("INDUSTRY_ID", "")
CATEGORY_ID: str = os.getenv("CATEGORY_ID", "")
CATEGORY_NAME: str = os.getenv("CATEGORY_NAME", "")

# 多类目模式：目标一级类目名称列表（逗号分隔）。
# 系统自动发现这些一级类目下的所有二级类目，逐个采集。
# 留空则回退到单类目模式（使用上面的 INDUSTRY_ID / CATEGORY_ID）。
_TARGET_L1_RAW = os.getenv(
    "TARGET_L1_CATEGORIES",
    "智能家居,玩具乐器,钟表配饰,图书教育,服饰内衣,个护家清,运动户外",
).strip()
TARGET_ALL_L1_CATEGORIES: bool = _TARGET_L1_RAW == "*"
TARGET_L1_CATEGORIES: list[str] = [
    s.strip()
    for s in _TARGET_L1_RAW.split(",")
    if s.strip()
] if not TARGET_ALL_L1_CATEGORIES else []

# 服配叶子类目支线：从 L1 到目标父节点的名称路径（逗号分隔）及叶子类目名。
# 运行时从 category_raw_dump.json 解析 id，不硬编码。
_ACC_PATH_RAW = os.getenv("ACC_PATH", "服饰内衣,服装,服装配饰")
ACC_PATH: list[str] = [s.strip() for s in _ACC_PATH_RAW.split(",") if s.strip()]
_ACC_LEAF_NAMES_RAW = os.getenv(
    "ACC_LEAF_NAMES", "帽子,丝巾/披肩/头巾,面罩,防晒口罩,防晒袖套/冰袖",
)
ACC_LEAF_NAMES: list[str] = [s.strip() for s in _ACC_LEAF_NAMES_RAW.split(",") if s.strip()]

# 类目树缓存文件路径
CATEGORY_TREE_CACHE: str = os.getenv(
    "CATEGORY_TREE_CACHE", str(BASE_DIR / "data" / "category_tree.json")
)

# 类目 id→层级名 拍平索引缓存（用于把商品 leaf_category_id 翻译成三级/叶子类目名）
CATEGORY_LOOKUP_CACHE: str = os.getenv(
    "CATEGORY_LOOKUP_CACHE", str(BASE_DIR / "data" / "category_lookup.json")
)

# 采集完整性下限：低于此条数视为本轮采集失败（如 Cookie 中途失效），
# 拒绝写入残缺快照，避免污染差分。默认 = 满额的 80%。
MIN_PRODUCTS: int = _safe_int("MIN_PRODUCTS", int(PAGE_SIZE * TOTAL_PAGES * 0.8))

# ── 推送 ──────────────────────────────────────────────────────────────
NOTIFY_CHANNEL: str = os.getenv("NOTIFY_CHANNEL", "wecom") or "none"
WECOM_WEBHOOK_URL: str = os.getenv("WECOM_WEBHOOK_URL", "")
LARK_WEBHOOK_URL: str = os.getenv("LARK_WEBHOOK_URL", "")

# ── 飞书多维表格同步（独立于上面的通知渠道）──────────────────────────
# 配齐 APP_TOKEN + TABLE_ID 即启用；每轮把异动事件写入该 Base 表。
# 写入经本机 lark-cli 子进程，身份默认 user（token 自动续期，适合每小时 cron）。
LARK_BASE_APP_TOKEN: str = os.getenv("LARK_BASE_APP_TOKEN", "")
LARK_TABLE_ID: str = os.getenv("LARK_TABLE_ID", "")
LARK_AS: str = os.getenv("LARK_AS", "user")  # user | bot

# 服配叶子类目支线：独立飞书表（复用 LARK_BASE_APP_TOKEN / LARK_AS）
LARK_ACC_TABLE_ID: str = os.getenv("LARK_ACC_TABLE_ID", "")

# ── 企微智能表格同步（独立于 Webhook 通知）─────────────────────────────
# 需要企业自建应用凭证（非群机器人 Webhook），配齐 4 项即自动启用。
# corpid 在企业微信管理后台「我的企业 → 企业信息」底部获取（ww 开头）。
# corpsecret 在「应用管理 → 自建应用 → 查看 Secret」获取。
# docid / sheet_id 从智能表格 URL 或「文档 → 更多 → 权限」中获取。
WECOM_CORPID: str = os.getenv("WECOM_CORPID", "")
WECOM_CORPSECRET: str = os.getenv("WECOM_CORPSECRET", "")
WECOM_SMARTSHEET_DOCID: str = os.getenv("WECOM_SMARTSHEET_DOCID", "")
WECOM_SMARTSHEET_SHEET_ID: str = os.getenv("WECOM_SMARTSHEET_SHEET_ID", "")
WECOM_SMARTSHEET_URL: str = os.getenv("WECOM_SMARTSHEET_URL", "")

# 注：差分阈值的唯一真相源在 monitor/diff.py:_classify_event，
# 不在此处配置（曾有一份从未被引用且与硬编码不一致的死配置，已移除）。


def validate() -> list[str]:
    """校验必填配置项，返回错误信息列表（空 = 全部通过）。"""
    errors: list[str] = []
    if not BROWSER_USER_DATA_DIR:
        errors.append("BROWSER_USER_DATA_DIR 未设置，Playwright 无法启动持久化浏览器上下文")
    return errors
