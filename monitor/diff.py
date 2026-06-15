"""
差分引擎：对比相邻两轮快照，生成事件列表。

事件优先级（互斥，同商品只触发最高优先级事件）：
    ENTER_TOP10              上轮不在 TOP10 且本轮进入 TOP10
    RANK_UP_50_PLUS_WARNING  delta >= 51
    RANK_UP_30_50_WARNING    delta in [30, 50]
    RANK_UP_20               delta in [20, 29]
    RANK_UP_10               delta in [10, 19]
    RANK_UP_5                delta in [5, 9]
    NEW_ENTRY                product_id 不在上一轮榜单

注意：排名数值越小越好（rank=1 最优）。
      delta = rank_previous - rank_current（正数 = 排名上升）。
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


EventType = str

# 事件类型常量
NEW_ENTRY = "NEW_ENTRY"
ENTER_TOP10 = "ENTER_TOP10"
RANK_UP_5 = "RANK_UP_5"
RANK_UP_10 = "RANK_UP_10"
RANK_UP_20 = "RANK_UP_20"
RANK_UP_30_50_WARNING = "RANK_UP_30_50_WARNING"
RANK_UP_50_PLUS_WARNING = "RANK_UP_50_PLUS_WARNING"

_TOP10_THRESHOLD = 10


def _classify_event(rank_cur: int, rank_prev: int, delta: int) -> Optional[EventType]:
    """
    按优先级匹配事件类型。
    排名数值越小越好，delta 正数 = 排名上升。
    """
    # 优先：冲进 TOP10
    if rank_cur <= _TOP10_THRESHOLD and rank_prev > _TOP10_THRESHOLD:
        return ENTER_TOP10

    # 按 delta 分级
    if delta >= 51:
        return RANK_UP_50_PLUS_WARNING
    if delta >= 30:
        return RANK_UP_30_50_WARNING
    if delta >= 20:
        return RANK_UP_20
    if delta >= 10:
        return RANK_UP_10
    if delta >= 5:
        return RANK_UP_5
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
            event_type = _classify_event(rank_cur, rank_prev, delta)
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
        "rank_current": rank_current,
        "rank_previous": rank_previous,
        "rank_delta": rank_delta,
        "created_at": created_at,
        "industry_name": row.get("industry_name", ""),
        "category_name": row.get("category_name", ""),
        "category_l3_name": row.get("category_l3_name", ""),
        "leaf_category_name": row.get("leaf_category_name", ""),
    }
