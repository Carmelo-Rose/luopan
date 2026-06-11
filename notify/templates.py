"""
推送消息模板。

分组顺序：
  1. 🚨 重磅异动  = ENTER_TOP10 + RANK_UP_50_PLUS_WARNING
  2. ⚠️ 急升预警  = RANK_UP_30_50_WARNING
  3. 📈 稳步上升  = RANK_UP_10 + RANK_UP_20 + RANK_UP_5
  4. 🆕 新进 TOP200 = NEW_ENTRY

GROUPS 须覆盖 monitor/diff.py 全部事件类型，否则 group_events() 打 WARNING。
"""
import logging
from datetime import datetime

from monitor.diff import (
    NEW_ENTRY,
    ENTER_TOP10,
    RANK_UP_5,
    RANK_UP_10,
    RANK_UP_20,
    RANK_UP_30_50_WARNING,
    RANK_UP_50_PLUS_WARNING,
)

logger = logging.getLogger(__name__)

_TITLE_MAXLEN = 60

GROUPS: list[tuple[str, set]] = [
    ("🚨 重磅异动",   {ENTER_TOP10, RANK_UP_50_PLUS_WARNING}),
    ("⚠️ 急升预警",   {RANK_UP_30_50_WARNING}),
    ("📈 稳步上升",   {RANK_UP_10, RANK_UP_20, RANK_UP_5}),
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
    title = _trim(event.get("product_title", ""))
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


# ── 方案 D（已注释）───────────────────────────────────────────────────
# 企微三色 markdown + 箭头强度条 + 换行分层。
#
# 颜色规则（企微 markdown 仅支持三种）：
#   warning（橙红）→ 重磅异动：ENTER_TOP10 / RANK_UP_50_PLUS_WARNING / RANK_UP_30_50_WARNING
#   info   （绿）  → 稳步上升：RANK_UP_10 / RANK_UP_20 / RANK_UP_5
#   comment（灰）  → 上轮排名 / 新进榜排名
#
# _TITLE_MAXLEN_D = 24
#
# def _color(text: str, c: str) -> str:
#     return f'<font color="{c}">{text}</font>'
#
# def _arrow_bar(delta: int) -> str:
#     n = min(8, max(1, round(delta / 13)))
#     return "▲" * n
#
# def format_line_d(event: dict, link: bool = True) -> str:
#     etype = event.get("event_type", "")
#     title = _trim(event.get("product_title", ""), _TITLE_MAXLEN_D)
#     rank_cur = event.get("rank_current")
#     rank_prev = event.get("rank_previous")
#     delta = event.get("rank_delta")
#     url = event.get("product_url", "")
#     link_md = f"  [查看]({url})" if (link and url) else ""
#
#     if etype == NEW_ENTRY:
#         line1 = f"⚪ 〔{title}〕"
#         line2 = f"\t{_color(f'#{rank_cur}', 'comment')} 首次入榜{link_md}"
#         return f"{line1}\n{line2}"
#
#     bar = _color(_arrow_bar(delta or 0), "warning")
#     if etype == ENTER_TOP10:
#         sub = _color("冲进 TOP10", "warning")
#     elif etype == RANK_UP_50_PLUS_WARNING:
#         sub = _color(f"暴升 {delta} 位", "warning")
#     elif etype == RANK_UP_30_50_WARNING:
#         sub = _color(f"急升 {delta} 位", "warning")
#     else:
#         bar = _color(_arrow_bar(delta or 0), "info")
#         sub = _color(f"↑{delta}", "info")
#
#     line1 = f"{bar} {sub}"
#     line2_rank = (
#         f"{_color(f'#{rank_prev}', 'comment')} ➜ {_color(f'#{rank_cur}', 'warning')}"
#         if etype in (ENTER_TOP10, RANK_UP_50_PLUS_WARNING, RANK_UP_30_50_WARNING)
#         else f"{_color(f'#{rank_cur}', 'info')}"
#     )
#     line2 = f"〔{title}〕\n\t{line2_rank}{link_md}"
#     return f"{line1}\n{line2}"
