"""
飞书群机器人 Webhook 推送（存根，备用）。
文档：https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN

消息按 notify/templates.py 的"推送规则"分组展示，与企微一致。
"""
import logging
import requests

from notify.templates import group_events, format_line

logger = logging.getLogger(__name__)


def send_events(webhook_url: str, events: list[dict], scope_key: str = "") -> set:
    """
    推送到飞书 Webhook（单条文本，存根渠道）。

    与 wecom 一致：返回已送达事件 id 集合。单条消息成功则全部 id，失败返回空集。
    飞书单条文本上限很大，这里不分块。
    """
    if not events:
        return set()
    if not webhook_url:
        logger.warning("LARK_WEBHOOK_URL 未配置，跳过推送")
        return set()

    lines = [f"📊 抖音罗盘榜监控（{scope_key}）　共 {len(events)} 条变动"]
    for gtitle, gevents in group_events(events):
        lines.append("")
        lines.append(f"{gtitle}（{len(gevents)}）")
        for ev in gevents:
            lines.append(format_line(ev, link=False))
    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error("飞书 Webhook HTTP %d: %s", resp.status_code, resp.text[:200])
            return set()
        data = resp.json()
        # 显式判定成功：飞书成功响应 code==0（新版）或 StatusCode==0（旧版）。
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            return {e["id"] for e in events if e.get("id") is not None}
        logger.error("飞书 Webhook 返回错误: %s", data)
        return set()
    except Exception as exc:
        logger.error("飞书 Webhook 推送异常: %s", exc)
        return set()
