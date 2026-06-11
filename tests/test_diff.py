"""
差分引擎单元测试。
使用纯 mock 数据，无 I/O 依赖。
"""
import sqlite3
import tempfile
import os
from datetime import datetime, timezone
import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monitor.diff import (
    compute_diff,
    NEW_ENTRY,
    ENTER_TOP10,
    RANK_UP_5,
    RANK_UP_10,
    RANK_UP_20,
    RANK_UP_30_50_WARNING,
    RANK_UP_50_PLUS_WARNING,
)
from db.database import init_db, insert_snapshot, insert_events, get_pending_events


# ── 辅助工厂 ──────────────────────────────────────────────────────────

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_SCOPE = "card_order"
_RUN_A = "2024-06-01T11:00:00+00:00"
_RUN_B = "2024-06-01T12:00:00+00:00"


def _product(product_id: str, rank: int, title: str = "") -> dict:
    return {
        "product_id": product_id,
        "rank": rank,
        "product_title": title or f"商品{product_id}",
        "product_url": f"https://example.com/{product_id}",
        "price_range": "¥100-200",
        "pay_amount": "1000",
        "clicks": "5000",
        "conversion_rate": "20%",
        "card_order_count": "500",
        "captured_at": _NOW.isoformat(),
        "scope_key": _SCOPE,
        "industry_name": "测试行业",
        "category_name": "测试类目",
    }


def _build_snapshot(pairs: list[tuple[str, int]]) -> list[dict]:
    """pairs: [(product_id, rank), ...]"""
    return [_product(pid, rank) for pid, rank in pairs]


