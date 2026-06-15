from pathlib import Path
from unittest.mock import Mock

from notify import wecom


def test_summary_requires_excel_delivery(monkeypatch, tmp_path):
    excel_path = Path(tmp_path) / "report.xlsx"
    excel_path.write_bytes(b"xlsx")
    response = Mock(status_code=200)
    response.json.return_value = {"errcode": 0}
    monkeypatch.setattr(wecom.requests, "post", Mock(return_value=response))
    monkeypatch.setattr(wecom, "_upload_file", Mock(return_value="media-id"))
    monkeypatch.setattr(wecom, "_send_file", Mock(return_value=False))

    delivered = wecom.send_summary(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        events=[],
        categories_count=1,
        excel_path=str(excel_path),
        timestamp=__import__("datetime").datetime(2026, 6, 13, 10, 0),
        category_results=[],
    )

    assert delivered is False
