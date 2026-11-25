import asyncio
from unittest.mock import AsyncMock

import main
from config import settings


# category_id / industry_id 只是夹具，不是线上类目 id，不能拿去采集。
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
    monkeypatch.setattr(settings, "EXCLUDE_L1_L2_CATEGORIES", [])
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


# 夹具 id，不是线上类目 id。图书教育下保留第二个二级，证明不是收成只采文教。
_SCOPE_TREE = {
    "图书教育": [
        {"name": "文教文化用品", "category_id": "fixture-edu-supplies", "industry_id": "fixture-edu"},
        {"name": "书籍/杂志/报纸", "category_id": "fixture-edu-books", "industry_id": "fixture-edu"},
    ],
    "服饰内衣": [
        {"name": "服装", "category_id": "fixture-apparel", "industry_id": "fixture-cloth"},
        {"name": "内衣", "category_id": "fixture-underwear", "industry_id": "fixture-cloth"},
    ],
    "运动户外": [
        {"name": "服装", "category_id": "fixture-sport-apparel", "industry_id": "fixture-sport"},
    ],
}


def _patch_fresh(monkeypatch, exclude_l2, exclude_pairs):
    monkeypatch.setattr(
        settings,
        "TARGET_L1_CATEGORIES",
        ["图书教育", "服饰内衣", "运动户外"],
    )
    monkeypatch.setattr(settings, "TARGET_ALL_L1_CATEGORIES", False)
    monkeypatch.setattr(settings, "EXCLUDE_L2_CATEGORIES", exclude_l2)
    monkeypatch.setattr(settings, "EXCLUDE_L1_L2_CATEGORIES", exclude_pairs)
    monkeypatch.setattr(settings, "CATEGORY_TREE_CACHE", "unused/category_tree.json")
    monkeypatch.setattr(
        "collector.category_discovery.load_category_tree",
        lambda path, ttl=0, allow_expired=False: _SCOPE_TREE,
    )


def test_parse_l1_l2_exclusions():
    from config.settings import _EXCLUDE_L1_L2_DEFAULT, parse_l1_l2_exclusions

    assert parse_l1_l2_exclusions(_EXCLUDE_L1_L2_DEFAULT) == [("服饰内衣", "服装")]
    assert parse_l1_l2_exclusions("") == []
    assert parse_l1_l2_exclusions("服装") == []
    assert parse_l1_l2_exclusions(" 服饰内衣>服装 , 运动户外>户外/服装 ") == [
        ("服饰内衣", "服装"),
        ("运动户外", "户外/服装"),
    ]


def test_multi_keeps_edu_supplies_and_drops_only_apparel_pair(monkeypatch):
    """图书教育的二级原样纳入；只去掉服饰内衣>服装，其他一级的「服装」还在。"""
    _patch_fresh(monkeypatch, [], [("服饰内衣", "服装")])

    flat = asyncio.run(main._resolve_categories())
    pairs = [(c["industry_name"], c["category_name"]) for c in flat]

    assert ("图书教育", "文教文化用品") in pairs
    assert ("图书教育", "书籍/杂志/报纸") in pairs
    assert ("服饰内衣", "服装") not in pairs
    assert ("服饰内衣", "内衣") in pairs
    assert ("运动户外", "服装") in pairs
    edu = next(c for c in flat if c["category_name"] == "文教文化用品")
    assert edu["category_id"] == "fixture-edu-supplies"
    assert edu["industry_id"] == "fixture-edu"


def test_name_only_exclude_still_drops_same_l2_under_every_l1(monkeypatch):
    """旧键仍按二级名排除，含义不变：两个一级下的「服装」都会跳。"""
    _patch_fresh(monkeypatch, ["服装"], [])

    flat = asyncio.run(main._resolve_categories())
    pairs = [(c["industry_name"], c["category_name"]) for c in flat]

    assert ("服饰内衣", "服装") not in pairs
    assert ("运动户外", "服装") not in pairs
    assert ("图书教育", "文教文化用品") in pairs
    assert ("服饰内衣", "内衣") in pairs
