"""
飞书集成：
  1. send_events —— 群机器人 Webhook 文本推送（存根，备用通知渠道）。
  2. sync_events_to_base —— 把每轮异动事件写入飞书多维表格（主用，数据落库）。

Base 写入通过本机已配置的 lark-cli（子进程）完成，身份默认 user（何盈快，token
自动续期，适合每小时 cron）。需要在 .env 配 LARK_BASE_APP_TOKEN / LARK_TABLE_ID。
文档：https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN
"""
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

import requests

from config import settings
from notify.templates import group_events, format_line

logger = logging.getLogger(__name__)

# 事件类型 → 飞书表「事件类型」单选选项（与表内预设选项一致）
_EVENT_LABELS = {
    "NEW_ENTRY": "新进榜",
    "ENTER_TOP10": "冲进TOP10",
    "RANK_UP_50_PLUS_WARNING": "暴升50+",
    "RANK_UP_30_50_WARNING": "急升30-50",
    "RANK_UP_20": "上升20-29",
    "RANK_UP_10": "上升10-19",
    "RANK_UP_5": "上升5-9",
}

# 飞书表字段顺序（必须与目标表字段名完全一致）
_BASE_FIELDS = [
    "商品标题", "采集轮次", "一级类目", "二级类目", "三级类目", "叶子类目",
    "当前排名", "上轮排名", "升幅", "事件类型",
]

_CST = timezone(timedelta(hours=8))
_BASE_BATCH = 200  # 飞书 record-batch-create / record-delete 单批上限

# 飞书 Base 写入模式：
#   "overwrite" —— 每轮写入前先清空整表，Base 始终只保留「最新一轮」异动（当前启用）。
#   "append"    —— 不清空，直接追加，保留全部历史轮次（旧的叠加行为）。
# 想恢复叠加：把下面改回 "append" 即可，其余代码无需改动。
_BASE_WRITE_MODE = "overwrite"


def _round_label(run_id: str) -> str:
    """run_id(ISO，UTC) → 北京时间 'YYYY-MM-DD HH:MM' 作为采集轮次标签。"""
    try:
        dt = datetime.fromisoformat(run_id)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(run_id)[:16]


def _event_to_row(ev: dict, round_label: str) -> list:
    """单条事件 → 飞书一行（顺序对齐 _BASE_FIELDS）。数值缺失写 None（空单元格）。"""
    return [
        ev.get("product_title", "") or "",
        round_label,
        ev.get("industry_name", "") or "",
        ev.get("category_name", "") or "",
        ev.get("category_l3_name", "") or "",
        ev.get("leaf_category_name", "") or "",
        ev.get("rank_current"),
        ev.get("rank_previous"),
        ev.get("rank_delta"),
        _EVENT_LABELS.get(ev.get("event_type", ""), ev.get("event_type", "")),
    ]


def _lark_batch_create(rows: list[list]) -> bool:
    """调 lark-cli 批量写一批记录到 Base。成功返回 True。"""
    payload = {"fields": _BASE_FIELDS, "rows": rows}
    tmp = None
    # lark-cli 的 --json @file 只接受「当前目录内的相对路径」，故在 cwd 建临时文件、传相对名
    cwd = os.getcwd()
    try:
        # JSON 含中文，写临时文件再用 --json @file，避免命令行传参编码/引号问题
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lark_base_", dir=cwd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        rel = os.path.basename(tmp)

        cmd = (
            f'lark-cli base +record-batch-create '
            f'--base-token {settings.LARK_BASE_APP_TOKEN} '
            f'--table-id {settings.LARK_TABLE_ID} '
            f'--json @"{rel}" --as {settings.LARK_AS}'
        )
        env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", env=env, timeout=60, cwd=cwd,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            logger.error("飞书 Base 写入无可解析响应: %s", out[:200])
            return False
        data = json.loads(m.group(0))
        if not data.get("ok"):
            logger.error("飞书 Base 写入失败: %s", json.dumps(data.get("error", {}), ensure_ascii=False)[:300])
            return False
        return True
    except Exception as exc:
        logger.error("飞书 Base 写入异常: %s", exc)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _lark_list_all_record_ids() -> list[str]:
    """列出整表全部 record_id（按 _BASE_BATCH 分页直到 has_more=false）。

    失败时返回已取到的部分（调用方据「取到数 vs 实删数」判断是否清空干净）。
    """
    ids: list[str] = []
    offset = 0
    env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}
    while True:
        cmd = (
            f'lark-cli base +record-list '
            f'--base-token {settings.LARK_BASE_APP_TOKEN} '
            f'--table-id {settings.LARK_TABLE_ID} '
            f'--limit {_BASE_BATCH} --offset {offset} '
            f'--format json --as {settings.LARK_AS}'
        )
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", env=env, timeout=60,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            m = re.search(r"\{.*\}", out, re.S)
            if not m:
                logger.error("飞书 Base 列举记录无可解析响应: %s", out[:200])
                break
            data = json.loads(m.group(0))
            if not data.get("ok"):
                logger.error("飞书 Base 列举记录失败: %s",
                             json.dumps(data.get("error", {}), ensure_ascii=False)[:300])
                break
        except Exception as exc:
            logger.error("飞书 Base 列举记录异常: %s", exc)
            break

        d = data.get("data", {}) or {}
        page_ids = d.get("record_id_list") or []
        ids.extend(page_ids)
        if not page_ids or not d.get("has_more"):
            break
        offset += _BASE_BATCH
    return ids


