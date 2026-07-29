import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import main
from collector.douyin_compass import RateLimitError
from config import settings
from db import database
from main import _collect_main_category_with_retry, _load_summary_sidecar, _save_summary_sidecar


def _category():
    return {
        "industry_id": "15",
        "category_id": "1000001648",
        "industry_name": "图书教育",
        "category_name": "文教文化用品",
    }


def test_main_category_retries_on_empty_result(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 3)
    collector = AsyncMock()
    collector.collect.side_effect = [[], [{}, {}, {}]]

    products, rate_limited = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert len(products) == 3
    assert rate_limited is False
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


def test_main_category_retries_on_exception(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 2)
    collector = AsyncMock()
    collector.collect.side_effect = [RuntimeError("transient"), [{}, {}]]

    products, rate_limited = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", True, retry_delay_seconds=0,
        )
    )

    assert len(products) == 2
    assert rate_limited is False
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


def test_main_category_returns_empty_after_second_incomplete_result(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 3)
    collector = AsyncMock()
    collector.collect.side_effect = [[{}], [{}, {}]]

    products, rate_limited = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert products == []
    assert rate_limited is False
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


def test_main_category_rate_limit_recovers_after_backoff(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 2)
    monkeypatch.setattr(settings, "RATE_LIMIT_BACKOFFS", [0, 0, 0])
    collector = AsyncMock()
    collector.collect.side_effect = [
        RateLimitError("code=11001", "请求过于频繁"),
        RateLimitError("code=11001", "请求过于频繁"),
        [{}, {}],
    ]

    products, rate_limited = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert len(products) == 2
    assert rate_limited is False
    # 第 1 次撞限流即转入退避重试，不走「换新页面固定重试」那条路
    collector.reset_page.assert_not_awaited()
    assert collector.collect.await_count == 3


def test_main_category_rate_limit_exhausts_backoff(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 2)
    monkeypatch.setattr(settings, "RATE_LIMIT_BACKOFFS", [0, 0])
    collector = AsyncMock()
    collector.collect.side_effect = RateLimitError("code=11001", "请求过于频繁")

    products, rate_limited = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert products == []
    assert rate_limited is True
    # 首次 + 2 档退避 = 3 次调用
    assert collector.collect.await_count == 3


def _snapshot(scope_key):
    return {
        "scope_key": scope_key,
        "rank": 1,
        "product_id": scope_key,
        "product_title": "test",
        "product_url": "",
        "price_range": "",
        "pay_amount": "",
        "clicks": "",
        "conversion_rate": "",
        "card_order_count": "",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "industry_name": "test",
        "category_name": "test",
    }


def test_discard_run_removes_only_the_failed_lane(tmp_path):
    db_path = tmp_path / "compass.db"
    database.init_db(str(db_path))
    conn = database.get_connection(str(db_path))
    try:
        database.insert_snapshot(conn, "failed-run", [_snapshot("video_order_test")])
        database.insert_snapshot(conn, "failed-run", [_snapshot("video_acc_test")])
        database.insert_events(conn, [{
            "run_id": "failed-run",
            "scope_key": "video_order_test",
            "event_type": "NEW_ENTRY",
            "product_id": "order-event",
            "product_title": "test",
            "product_url": "",
            "rank_current": 1,
            "rank_previous": None,
            "rank_delta": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }])

        snapshots, events = database.discard_run(conn, "failed-run", "video_order")

        assert (snapshots, events) == (1, 1)
        remaining = conn.execute(
            "select scope_key from products_snapshot where run_id='failed-run'"
        ).fetchall()
        assert [row["scope_key"] for row in remaining] == ["video_acc_test"]
    finally:
        conn.close()


def test_failed_summary_sidecar_marks_flush_as_blocked(monkeypatch, tmp_path):
    sidecar_path = tmp_path / "pending_summary_multi.json"
    monkeypatch.setattr(main, "_summary_sidecar_path", lambda lane: str(sidecar_path))

    _save_summary_sidecar(
        "multi",
        "failed-run",
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        categories_collected=13,
        category_results=[{"status": "采集失败"}],
        scope_prefix="video_order",
        categories_expected=19,
        collection_failed=True,
    )

    sidecar = _load_summary_sidecar("multi")

    assert sidecar["collection_failed"] is True
    assert sidecar["categories_collected"] == 13
    assert sidecar["categories_expected"] == 19
