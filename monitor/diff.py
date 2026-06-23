"""
差分引擎：对比相邻两轮快照，生成事件列表。

事件类型（互斥，同商品只触发最高档位事件）：
    RANK_UP_150   delta >= 150
    RANK_UP_100   delta in [100, 149]
    RANK_UP_50    delta in [50, 99]
    NEW_ENTRY     product_id 不在上一轮榜单

注意：排名数值越小越好（rank=1 最优）。
      delta = rank_previous - rank_current（正数 = 排名上升）。
      排名下跌或小幅波动（delta < 50）不触发事件。
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


EventType = str

# 事件类型常量
NEW_ENTRY = "NEW_ENTRY"
RANK_UP_50 = "RANK_UP_50"
RANK_UP_100 = "RANK_UP_100"
RANK_UP_150 = "RANK_UP_150"


def _classify_event(delta: int) -> Optional[EventType]:
    """
    按 delta 分级匹配事件类型（仅数值，不涉及 TOP10 特判）。
    排名数值越小越好，delta 正数 = 排名上升。
    """
    if delta >= 150:
        return RANK_UP_150
    if delta >= 100:
        return RANK_UP_100
    if delta >= 50:
        return RANK_UP_50
    return None


def compute_diff(
    run_id: str,
    scope_key: str,
    current_snapshot: list[dict],
    previous_snapshot: list[dict],
    now: Optional[datetime] = None,
) -> list[dict]:
    """
    计算两轮快照之间的事件。

    Parameters
    ----------
    run_id           : 当前轮次 ID（写入事件的 run_id）
    scope_key        : 榜单维度
    current_snapshot : 当前轮 list[dict]，每条含 rank / product_id 等字段
    previous_snapshot: 上一轮 list[dict]
    now              : 事件时间戳，默认 UTC now

    Returns
    -------
    list[dict]  可直接传入 db.insert_events() 的事件列表
    """
    if now is None:
        now = datetime.now(timezone.utc)
    created_at = now.isoformat()

    # 上一轮：product_id -> rank，检测重复 product_id
    prev_map: dict[str, int] = {}
    for row in previous_snapshot:
        pid = row["product_id"]
        if pid in prev_map:
            logger.warning("上一轮快照中存在重复 product_id=%s（保留 rank=%d）", pid, row["rank"])
        prev_map[pid] = row["rank"]

    events: list[dict] = []

    for row in current_snapshot:
        pid = row["product_id"]
        rank_cur = row["rank"]

        if not isinstance(rank_cur, int) or rank_cur < 1:
            logger.warning("无效排名 rank_cur=%r (product_id=%s)，跳过", rank_cur, pid)
            continue

        if pid not in prev_map:
            # 新进榜
            events.append(
                _make_event(
                    run_id=run_id,
                    scope_key=scope_key,
                    event_type=NEW_ENTRY,
                    row=row,
                    rank_current=rank_cur,
                    rank_previous=None,
                    rank_delta=None,
                    created_at=created_at,
                )
            )
        else:
            rank_prev = prev_map[pid]
            delta = rank_prev - rank_cur  # 正数 = 排名上升
            event_type = _classify_event(delta)
            if event_type:
                events.append(
                    _make_event(
                        run_id=run_id,
                        scope_key=scope_key,
                        event_type=event_type,
                        row=row,
                        rank_current=rank_cur,
                        rank_previous=rank_prev,
                        rank_delta=delta,
                        created_at=created_at,
                    )
                )

    return events


def _make_event(
    *,
    run_id: str,
    scope_key: str,
    event_type: EventType,
    row: dict,
    rank_current: int,
    rank_previous: Optional[int],
    rank_delta: Optional[int],
    created_at: str,
) -> dict:
    return {
        "run_id": run_id,
        "scope_key": scope_key,
        "event_type": event_type,
        "product_id": row["product_id"],
        "product_title": row.get("product_title", ""),
        "product_url": row.get("product_url", ""),
        "image": row.get("image", ""),
        # pay_amount 取自快照；price 由 main 在拓价后回填（默认回退脱敏价格带）
        "pay_amount": row.get("pay_amount", ""),
        "price": row.get("price_range", ""),
        "rank_current": rank_current,
        "rank_previous": rank_previous,
        "rank_delta": rank_delta,
        "created_at": created_at,
        "industry_name": row.get("industry_name", ""),
        "category_name": row.get("category_name", ""),
        "category_l3_name": row.get("category_l3_name", ""),
        "leaf_category_name": row.get("leaf_category_name", ""),
    }
