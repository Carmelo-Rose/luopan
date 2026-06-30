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
from notify.templates import group_events, format_line, EVENT_LABELS

logger = logging.getLogger(__name__)

# 事件类型 → 飞书表「事件类型」单选选项（唯一真相源在 templates.EVENT_LABELS）
_EVENT_LABELS = EVENT_LABELS

# 飞书表字段顺序（必须与目标表字段名完全一致）
# 大盘表：跨多个一级类目，故保留「一级/二级类目」两列。
_BASE_FIELDS = [
    "商品标题", "采集轮次", "一级类目", "二级类目",
    "当前排名", "上轮排名", "升幅", "事件类型", "支付金额", "价格", "商品图",
]

# 服配表字段顺序：服配各叶子的一级/二级/三级类目恒为「服饰内衣/服装/服装配饰」，
# 三列完全重复无信息量，故全部去掉，只保留「叶子类目」区分帽子/面罩/防晒口罩等
# （叶子类目在飞书表里配成带颜色的单选标签，直观区分）。
# 注意：写入字段必须是目标表里真实存在的列，否则整表会被清空（见 HANDOFF §4.2 同类坑），
# 改这里前先确认 tbllW7yLiCQu606X 的列已同步删掉一级/二级/三级类目。
_ACC_FIELDS = [
    "商品标题", "采集轮次", "叶子类目",
    "当前排名", "上轮排名", "升幅", "事件类型", "支付金额", "价格", "商品图",
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


def _event_value_map(ev: dict, round_label: str) -> dict:
    """单条事件 → {飞书字段名: 值} 全量映射。各字段列表据此按需取值、排序。

    数值缺失写 None（空单元格）。商品图字段为超链接(text/url)类型，直接写 URL 字符串，
    lark-cli 会渲染成可点击链接（[url](url)）；空串=空单元格。
    """
    return {
        "商品标题": ev.get("product_title", "") or "",
        "采集轮次": round_label,
        "一级类目": ev.get("industry_name", "") or "",
        "二级类目": ev.get("category_name", "") or "",
        "三级类目": ev.get("category_l3_name", "") or "",
        "叶子类目": ev.get("leaf_category_name", "") or "",
        "当前排名": ev.get("rank_current"),
        "上轮排名": ev.get("rank_previous"),
        "升幅": ev.get("rank_delta"),
        "事件类型": _EVENT_LABELS.get(ev.get("event_type", ""), ev.get("event_type", "")),
        "支付金额": ev.get("pay_amount", "") or "",
        "价格": ev.get("price", "") or "",
        "商品图": ev.get("image", "") or "",
    }


def _event_to_row(ev: dict, round_label: str, fields: list) -> list:
    """单条事件 → 飞书一行，顺序严格对齐传入的 fields。"""
    vm = _event_value_map(ev, round_label)
    return [vm.get(f) for f in fields]


def _lark_batch_create(rows: list[list], table_id: str = "", fields: list | None = None) -> list[str] | None:
    """调 lark-cli 批量写一批记录到 Base。成功返回本批新建的 record_id 列表，失败返回 None。

    返回 record_id 是为了「先写后删」覆盖模式在写入失败时能回滚本轮已写入的记录
    （见 sync_events_to_base）。成功但响应未带 id 时返回空列表（仍视为成功）。
    fields 默认 _BASE_FIELDS（大盘）；服配传 _ACC_FIELDS。
    """
    tid = table_id or settings.LARK_TABLE_ID
    payload = {"fields": fields or _BASE_FIELDS, "rows": rows}
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
            f'--table-id {tid} '
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
            return None
        data = json.loads(m.group(0))
        if not data.get("ok"):
            logger.error("飞书 Base 写入失败: %s", json.dumps(data.get("error", {}), ensure_ascii=False)[:300])
            return None
        d = data.get("data", {}) or {}
        return d.get("record_id_list") or []
    except Exception as exc:
        logger.error("飞书 Base 写入异常: %s", exc)
        return None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _lark_list_all_record_ids(table_id: str = "") -> list[str]:
    """列出整表全部 record_id（按 _BASE_BATCH 分页直到 has_more=false）。

    失败时返回已取到的部分（调用方据「取到数 vs 实删数」判断是否清空干净）。
    """
    tid = table_id or settings.LARK_TABLE_ID
    ids: list[str] = []
    offset = 0
    env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}
    while True:
        cmd = (
            f'lark-cli base +record-list '
            f'--base-token {settings.LARK_BASE_APP_TOKEN} '
            f'--table-id {tid} '
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


def _lark_delete_records(ids: list[str], table_id: str = "") -> int:
    """按 _BASE_BATCH 分批删除给定 record_id，返回成功删除数。遇首个失败批即停。"""
    if not ids:
        return 0
    tid = table_id or settings.LARK_TABLE_ID
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
                f'--table-id {tid} '
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

# 注：旧的 _clear_base_table（先清空再写）已删除——覆盖模式改为「先写后删」原子交换，
# 见 sync_events_to_base。清旧由那里直接调 _lark_delete_records(old_ids) 完成。


def sync_events_to_base(
    events: list[dict], run_id: str, table_id: str = "", include_leaf: bool = False,
) -> int:
    """
    把一轮全部异动事件写入飞书多维表格（按 _BASE_BATCH 分批）。

    写入模式由 _BASE_WRITE_MODE 决定：
      - "overwrite"（默认）：写入前先清空整表，Base 只保留最新一轮。
      - "append"：不清空，直接追加，保留全部历史轮次。
    返回成功写入的事件条数；遇首个失败批立即停止并返回此前已写入数（便于排障，
    不做幂等去重——每轮 run_id 唯一、事件已在 DB 层去重，正常流程不会重复写）。
    未配置 LARK_BASE_APP_TOKEN / table_id 时跳过（返回 0）。

    Parameters
    ----------
    table_id : str
        目标飞书表 id，默认使用 settings.LARK_TABLE_ID（大盘表）。
        服配支线传 settings.LARK_ACC_TABLE_ID。
    include_leaf : bool
        True 时使用服配字段集 _ACC_FIELDS（无一级/二级/三级类目，仅「叶子类目」单选标签列）。
        大盘表必须 False（使用 _BASE_FIELDS，含一级/二级类目）。
    """
    if not events:
        return 0
    tid = table_id or settings.LARK_TABLE_ID
    if not (settings.LARK_BASE_APP_TOKEN and tid):
        logger.info("未配置 LARK_BASE_APP_TOKEN / table_id，跳过飞书 Base 同步")
        return 0

    fields = _ACC_FIELDS if include_leaf else _BASE_FIELDS
    # 每行按事件自身 run_id 打轮次标签（events 可能跨两轮——含上一轮补发的残留），
    # 缺失时回退到本轮 run_id。
    rows = [
        _event_to_row(e, _round_label(e.get("run_id") or run_id), fields)
        for e in events
    ]

    # ── 覆盖模式：先写新，全部成功后再删旧（原子交换），避免写入失败把整表清空 ──
    # 旧实现是「先清空再写」：一旦某批写入失败（如单选 not_found），整表已被清空、
    # 新数据又没写进去 → 表变空（2026-06-23 大盘事故）。改为：
    #   1) 先快照现有 record_id；2) 写入本轮新行；3) 仅当全部写成功才删旧快照；
    #   4) 写入中途失败则回滚本轮已写入的新行、保留旧数据（表至少还是上一轮，不会空）。
    # append 模式不快照、不删旧，下面的批量新建即旧的「叠加追加」行为。
    old_ids = _lark_list_all_record_ids(tid) if _BASE_WRITE_MODE == "overwrite" else []

    written = 0
    new_ids: list[str] = []
    write_failed = False
    for i in range(0, len(rows), _BASE_BATCH):
        batch = rows[i:i + _BASE_BATCH]
        ids = _lark_batch_create(batch, tid, fields=fields)
        if ids is None:
            logger.warning("飞书 Base 第 %d 批写入失败，已写入 %d 条", i // _BASE_BATCH + 1, written)
            write_failed = True
            break
        new_ids.extend(ids)
        written += len(batch)
        logger.info("飞书 Base 同步进度: %d/%d 条", written, len(rows))

    if _BASE_WRITE_MODE == "overwrite":
        if not write_failed and written == len(rows):
            # 成功：删除旧快照，完成交换（表只剩本轮）
            if old_ids:
                deleted = _lark_delete_records(old_ids, tid)
                logger.info("飞书 Base 覆盖交换: 删除上一轮 %d/%d 条", deleted, len(old_ids))
        else:
            # 失败：回滚本轮已写入的新行，保留旧数据，表不会变空
            if new_ids:
                rolled = _lark_delete_records(new_ids, tid)
                logger.error(
                    "飞书 Base 写入失败，已回滚本轮新写入 %d/%d 条，保留上一轮 %d 条（表未清空）",
                    rolled, len(new_ids), len(old_ids),
                )
            return 0

    logger.info("飞书 Base 同步完成: 写入 %d/%d 条（轮次 %s）", written, len(rows), _round_label(run_id))
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