def _lark_delete_records(ids: list[str]) -> int:
    """按 _BASE_BATCH 分批删除给定 record_id，返回成功删除数。遇首个失败批即停。"""
    if not ids:
        return 0
    deleted = 0
    cwd = os.getcwd()
    env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}
    for i in range(0, len(ids), _BASE_BATCH):
        batch = ids[i:i + _BASE_BATCH]
        tmp = None
        try:
            # 与 create 一致：record_id 虽是 ASCII，仍走临时文件 @file，规避命令行引号问题
            fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lark_del_", dir=cwd)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"record_id_list": batch}, f, ensure_ascii=False)
            rel = os.path.basename(tmp)
            cmd = (
                f'lark-cli base +record-delete '
                f'--base-token {settings.LARK_BASE_APP_TOKEN} '
                f'--table-id {settings.LARK_TABLE_ID} '
                f'--json @"{rel}" --yes --as {settings.LARK_AS}'
            )
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", env=env, timeout=60, cwd=cwd,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            m = re.search(r"\{.*\}", out, re.S)
            if not m or not json.loads(m.group(0)).get("ok"):
                logger.error("飞书 Base 删除批次失败: %s", out[:200])
                break
            deleted += len(batch)
        except Exception as exc:
            logger.error("飞书 Base 删除记录异常: %s", exc)
            break
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return deleted


def _clear_base_table() -> bool:
    """清空飞书 Base 整表（覆盖模式写入前调用）。全部删除成功（或本就为空）返回 True。"""
    ids = _lark_list_all_record_ids()
    if not ids:
        logger.info("飞书 Base 当前无记录，无需清空")
        return True
    deleted = _lark_delete_records(ids)
    ok = deleted == len(ids)
    logger.info("飞书 Base 清空: 删除 %d/%d 条%s",
                deleted, len(ids), "" if ok else "（未全部删除）")
    return ok


def sync_events_to_base(events: list[dict], run_id: str) -> int:
    """
    把一轮全部异动事件写入飞书多维表格（按 _BASE_BATCH 分批）。

    写入模式由 _BASE_WRITE_MODE 决定：
      - "overwrite"（默认）：写入前先清空整表，Base 只保留最新一轮。
      - "append"：不清空，直接追加，保留全部历史轮次。
    返回成功写入的事件条数；遇首个失败批立即停止并返回此前已写入数（便于排障，
    不做幂等去重——每轮 run_id 唯一、事件已在 DB 层去重，正常流程不会重复写）。
    未配置 LARK_BASE_APP_TOKEN / LARK_TABLE_ID 时跳过（返回 0）。
    """
    if not events:
        return 0
    if not (settings.LARK_BASE_APP_TOKEN and settings.LARK_TABLE_ID):
        logger.info("未配置 LARK_BASE_APP_TOKEN / LARK_TABLE_ID，跳过飞书 Base 同步")
        return 0

    # 每行按事件自身 run_id 打轮次标签（events 可能跨两轮——含上一轮补发的残留），
    # 缺失时回退到本轮 run_id。
    rows = [_event_to_row(e, _round_label(e.get("run_id") or run_id)) for e in events]

    # ── 覆盖模式：写入前清空整表，使 Base 只保留最新一轮 ──────────────
    # append 模式跳过这步，下面的批量新建即为旧的「叠加追加」行为。
    if _BASE_WRITE_MODE == "overwrite":
        if not _clear_base_table():
            logger.warning("飞书 Base 清空未完全成功，仍继续写入本轮（可能与残留旧数据并存）")

    written = 0
    for i in range(0, len(rows), _BASE_BATCH):
        batch = rows[i:i + _BASE_BATCH]
        if not _lark_batch_create(batch):
            logger.warning("飞书 Base 第 %d 批写入失败，已写入 %d 条后停止", i // _BASE_BATCH + 1, written)
            break
        written += len(batch)
        logger.info("飞书 Base 同步进度: %d/%d 条", written, len(rows))

    logger.info("飞书 Base 同步完成: 写入 %d/%d 条（轮次 %s）", written, len(rows), round_label)
    return written


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
