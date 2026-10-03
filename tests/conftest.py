"""tests/ 公共夹具。"""
import pytest

from config import settings


@pytest.fixture(autouse=True)
def _isolate_category_caches(monkeypatch, tmp_path):
    """把类目缓存路径重定向到 tmp_path，防止测试把假数据写进真实 data/。

    collector.category_discovery 的 discover_categories / ensure_category_lookup 会落盘
    category_lookup.json，并把 category_raw_dump.json 写到 CATEGORY_TREE_CACHE 的同目录；
    生产机上 --acc 靠 data/category_raw_dump.json 解析服配叶子，被测试覆盖会直接挂。
    """
    monkeypatch.setattr(settings, "CATEGORY_TREE_CACHE", str(tmp_path / "category_tree.json"))
    monkeypatch.setattr(settings, "CATEGORY_LOOKUP_CACHE", str(tmp_path / "category_lookup.json"))
