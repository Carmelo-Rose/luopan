import asyncio
from unittest.mock import AsyncMock

from collector.category_discovery import discover_categories


def test_empty_target_list_discovers_all_categories():
    page = AsyncMock()
    page.evaluate.return_value = {
        "options": [
            {
                "label": "智能家居",
                "value": "7",
                "children": [{"label": "家具", "value": "1001"}],
            },
            {
                "label": "图书教育",
                "value": "15",
                "children": [{"label": "文教文化用品", "value": "1002"}],
            },
        ],
    }

    tree = asyncio.run(discover_categories(page, []))

    assert set(tree) == {"智能家居", "图书教育"}


def test_target_list_filters_unavailable_categories():
    page = AsyncMock()
    page.evaluate.return_value = {
        "options": [
            {
                "label": "智能家居",
                "value": "7",
                "children": [{"label": "家具", "value": "1001"}],
            },
            {
                "label": "图书教育",
                "value": "15",
                "children": [{"label": "文教文化用品", "value": "1002"}],
            },
        ],
    }

    tree = asyncio.run(discover_categories(page, ["智能家居", "玩具乐器"]))

    assert set(tree) == {"智能家居"}


def test_l2_id_is_used_not_the_all_placeholder():
    """采集粒度为 L2：category_id 用 L2 自身 id，跳过 value=0 的「全部」占位项。"""
    page = AsyncMock()
    page.evaluate.return_value = {
        "options": [
            {
                "label": "智能家居",
                "value": "7",
                "children": [
                    {
                        "label": "五金/工具",
                        "value": "1000001142",
                        "children": [
                            {"label": "全部", "value": "0", "isLeaf": True},
                            {"label": "手动工具", "value": "1000001143"},
                        ],
                    },
                    {
                        "label": "电子/电工",
                        "value": "1000002719",
                        "children": [
                            {"label": "全部", "value": "0", "isLeaf": True},
                        ],
                    },
                ],
            },
        ],
    }

    tree = asyncio.run(discover_categories(page, ["智能家居"]))

    assert tree["智能家居"] == [
        {"name": "五金/工具", "category_id": "1000001142", "industry_id": "7"},
        {"name": "电子/电工", "category_id": "1000002719", "industry_id": "7"},
    ]


def test_all_placeholder_at_l2_level_is_skipped():
    """若「全部」(value=0) 误入 L2 列，应被跳过而不会产出 category_id=0 的脏数据。"""
    page = AsyncMock()
    page.evaluate.return_value = {
        "options": [
            {
                "label": "服饰内衣",
                "value": "4",
                "children": [
                    {"label": "全部", "value": "0", "isLeaf": True},
                    {"label": "服装", "value": "1000003282"},
                ],
            },
        ],
    }

    tree = asyncio.run(discover_categories(page, ["服饰内衣"]))

    assert tree["服饰内衣"] == [
        {"name": "服装", "category_id": "1000003282", "industry_id": "4"},
    ]
