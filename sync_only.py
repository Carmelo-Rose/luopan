"""
独立脚本：只从 DB 读取最新异动事件，写入企微智能表格。
不采集、不差分、不推企微 Webhook。

用法：
    python sync_only.py                   # 写入最新一轮事件
    python sync_only.py --last 3          # 写入最近3轮事件
    python sync_only.py --run-id <id>     # 写入指定轮次
"""
import argparse
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import settings
from db import database
from notify.wecom_smartsheet import sync_to_smartsheet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sync_only")


def main():
    parser = argparse.ArgumentParser(description="企微智能表格独立同步脚本")
    parser.add_argument("--last", type=int, default=1, help="同步最近N轮事件（默认1）")
    parser.add_argument("--run-id", help="指定同步某个 run_id")
    args = parser.parse_args()

    database.init_db(settings.DB_PATH)
    conn = database.get_connection(settings.DB_PATH)

    try:
        if args.run_id:
            target_runs = [args.run_id]
        else:
            target_runs = database.get_latest_run_ids(conn, args.last)

        if not target_runs:
            logger.error("数据库中没有任何轮次记录，请先运行采集")
            return

        logger.info("目标轮次: %s", target_runs)

        # 获取待推送事件（notified=0），限定在目标轮次内
        all_pending = database.get_pending_events(conn)
        events = [e for e in all_pending if e.get("run_id") in target_runs]

        if not events:
            # 如果没有 pending 事件，尝试取目标轮次的全部事件（含已通知的）
            logger.info("无待推送事件，尝试读取目标轮次全部事件...")
            events = _get_events_by_run_ids(conn, target_runs)

        if not events:
            logger.warning("目标轮次无任何事件，请确认 DB 中有数据")
            return

        logger.info("读取到 %d 条事件", len(events))

        written = sync_to_smartsheet(events, target_runs[0])
        logger.info("完成: 写入 %d / %d 条", written, len(events))

    finally:
        conn.close()


def _get_events_by_run_ids(conn, run_ids: list[str]) -> list[dict]:
    """按 run_id 列表查询全部事件（含已通知的）。"""
    if not run_ids:
        return []
    placeholders = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT * FROM ranking_event WHERE run_id IN ({placeholders}) "
        "ORDER BY created_at DESC",
        run_ids,
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    main()
