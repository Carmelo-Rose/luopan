from urllib.parse import parse_qs, urlparse

from collector.douyin_compass import _apply_query_overrides


def test_apply_query_overrides_replaces_and_preserves_params():
    original = (
        "https://example.com/product_card_hot_v2"
        "?date_type=21&industry_id=1&page_no=1&search_info="
    )

    result = _apply_query_overrides(
        original,
        {
            "date_type": "1",
            "industry_id": "7",
            "category_id": "1000002719",
        },
    )

    params = parse_qs(urlparse(result).query, keep_blank_values=True)
    assert params["date_type"] == ["1"]
    assert params["industry_id"] == ["7"]
    assert params["category_id"] == ["1000002719"]
    assert params["page_no"] == ["1"]
    assert params["search_info"] == [""]


def test_apply_query_overrides_ignores_empty_values():
    original = "https://example.com/product_card_hot_v2?category_id=old"

    result = _apply_query_overrides(original, {"category_id": ""})

    assert parse_qs(urlparse(result).query) == {"category_id": ["old"]}
