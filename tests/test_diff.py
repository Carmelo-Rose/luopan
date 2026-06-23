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
    RANK_UP_50,
    RANK_UP_100,
    RANK_UP_150,
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
        """第一轮没有上一轮快照时，所有商品都算 NEW_ENTRY。"""
        current = _build_snapshot([("p1", 1), ("p2", 2)])
        events = compute_diff(_RUN_B, _SCOPE, current, [], now=_NOW)
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

    # ── RANK_UP_50 (delta 50~99) ────────────────────────────────────

    def test_rank_up_50_lower_bound(self):
        """delta=50 触发 RANK_UP_50。"""
        prev = _build_snapshot([("p1", 100)])
        curr = _build_snapshot([("p1", 50)])   # delta = 50
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert len(events) == 1
        assert events[0]["event_type"] == RANK_UP_50
        assert events[0]["rank_delta"] == 50

    def test_rank_up_50_upper_bound(self):
        """delta=99 仍属于 RANK_UP_50。"""
        prev = _build_snapshot([("p1", 199)])
        curr = _build_snapshot([("p1", 100)])   # delta = 99
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_50

    def test_rank_up_49_no_event(self):
        """delta=49 不触发事件（低于 50 阈值）。"""
        prev = _build_snapshot([("p1", 100)])
        curr = _build_snapshot([("p1", 51)])   # delta = 49
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    # ── RANK_UP_100 (delta 100~149) ────────────────────────────────

    def test_rank_up_100_lower_bound(self):
        """delta=100 触发 RANK_UP_100。"""
        prev = _build_snapshot([("p1", 150)])
        curr = _build_snapshot([("p1", 50)])   # delta = 100
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_100
        assert events[0]["rank_delta"] == 100

    def test_rank_up_100_upper_bound(self):
        """delta=149 仍属于 RANK_UP_100。"""
        prev = _build_snapshot([("p1", 199)])
        curr = _build_snapshot([("p1", 50)])   # delta = 149
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_100

    # ── RANK_UP_150 (delta >= 150) ─────────────────────────────────

    def test_rank_up_150_lower_bound(self):
        """delta=150 触发 RANK_UP_150。"""
        prev = _build_snapshot([("p1", 200)])
        curr = _build_snapshot([("p1", 50)])   # delta = 150
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_150
        assert events[0]["rank_delta"] == 150

    def test_rank_up_150_large_delta(self):
        """超大 delta 仍归入 RANK_UP_150。"""
        prev = _build_snapshot([("p1", 200)])
        curr = _build_snapshot([("p1", 1)])   # delta = 199
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events[0]["event_type"] == RANK_UP_150

    # ── 无事件场景 ──────────────────────────────────────────────────

    def test_rank_drop_no_event(self):
        """排名下跌（delta 为负）不触发任何事件。"""
        prev = _build_snapshot([("p1", 1)])
        curr = _build_snapshot([("p1", 50)])   # delta = -49
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    def test_small_rise_no_event(self):
        """delta < 50 不触发事件。"""
        prev = _build_snapshot([("p1", 100)])
        curr = _build_snapshot([("p1", 80)])   # delta = 20
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    def test_exact_boundary_49_no_event(self):
        """delta=49 精确边界：不触发（RANK_UP_50 从 50 开始）。"""
        prev = _build_snapshot([("p1", 149)])
        curr = _build_snapshot([("p1", 100)])   # delta = 49
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        assert events == []

    # ── 多事件 ──────────────────────────────────────────────────────

    def test_multiple_events_in_one_run(self):
        """一轮可同时产生多个不同事件。"""
        prev = _build_snapshot([
            ("p_stable", 1),
            ("p_up50",   100),
            ("p_up100",  200),
            ("p_up150",  200),
        ])
        curr = _build_snapshot([
            ("p_stable", 1),
            ("p_up50",   50),     # delta=50, RANK_UP_50
            ("p_up100",  99),     # delta=101, RANK_UP_100
            ("p_up150",  1),      # delta=199, RANK_UP_150
            ("p_new",    5),      # NEW_ENTRY
        ])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        types = {e["product_id"]: e["event_type"] for e in events}
        assert types["p_up50"] == RANK_UP_50
        assert types["p_up100"] == RANK_UP_100
        assert types["p_up150"] == RANK_UP_150
        assert types["p_new"] == NEW_ENTRY
        assert "p_stable" not in types

    def test_event_fields_completeness(self):
        """事件字典包含所有必要字段。"""
        prev = _build_snapshot([("p1", 150)])
        curr = _build_snapshot([("p1", 50)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        required_keys = {
            "run_id", "scope_key", "event_type", "product_id",
            "product_title", "product_url", "rank_current",
            "rank_previous", "rank_delta", "created_at",
        }
        assert required_keys.issubset(set(events[0].keys()))

    def test_new_entry_direct_to_top5(self):
        """新进榜直接进 TOP5 仍是 NEW_ENTRY（没上轮排名，不走 delta 判别）。"""
        prev = _build_snapshot([("p_old", 1)])
        curr = _build_snapshot([("p_old", 1), ("p_new", 5)])
        events = compute_diff(_RUN_B, _SCOPE, curr, prev, now=_NOW)
        new_ev = [e for e in events if e["product_id"] == "p_new"][0]
        assert new_ev["event_type"] == NEW_ENTRY


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
        ev2 = self._make_event(_RUN_B, "p1", RANK_UP_50)
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
