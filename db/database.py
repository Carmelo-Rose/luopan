"""
SQLite 数据访问层。
所有 SQL 操作在此文件中集中管理，上层模块通过函数调用而非直接操作 DB。
"""
import sqlite3
import os
from pathlib import Path
from typing import Optional

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    """返回带 row_factory 的连接，调用方负责关闭。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 新增列迁移：(表名, 列名)。schema.sql 用 CREATE TABLE IF NOT EXISTS，
# 不会给已存在的旧库补列，故对已有 DB 用 ALTER TABLE 幂等补齐。
# 含 industry_name/category_name：早期 schema 无此列，老库需一并补齐。
_COLUMN_MIGRATIONS = [
    ("products_snapshot", "industry_name"),
    ("products_snapshot", "category_name"),
    ("products_snapshot", "category_l3_name"),
    ("products_snapshot", "leaf_category_name"),
    ("ranking_event", "industry_name"),
    ("ranking_event", "category_name"),
    ("ranking_event", "category_l3_name"),
    ("ranking_event", "leaf_category_name"),
]


def init_db(db_path: str) -> None:
    """建表（幂等）+ 对旧库补齐新增列。"""
    conn = get_connection(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, col in _COLUMN_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在（新库由 schema.sql 建好），忽略
        conn.commit()
    finally:
        conn.close()


# ── 快照写入 ──────────────────────────────────────────────────────────

def insert_snapshot(conn: sqlite3.Connection, run_id: str, rows: list[dict]) -> None:
    """批量插入一轮快照，已存在则忽略（IGNORE）。"""
    sql = """
        INSERT OR IGNORE INTO products_snapshot
            (run_id, scope_key, rank, product_id, product_title, product_url,
             price_range, pay_amount, clicks, conversion_rate,
             card_order_count, captured_at, industry_name, category_name,
             category_l3_name, leaf_category_name)
        VALUES
            (:run_id, :scope_key, :rank, :product_id, :product_title, :product_url,
             :price_range, :pay_amount, :clicks, :conversion_rate,
             :card_order_count, :captured_at,
             COALESCE(:industry_name, ''), COALESCE(:category_name, ''),
             COALESCE(:category_l3_name, ''), COALESCE(:leaf_category_name, ''))
    """
    # 防御性默认：旧调用方/测试可能不带新字段，统一补空避免绑定缺参
    data = [
        {"category_l3_name": "", "leaf_category_name": "", **r, "run_id": run_id}
        for r in rows
    ]
    conn.executemany(sql, data)
    conn.commit()


# ── 快照查询 ──────────────────────────────────────────────────────────

def get_latest_two_run_ids(
    conn: sqlite3.Connection, scope_key: str
) -> tuple[Optional[str], Optional[str]]:
    """
    返回 (latest_run_id, previous_run_id)。
    latest 是最新一轮（刚写入），previous 是上一轮。
    若不足两轮，对应位置返回 None。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT run_id FROM products_snapshot
        WHERE scope_key = ?
        ORDER BY run_id DESC
        LIMIT 2
        """,
        (scope_key,),
    ).fetchall()
    run_ids = [r["run_id"] for r in rows]
    latest = run_ids[0] if len(run_ids) >= 1 else None
    previous = run_ids[1] if len(run_ids) >= 2 else None
    return latest, previous


def get_snapshot(
    conn: sqlite3.Connection, run_id: str, scope_key: str
) -> list[dict]:
    """返回某轮快照，按 rank 升序，list of dict。"""
    rows = conn.execute(
        """
        SELECT * FROM products_snapshot
        WHERE run_id = ? AND scope_key = ?
        ORDER BY rank ASC
        """,
        (run_id, scope_key),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_run_ids(conn: sqlite3.Connection, scope_key: str) -> list[str]:
    """返回该 scope_key 下所有 run_id，降序。"""
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM products_snapshot WHERE scope_key=? ORDER BY run_id DESC",
        (scope_key,),
    ).fetchall()
    return [r["run_id"] for r in rows]


# ── 事件写入 ──────────────────────────────────────────────────────────

def insert_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    """批量写入事件，ON CONFLICT IGNORE 实现去重。返回实际写入条数。

    使用 conn.total_changes 差值统计：total_changes 是连接生命周期内累计
    被修改的行数，executemany 多条 INSERT OR IGNORE 会逐条累加，因此差值
    即本次真正写入（未被 IGNORE）的条数。注意不能用 SELECT changes()，
    它只反映最近一条语句的影响行数（且 SELECT 本身会重置语义）。
    """
    if not events:
        return 0
    sql = """
        INSERT OR IGNORE INTO ranking_event
            (run_id, scope_key, event_type, product_id, product_title, product_url,
             rank_current, rank_previous, rank_delta, created_at, notified,
             industry_name, category_name, category_l3_name, leaf_category_name)
        VALUES
            (:run_id, :scope_key, :event_type, :product_id, :product_title, :product_url,
             :rank_current, :rank_previous, :rank_delta, :created_at, 0,
             COALESCE(:industry_name, ''), COALESCE(:category_name, ''),
             COALESCE(:category_l3_name, ''), COALESCE(:leaf_category_name, ''))
    """
    # 防御性默认：旧调用方/测试可能不带新字段，统一补空避免绑定缺参
    data = [
        {"category_l3_name": "", "leaf_category_name": "", **e}
        for e in events
    ]
    before = conn.total_changes
    conn.executemany(sql, data)
    conn.commit()
    return conn.total_changes - before


def get_pending_events(conn: sqlite3.Connection) -> list[dict]:
    """返回所有未推送事件。"""
    rows = conn.execute(
        "SELECT * FROM ranking_event WHERE notified=0 ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_events_notified(conn: sqlite3.Connection, event_ids: list[int]) -> None:
    """标记事件为已推送。"""
    if not event_ids:
        return
    placeholders = ",".join("?" * len(event_ids))
    conn.execute(
        f"UPDATE ranking_event SET notified=1 WHERE id IN ({placeholders})",
        event_ids,
    )
    conn.commit()
