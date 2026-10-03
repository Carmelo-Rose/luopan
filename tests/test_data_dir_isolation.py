from pathlib import Path

from config import settings


def test_category_caches_are_redirected_to_tmp_path(tmp_path):
    """tests/conftest.py 的自动夹具必须生效：类目缓存（及同目录的 raw dump）都落在 tmp_path。"""
    for cache in (settings.CATEGORY_TREE_CACHE, settings.CATEGORY_LOOKUP_CACHE):
        assert Path(cache).resolve().parent == tmp_path.resolve()