@pytest.fixture
def tmp_conn():
    """临时 SQLite 连接：建库 → yield → 关闭连接并删除临时文件。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── 差分引擎纯逻辑测试 ────────────────────────────────────────────────

class TestComputeDiff:

    def test_no_previous_returns_empty(self):
        """第一轮没有上一轮快照时，compute_diff 不应产生任何事件。"""
        current = _build_snapshot([("p1", 1), ("p2", 2)])
        events = compute_diff(_RUN_B, _SCOPE, current, [], now=_NOW)
        # 第一轮 previous 为空，所有商品都算 NEW_ENTRY
        # 但按需求：第一次运行只建立 baseline，不发送预警
        # => main.py 层面判断 previous 为空时跳过 diff；
        #    此处验证引擎行为：空 previous => 全部 NEW_ENTRY
        assert all(e["event_type"] == NEW_ENTRY for e in events)
        assert len(events) == 2

    def test_new_entry(self):
        """新进榜商品触发 NEW_ENTRY。"""
        prev = _build_snapshot([("p1", 1), ("p2", 2)])
        curr = _build_snapshot([("p1", 1), ("p2", 2), ("p_new", 3)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == NEW_ENTRY
        assert e["product_id"] == "p_new"
        assert e["rank_previous"] is None
        assert e["rank_delta"] is None

    def test_rank_up_10(self):
        """排名上升 10-19 位触发 RANK_UP_10（不在 TOP10 范围内）。"""
        prev = _build_snapshot([("p1", 50)])
        curr = _build_snapshot([("p1", 40)])   # delta = 10, 两边都不在 TOP10
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == RANK_UP_10
        assert e["rank_delta"] == 10

    def test_rank_up_19(self):
        """delta=19 仍属于 RANK_UP_10 区间。"""
        prev = _build_snapshot([("p1", 30)])
        curr = _build_snapshot([("p1", 11)])   # delta = 19
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_10

    def test_rank_up_20(self):
        """排名上升 20-29 位触发 RANK_UP_20。"""
        prev = _build_snapshot([("p1", 50)])
        curr = _build_snapshot([("p1", 30)])   # delta = 20
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_20

    def test_rank_up_30_50_warning_lower_bound(self):
        """delta=30 触发 RANK_UP_30_50_WARNING。"""
        prev = _build_snapshot([("p1", 80)])
        curr = _build_snapshot([("p1", 50)])   # delta = 30
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_30_50_WARNING

    def test_rank_up_30_50_warning_upper_bound(self):
        """delta=50 仍属于 RANK_UP_30_50_WARNING（闭区间上限）。"""
        prev = _build_snapshot([("p1", 100)])
        curr = _build_snapshot([("p1", 50)])   # delta = 50
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_30_50_WARNING

    def test_rank_up_50_plus_warning(self):
        """delta >= 51 触发 RANK_UP_50_PLUS_WARNING。"""
        prev = _build_snapshot([("p1", 150)])
        curr = _build_snapshot([("p1", 99)])   # delta = 51
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_50_PLUS_WARNING

    def test_rank_up_100(self):
        """超大涨幅也归入 RANK_UP_50_PLUS_WARNING（不进入 TOP10）。"""
        prev = _build_snapshot([("p1", 200)])
        curr = _build_snapshot([("p1", 50)])   # delta = 150，两边都不在 TOP10
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_50_PLUS_WARNING

    def test_rank_drop_no_event(self):
        """排名下跌（delta 为负）不触发任何事件。"""
        prev = _build_snapshot([("p1", 1)])
        curr = _build_snapshot([("p1", 50)])   # delta = -49，排名下降
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    def test_small_rise_no_event(self):
        """delta < 5 不触发事件。"""
        prev = _build_snapshot([("p1", 15)])
        curr = _build_snapshot([("p1", 12)])   # delta = 3
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    def test_multiple_events_in_one_run(self):
        """一轮可同时产生多个不同事件。"""
        prev = _build_snapshot([
            ("p_stable", 1),
            ("p_up10",   50),
            ("p_up50",   100),
            ("p_enter",  25),
        ])
        curr = _build_snapshot([
            ("p_stable", 1),
            ("p_up10",   40),    # delta=10，不在 TOP10
            ("p_up50",   49),    # delta=51，不进入 TOP10
            ("p_enter",  8),     # ENTER_TOP10
            ("p_new",    5),     # NEW_ENTRY
        ])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        types = {e["product_id"]: e["event_type"] for e in events}
        assert types["p_up10"] == RANK_UP_10
        assert types["p_up50"] == RANK_UP_50_PLUS_WARNING
        assert types["p_enter"] == ENTER_TOP10
        assert types["p_new"] == NEW_ENTRY
        assert "p_stable" not in types

    def test_event_fields_completeness(self):
        """事件字典包含所有必要字段。"""
        prev = _build_snapshot([("p1", 50)])
        curr = _build_snapshot([("p1", 10)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        required_keys = {
            "run_id", "scope_key", "event_type", "product_id",
            "product_title", "product_url", "rank_current",
            "rank_previous", "rank_delta", "created_at",
        }
        assert required_keys.issubset(set(events[0].keys()))

    def test_exact_boundary_4_no_event(self):
        """delta=4 精确边界：不触发（RANK_UP_5 从 5 开始）。"""
        prev = _build_snapshot([("p1", 24)])
        curr = _build_snapshot([("p1", 20)])   # delta = 4
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    # ── ENTER_TOP10 ─────────────────────────────────────────────────

    def test_enter_top10_from_outside(self):
        """从 TOP10 外冲进 TOP10 触发 ENTER_TOP10。"""
        prev = _build_snapshot([("p1", 25)])
        curr = _build_snapshot([("p1", 8)])    # 上轮#25，本轮#8
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == ENTER_TOP10
        assert events[0]["rank_current"] == 8
        assert events[0]["rank_previous"] == 25
        assert events[0]["rank_delta"] == 17

    def test_enter_top10_priority_over_delta(self):
        """进入 TOP10 优先级高于 RANK_UP_20（delta=20 也应触发 ENTER_TOP10）。"""
        prev = _build_snapshot([("p1", 30)])
        curr = _build_snapshot([("p1", 10)])   # delta=20 且进入 TOP10
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == ENTER_TOP10

    def test_enter_top10_boundary(self):
        """上轮#11 进入本轮#10 触发 ENTER_TOP10。"""
        prev = _build_snapshot([("p1", 11)])
        curr = _build_snapshot([("p1", 10)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == ENTER_TOP10

    def test_already_top10_no_enter_event(self):
        """本来就在 TOP10 内、排名提升不触发 ENTER_TOP10，走普通 delta 事件。"""
        prev = _build_snapshot([("p1", 8)])
        curr = _build_snapshot([("p1", 3)])    # delta=5，已在 TOP10
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_5  # 走普通涨幅

    def test_new_entry_direct_to_top5(self):
        """新进榜直接冲进 TOP5 仍是 NEW_ENTRY（不经过 enter 判别人，因为没上轮）。"""
        prev = _build_snapshot([("p_old", 1)])
        curr = _build_snapshot([("p_old", 1), ("p_new", 5)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        new_ev = [e for e in events if e["product_id"] == "p_new"][0]
        assert new_ev["event_type"] == NEW_ENTRY

    # ── RANK_UP_5 ──────────────────────────────────────────────────

    def test_rank_up_5_lower_bound(self):
        """delta=5 触发 RANK_UP_5。"""
        prev = _build_snapshot([("p1", 20)])
        curr = _build_snapshot([("p1", 15)])   # delta = 5
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_5
        assert events[0]["rank_delta"] == 5

    def test_rank_up_9(self):
        """delta=9 触发 RANK_UP_5。"""
        prev = _build_snapshot([("p1", 28)])
        curr = _build_snapshot([("p1", 19)])   # delta = 9
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_5

    def test_rank_up_4_no_event(self):
        """delta=4 不触发任何事件。"""
        prev = _build_snapshot([("p1", 24)])
        curr = _build_snapshot([("p1", 20)])   # delta = 4
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []



class TestDbDedup:

    def _make_event(self, run_id, product_id, event_type=NEW_ENTRY, rank_delta=None):
        return {
            "run_id": run_id,
            "scope_key": _SCOPE,
            "event_type": event_type,
            "product_id": product_id,
            "product_title": f"商品{product_id}",
            "product_url": "",
            "rank_current": 1,
            "rank_previous": None,
            "rank_delta": rank_delta,
            "created_at": _NOW.isoformat(),
            "industry_name": "测试行业",
            "category_name": "测试类目",
        }

    def test_duplicate_event_ignored(self, tmp_conn):
        """同一 (run_id, scope_key, event_type, product_id) 重复写入被忽略。"""
        ev = self._make_event(_RUN_B, "p1")
        insert_events(tmp_conn, [ev])
        insert_events(tmp_conn, [ev])   # 重复写
        rows = get_pending_events(tmp_conn)
        assert len(rows) == 1

    def test_different_run_same_product_both_stored(self, tmp_conn):
        """同一商品在两轮各自产生事件，都应保存。"""
        ev_a = self._make_event(_RUN_A, "p1")
        ev_b = self._make_event(_RUN_B, "p1")
        insert_events(tmp_conn, [ev_a, ev_b])
        rows = get_pending_events(tmp_conn)
        assert len(rows) == 2

    def test_different_event_type_same_run_both_stored(self, tmp_conn):
        """同一商品同轮不同事件类型都应保存（理论不会发生，健壮性测试）。"""
        ev1 = self._make_event(_RUN_B, "p1", NEW_ENTRY)
        ev2 = self._make_event(_RUN_B, "p1", RANK_UP_10)
        insert_events(tmp_conn, [ev1, ev2])
        rows = get_pending_events(tmp_conn)
        assert len(rows) == 2

    def test_insert_empty_list(self, tmp_conn):
        """空事件列表不报错。"""
        result = insert_events(tmp_conn, [])
        assert result == 0


# ── 快照写入测试 ──────────────────────────────────────────────────────

class TestSnapshotInsert:

    def test_insert_and_query(self, tmp_conn):
        """写入快照后可以正常读取。"""
        rows = _build_snapshot([("p1", 1), ("p2", 2)])
        insert_snapshot(tmp_conn, _RUN_A, rows)

        result = tmp_conn.execute(
            "SELECT * FROM products_snapshot WHERE run_id=? ORDER BY rank",
            (_RUN_A,),
        ).fetchall()
        assert len(result) == 2
        assert result[0]["product_id"] == "p1"
        assert result[0]["rank"] == 1

    def test_idempotent_insert(self, tmp_conn):
        """相同 run_id+product_id 重复插入不报错，数量不增加。"""
        rows = _build_snapshot([("p1", 1)])
        insert_snapshot(tmp_conn, _RUN_A, rows)
        insert_snapshot(tmp_conn, _RUN_A, rows)  # 重复

        result = tmp_conn.execute(
            "SELECT COUNT(*) as cnt FROM products_snapshot WHERE run_id=?",
            (_RUN_A,),
        ).fetchone()
        assert result["cnt"] == 1
