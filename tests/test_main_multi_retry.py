import asyncio
from unittest.mock import AsyncMock

from config import settings
from db import database
from main import _collect_main_category_with_retry


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

    products = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert len(products) == 3
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


def test_main_category_retries_on_exception(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 2)
    collector = AsyncMock()
    collector.collect.side_effect = [RuntimeError("transient"), [{}, {}]]

    products = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", True, retry_delay_seconds=0,
        )
    )

    assert len(products) == 2
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


def test_main_category_returns_empty_after_second_incomplete_result(monkeypatch):
    monkeypatch.setattr(settings, "MIN_PRODUCTS", 3)
    collector = AsyncMock()
    collector.collect.side_effect = [[{}], [{}, {}]]

    products = asyncio.run(
        _collect_main_category_with_retry(
            collector, _category(), "video_order_test", False, retry_delay_seconds=0,
        )
    )

    assert products == []
    assert collector.collect.await_count == 2
    collector.reset_page.assert_awaited_once()


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
