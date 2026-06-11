"""
企业微信群机器人 Webhook 推送。
文档：https://developer.work.weixin.qq.com/document/path/91770

消息按 notify/templates.py 的"推送规则"分组展示（新进 / 上升 10-30 / 30-50 / 50+）。
"""
import logging
import requests

from notify.templates import group_events, format_line, build_header

logger = logging.getLogger(__name__)

# 企微 markdown.content 上限 4096 **字节**（不是字符！中文 3 字节、emoji 4 字节），
# 故分块预算按 UTF-8 字节计，并留余量。
_MAX_LEN = 4000


def _blen(s: str) -> int:
    """字符串的 UTF-8 字节长度。"""
    return len(s.encode("utf-8"))


def _truncate_bytes(s: str, max_bytes: int) -> str:
    """按字节截断字符串（不切坏多字节字符），超长时附省略号。"""
    if _blen(s) <= max_bytes:
        return s
    budget = max(0, max_bytes - 3)  # 给 "…" 预留 3 字节
    return s.encode("utf-8")[:budget].decode("utf-8", "ignore") + "…"


def send_events(webhook_url: str, events: list[dict], scope_key: str = "") -> set:
    """
    将一批事件按分组模板推送到企微 Webhook。

    返回**已成功送达的事件 id 集合**：逐 chunk 原子 POST，遇首个失败（HTTP 非 200 /
    errcode!=0 / 异常）立即停止，只把此前成功 POST 的 chunk 内事件 id 计入返回集合。
    调用方据此标记 notified，未送达的留待下轮重试，既支持重试又不重复推送。
    """
    if not events:
        return set()
    if not webhook_url:
        logger.warning("WECOM_WEBHOOK_URL 未配置，跳过推送")
        return set()

    header = build_header(scope_key, len(events))
    groups = group_events(events)
    chunks = _chunk_grouped(header, groups, max_len=_MAX_LEN)

    delivered: set = set()
    for content, chunk_events in chunks:
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error("企微 Webhook HTTP %d: %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error("企微 Webhook 返回错误: %s", data)
                break
        except Exception as exc:
            logger.error("企微 Webhook 推送异常: %s", exc)
            break
        delivered.update(e["id"] for e in chunk_events if e.get("id") is not None)

    return delivered


def _chunk_grouped(
    top_header: str,
    groups: list[tuple[str, list[dict]]],
    max_len: int,
) -> list[tuple[str, list[dict]]]:
    """
    把分组事件渲染为若干 chunk，每个 chunk 前附 top_header；分组标题在每个 chunk 内
    首次出现该组时再渲染一次（跨 chunk 续传时标题会重复，避免断头）。
    返回 [(content, events_in_chunk), ...]，块内事件用于成功后按 id 标记。
    """
    chunks: list[tuple[str, list[dict]]] = []
    header_len = _blen(top_header)
    cur_lines = [top_header]
    cur_events: list[dict] = []
    cur_len = header_len  # 累计按 UTF-8 字节计

    def flush():
        nonlocal cur_lines, cur_events, cur_len
        if cur_events:
            chunks.append(("\n".join(cur_lines), cur_events))
        cur_lines = [top_header]
        cur_events = []
        cur_len = header_len

    for gtitle, gevents in groups:
        section = f"\n**{gtitle}（{len(gevents)}）**"
        section_len = _blen(section)
        header_emitted = False
        for ev in gevents:
            line = format_line(ev, link=True)
            # 单条本身超预算时按字节截断，避免单 chunk 超企微上限被整条拒收
            room = max_len - header_len - section_len - 4
            if room > 1:
                line = _truncate_bytes(line, room)
            line_len = _blen(line)

            need = line_len + 1 + (0 if header_emitted else section_len + 1)
            if cur_len + need > max_len and cur_events:
                flush()
                header_emitted = False  # 新 chunk 需重新渲染分组标题
                need = line_len + 1 + section_len + 1

            if not header_emitted:
                cur_lines.append(section)
                cur_len += section_len + 1
                header_emitted = True
            cur_lines.append(line)
            cur_events.append(ev)
            cur_len += line_len + 1

    flush()
    return chunks


# ── 多类目摘要推送 ────────────────────────────────────────────────────

def send_summary(
    webhook_url: str,
    events: list[dict],
    categories_count: int,
    excel_path: str,
    timestamp,
) -> bool:
    """
    多类目模式：发送简短摘要到企微（不推全量事件）。

    格式：
        📊 罗盘榜单异动  2026-06-11 14:00
        共采集 35 个二级类目，发现 128 条异动

        智能家居: 23 条 | 电子/电工: 45 条 | 家具: 12 条
        玩具乐器: 18 条 | 钟表配饰: 8 条

        完整报告：data/reports/榜单异动_2026-06-11_14-00.xlsx
    """
    if not events:
        # 无异动也发一条
        content = (
            f"**📊 罗盘榜单异动**\n"
            f"🕐 {timestamp.strftime('%Y-%m-%d %H:%M')}\n"
            f"共采集 {categories_count} 个二级类目，**0 条异动**"
        )
    else:
        # 按一级类目统计
        l1_counts: dict[str, int] = {}
        for ev in events:
            l1 = ev.get("industry_name", "未知")
            l1_counts[l1] = l1_counts.get(l1, 0) + 1

        l1_lines = []
        for name, count in sorted(l1_counts.items(), key=lambda x: -x[1]):
            l1_lines.append(f"{name}: **{count}** 条")
        l1_text = " | ".join(l1_lines)

        excel_hint = f"\n完整报告：{excel_path}" if excel_path else ""

        content = (
            f"**📊 罗盘榜单异动**\n"
            f"🕐 {timestamp.strftime('%Y-%m-%d %H:%M')}\n"
            f"共采集 {categories_count} 个二级类目，发现 **{len(events)}** 条异动\n\n"
            f"{l1_text}"
            f"{excel_hint}"
        )

    if not webhook_url:
        logger.warning("WECOM_WEBHOOK_URL 未配置，跳过摘要推送")
        return False

    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error("企微摘要 HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error("企微摘要返回错误: %s", data)
            return False
        logger.info("企微摘要已推送")
        return True
    except Exception as exc:
        logger.error("企微摘要推送异常: %s", exc)
        return False
