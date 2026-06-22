from pathlib import Path
from unittest.mock import Mock

from notify import wecom


def test_summary_with_links(monkeypatch, tmp_path):
    """测试 send_summary 在有链接时正确构建消息。"""
    response = Mock(status_code=200)
    response.json.return_value = {"errcode": 0}
    monkeypatch.setattr(wecom.requests, "post", Mock(return_value=response))

    delivered = wecom.send_summary(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        events=[],
        categories_count=1,
        timestamp=__import__("datetime").datetime(2026, 6, 13, 10, 0),
        category_results=[],
        lark_url="https://feishu.cn/base/xxx?table=yyy",
        wecom_sheet_url="https://doc.weixin.qq.com/smartsheet/zzz",
    )

    assert delivered is True
    # 验证 POST 被调用且消息中包含链接
    call_args = wecom.requests.post.call_args
    content = call_args[1]["json"]["markdown"]["content"]
    assert "飞书多维表格" in content
    assert "企微智能表格" in content


def test_summary_without_links(monkeypatch):
    """测试无链接时也能正常发送。"""
    response = Mock(status_code=200)
    response.json.return_value = {"errcode": 0}
    monkeypatch.setattr(wecom.requests, "post", Mock(return_value=response))

    delivered = wecom.send_summary(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        events=[],
        categories_count=1,
        timestamp=__import__("datetime").datetime(2026, 6, 13, 10, 0),
        category_results=[],
    )

    assert delivered is True
