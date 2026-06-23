"""
推送消息模板。

分组顺序（从高到低）：
  1. 🚀 升150+      = RANK_UP_150
  2. 📈 升100+      = RANK_UP_100
  3. 📈 升50+       = RANK_UP_50
  4. 🆕 新进 TOP200 = NEW_ENTRY

GROUPS 须覆盖 monitor/diff.py 全部事件类型，否则 group_events() 打 WARNING。
"""
import logging
import re
from datetime import datetime

from monitor.diff import (
    NEW_ENTRY,
    RANK_UP_50,
    RANK_UP_100,
    RANK_UP_150,
)

logger = logging.getLogger(__name__)

_TITLE_MAXLEN = 60

_MARKDOWN_SPECIAL = re.compile(r'([\\*_`\[\]#~>|])')


def _escape_markdown(text: str) -> str:
    """转义企微 markdown 中的特殊字符。"""
    return _MARKDOWN_SPECIAL.sub(r'\\\1', text)


# 事件类型 → 中文标签（唯一真相源，lark / excel / wecom_smartsheet 共用）
EVENT_LABELS: dict[str, str] = {
    "NEW_ENTRY": "新进榜",
    "RANK_UP_50": "升50+",
    "RANK_UP_100": "升100+",
    "RANK_UP_150": "升150+",
}

GROUPS: list[tuple[str, set]] = [
    ("🚀 升150+",      {RANK_UP_150}),
    ("📈 升100+",      {RANK_UP_100}),
    ("📈 升50+",       {RANK_UP_50}),
    ("🆕 新进 TOP200", {NEW_ENTRY}),
]

_KNOWN_TYPES: set = {t for _, types in GROUPS for t in types}


def _trim(title: str, n: int = _TITLE_MAXLEN) -> str:
    title = (title or "").strip()
    return title if len(title) <= n else title[: n - 1] + "…"


def group_events(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """按 GROUPS 分桶，返回 [(分组标题, 组内事件列表), ...]，自动跳过空组。"""
    unknown = {e.get("event_type") for e in events} - _KNOWN_TYPES
    if unknown:
        logger.warning(
            "以下事件类型未被任何推送分组覆盖，将不会出现在推送中（请补充 GROUPS）：%s",
            sorted(t for t in unknown if t),
        )

    grouped: list[tuple[str, list[dict]]] = []
    for title, types in GROUPS:
        members = [e for e in events if e.get("event_type") in types]
        if not members:
            continue
        if NEW_ENTRY in types:
            members.sort(key=lambda e: (e.get("rank_current") or 1_000_000))
        else:
            members.sort(key=lambda e: (e.get("rank_delta") or 0), reverse=True)
        grouped.append((title, members))
    return grouped


def format_line(event: dict, link: bool = True) -> str:
    """
    单行格式：
      商品标题  #116（↑51，上轮#167）(五金)
      新进榜：商品标题  #116（新进榜）(五金)
    """
    etype = event.get("event_type", "")
    title = _escape_markdown(_trim(event.get("product_title", "")))
    rank_cur = event.get("rank_current")
    rank_prev = event.get("rank_previous")
    delta = event.get("rank_delta")
    url = event.get("product_url", "")
    category_name = event.get("category_name", "")
    cat_suffix = f"({category_name})" if category_name else ""
    link_md = f"  [查看]({url})" if (link and url) else ""

    if etype == NEW_ENTRY:
        return f"{title}  #{rank_cur}（新进榜）{cat_suffix}{link_md}"

    rank_info = f"#{rank_cur}（↑{delta}，上轮#{rank_prev}）"
    return f"{title}  {rank_info}{cat_suffix}{link_md}"


def build_header(scope_key: str, total: int) -> str:
    """消息抬头：图标标题 + 时间 + 变动总数。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"**📊 罗盘榜单异动**\n"
        f"🕐 {ts} 共 {total} 条变动"
    )
