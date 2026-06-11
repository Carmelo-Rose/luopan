"""
推送路由：根据 NOTIFY_CHANNEL 配置，分发到对应渠道。
"""
import logging
from config import settings
from notify import wecom, lark

logger = logging.getLogger(__name__)


def dispatch(events: list[dict], scope_key: str = "") -> set:
    """
    将事件推送到配置的渠道。

    返回**已送达事件 id 集合**：
      - wecom / lark：实际推送结果（成功送达的 id）
      - none：视为全部送达（返回全部 id），便于 dry/测试链路标记 notified
      - 未知渠道：返回空集（不标记，留待修正配置后重试）
    """
    if not events:
        return set()

    channel = settings.NOTIFY_CHANNEL.lower()

    if channel == "wecom":
        return wecom.send_events(settings.WECOM_WEBHOOK_URL, events, scope_key)

    if channel == "lark":
        return lark.send_events(settings.LARK_WEBHOOK_URL, events, scope_key)

    if channel == "none":
        logger.info("NOTIFY_CHANNEL=none，跳过推送，共 %d 条事件", len(events))
        return {e["id"] for e in events if e.get("id") is not None}

    logger.warning("未知 NOTIFY_CHANNEL=%s，跳过推送", channel)
    return set()
