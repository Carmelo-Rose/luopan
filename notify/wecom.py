"""
企业微信群机器人 Webhook 推送。
文档：https://developer.work.weixin.qq.com/document/path/91770

消息按 notify/templates.py 的分组规则展示（升150+ / 升100+ / 升50+ / 新进榜）。
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
            available = max_len - header_len - section_len - 4
            if _blen(line) > available:
                line = _truncate_bytes(line, available)
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
    timestamp,
    category_results: list[dict] | None = None,
    lark_url: str = "",
    wecom_sheet_url: str = "",
) -> bool:
    """
    多类目模式：发送简短摘要到企微，附带在线表格链接（不推全量事件）。

    格式：
        📊 罗盘榜单异动  2026-06-11 14:00
        共采集 35 个二级类目，发现 128 条异动

        智能家居: 23 条 | 电子/电工: 45 条 | 家具: 12 条
        玩具乐器: 18 条 | 钟表配饰: 8 条

        📊 飞书多维表格：[点击查看](链接)
        📊 企微智能表格：[点击查看](链接)
    """
    # 确保时间为北京时间
    from datetime import timezone as _tz, timedelta
    cst = _tz(timedelta(hours=8))
    if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
        ts = timestamp.astimezone(cst)
    else:
        ts = timestamp
    ts_str = ts.strftime('%Y-%m-%d %H:%M')
    category_results = category_results or []
    failed_count = sum(1 for r in category_results if r.get("status") in ("采集失败", "采集异常"))
    baseline_count = sum(1 for r in category_results if r.get("status") == "首次基线")
    no_event_count = sum(1 for r in category_results if r.get("status") == "无异动")
    coverage_line = (
        f"\n覆盖状态：成功 {categories_count}，失败 {failed_count}，"
        f"首次基线 {baseline_count}，无异动 {no_event_count}"
        if category_results else ""
    )

    # 构建链接文本（在 if not events 之前定义，以便两个分支都能使用）
    link_lines = []
    if lark_url:
        link_lines.append(f"📊 飞书多维表格：[点击查看]({lark_url})")
    if wecom_sheet_url:
        link_lines.append(f"📊 企微智能表格：[点击查看]({wecom_sheet_url})")
    link_hint = "\n".join(link_lines)

    if not events:
        # 无异动也发一条
        content = (
            f"**📊 罗盘榜单异动**\n"
            f"🕐 {ts_str}\n"
            f"共采集 {categories_count} 个二级类目，**0 条异动**"
            f"{coverage_line}"
        )
        if link_hint:
            content += f"\n\n{link_hint}"
        messages = [content]
    else:
        # 按一级类目统计
        l1_counts: dict[str, int] = {}
        for ev in events:
            l1 = ev.get("industry_name", "未知")
            l1_counts[l1] = l1_counts.get(l1, 0) + 1

        l1_lines = []
        for name, count in sorted(l1_counts.items(), key=lambda x: -x[1]):
            l1_lines.append(f"{name}: **{count}** 条")

        # 构造多条消息，避免单条超长
        header = (
            f"**📊 罗盘榜单异动**\n"
            f"🕐 {ts_str}\n"
            f"共采集 {categories_count} 个二级类目，发现 **{len(events)}** 条异动\n\n"
            f"{coverage_line.lstrip()}\n\n"
        )
        header_len = _blen(header)

        link_hint_len = _blen(link_hint) if link_hint else 0

        chunks = []
        current_chunk_lines = []
        current_chunk_len = header_len

        # 单行预算：header + link_hint 之外可容纳的最大字节数
        line_budget = _MAX_LEN - header_len - link_hint_len
        for idx, line in enumerate(l1_lines):
            line_with_sep = f"{line} | " if idx < len(l1_lines) - 1 else line
            # 单行本身超预算时按字节截断，避免最终 chunk 突破企微上限
            if _blen(line_with_sep) > line_budget:
                line_with_sep = _truncate_bytes(line_with_sep, line_budget)
            line_len = _blen(line_with_sep)

            # 如果加上这一行会超长，先保存当前块，开新块
            if current_chunk_len + line_len + link_hint_len > _MAX_LEN and current_chunk_lines:
                chunks.append("".join(current_chunk_lines).rstrip(" |"))
                current_chunk_lines = []
                current_chunk_len = header_len

            current_chunk_lines.append(line_with_sep)
            current_chunk_len += line_len

        if current_chunk_lines:
            chunks.append("".join(current_chunk_lines).rstrip(" |"))

        # 第一块加 header，最后一块加 link_hint
        messages = []
        if not chunks:
            messages.append(header + link_hint)
        else:
            for i, chunk in enumerate(chunks):
                msg = header + chunk
                if i == len(chunks) - 1:
                    msg += link_hint
                messages.append(msg)

    if not webhook_url:
        logger.warning("WECOM_WEBHOOK_URL 未配置，跳过摘要推送")
        return False

    try:
        success = False
        for content in messages:
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error("企微摘要 HTTP %d: %s", resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error("企微摘要返回错误: %s", data)
                return False
            success = True
        logger.info("企微摘要已推送 (%d 条消息)", len(messages))

        return success
    except Exception as exc:
        logger.error("企微摘要推送异常: %s", exc)
        return False
