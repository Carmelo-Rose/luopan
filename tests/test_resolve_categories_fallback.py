import asyncio
from unittest.mock import AsyncMock

import main
from config import settings


_STALE_TREE = {
    "智能家居": [{"name": "五金/工具", "category_id": "1001", "industry_id": "7"}],
    "图书教育": [{"name": "文教文化用品", "category_id": "1002", "industry_id": "15"}],
}


def _patch_cache(monkeypatch, fresh_tree):
    monkeypatch.setattr(
        settings, "TARGET_L1_CATEGORIES", ["智能家居", "图书教育"]
    )
    monkeypatch.setattr(settings, "TARGET_ALL_L1_CATEGORIES", False)
    monkeypatch.setattr(settings, "EXCLUDE_L2_CATEGORIES", [])
    monkeypatch.setattr(settings, "CATEGORY_TREE_CACHE", "unused/category_tree.json")
    monkeypatch.setattr(
        "collector.category_discovery.load_category_tree",
        lambda path, ttl=0, allow_expired=False: fresh_tree if allow_expired else None,
    )
    monkeypatch.setattr("collector.category_discovery.save_category_tree", lambda *a: None)


def test_discovery_crash_falls_back_to_expired_cache(monkeypatch):
    """登录态失效会让 discover_categories 抛异常，整轮采集仍要用过期缓存跑下去。"""
    _patch_cache(monkeypatch, _STALE_TREE)
    monkeypatch.setattr(
        "collector.category_discovery.discover_categories",
        AsyncMock(side_effect=RuntimeError("Page.evaluate: Execution context was destroyed")),
    )
    monkeypatch.setattr("collector.douyin_compass.DouyinCompassCollector", lambda: AsyncMock())

    flat = asyncio.run(main._resolve_categories())

    assert [c["category_name"] for c in flat] == ["五金/工具", "文教文化用品"]


def test_missing_l1_discovery_crash_keeps_cached_categories(monkeypatch):
    """部分一级类目缺失时补发现失败，也不能连带丢掉已缓存的类目。"""
    _patch_cache(monkeypatch, {"智能家居": _STALE_TREE["智能家居"]})
    monkeypatch.setattr(
        "collector.category_discovery.discover_categories",
        AsyncMock(side_effect=RuntimeError("Page.evaluate: Execution context was destroyed")),
    )
    monkeypatch.setattr("collector.douyin_compass.DouyinCompassCollector", lambda: AsyncMock())

    flat = asyncio.run(main._resolve_categories())

    assert [c["category_name"] for c in flat] == ["五金/工具"]
