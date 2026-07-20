"""
SQLite 数据访问层。
所有 SQL 操作在此文件中集中管理，上层模块通过函数调用而非直接操作 DB。
"""
import sqlite3
import os
import json
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
    ("products_snapshot", "image"),
    ("products_snapshot", "shop_info"),
    ("ranking_event", "industry_name"),
    ("ranking_event", "category_name"),
    ("ranking_event", "category_l3_name"),
    ("ranking_event", "leaf_category_name"),
    ("ranking_event", "image"),
    ("ranking_event", "pay_amount"),
    ("ranking_event", "price"),
    ("ranking_event", "shop_info"),
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
             image, shop_info, price_range, pay_amount, clicks, conversion_rate,
             card_order_count, captured_at, industry_name, category_name,
             category_l3_name, leaf_category_name)
        VALUES
            (:run_id, :scope_key, :rank, :product_id, :product_title, :product_url,
             :image, :shop_info, :price_range, :pay_amount, :clicks, :conversion_rate,
             :card_order_count, :captured_at,
             COALESCE(:industry_name, ''), COALESCE(:category_name, ''),
             COALESCE(:category_l3_name, ''), COALESCE(:leaf_category_name, ''))
    """
    # 防御性默认：旧调用方/测试可能不带新字段，统一补空避免绑定缺参
    data = [
        {"category_l3_name": "", "leaf_category_name": "", "image": "", "shop_info": "", **r, "run_id": run_id}
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


def get_latest_run_ids(conn: sqlite3.Connection, limit: int = 2) -> list[str]:
    """返回全局（跨 scope）最近 limit 个 run_id，降序。

    用于「中断恢复」：把待推送范围限定在最近一两轮，这样上一轮被硬杀
    （未走到推送/同步）的事件能在本轮补发，又不会把更早的历史残留一起翻出来。
    """
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM products_snapshot ORDER BY run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["run_id"] for r in rows]


def get_latest_run_ids_for_prefix(
    conn: sqlite3.Connection, scope_prefix: str, limit: int = 2
) -> list[str]:
    """返回某 scope_key 前缀下最近 limit 个 run_id，降序。

    与 get_latest_run_ids（全局）不同：当多条独立管线（如大盘 video_order_* 与
    服配 video_acc_*）各自排程、run_id 交错写入同一张快照表时，全局「最近两轮」
    会被另一条管线的轮次挤占，导致本管线上一轮未送达的事件掉出补发窗口而永久滞留。
    本函数按前缀隔离窗口，使每条管线的中断补发只看自己的最近两轮。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT run_id FROM products_snapshot
        WHERE scope_key LIKE ?
        ORDER BY run_id DESC LIMIT ?
        """,
        (scope_prefix + "%", limit),
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
             rank_current, rank_previous, rank_delta, image, shop_info, pay_amount, price,
             created_at, notified,
             industry_name, category_name, category_l3_name, leaf_category_name)
        VALUES
            (:run_id, :scope_key, :event_type, :product_id, :product_title, :product_url,
             :rank_current, :rank_previous, :rank_delta, :image, :shop_info, :pay_amount, :price,
             :created_at, 0,
             COALESCE(:industry_name, ''), COALESCE(:category_name, ''),
             COALESCE(:category_l3_name, ''), COALESCE(:leaf_category_name, ''))
    """
    # 防御性默认：旧调用方/测试可能不带新字段，统一补空避免绑定缺参
    data = [
        {"category_l3_name": "", "leaf_category_name": "",
         "image": "", "shop_info": "", "pay_amount": "", "price": "", **e}
        for e in events
    ]
    before = conn.total_changes
    conn.executemany(sql, data)
    conn.commit()
    return conn.total_changes - before


def update_event_prices(
    conn: sqlite3.Connection, run_id: str, price_map: dict[str, str]
) -> int:
    """把详情页抓到的真实价格回填到本轮已落库事件。返回更新行数。

    price_map: {product_id: "¥59.9起"}。只更新本 run_id 的事件，抓不到的商品不动
    （保留 insert 时写入的 price_bin 回退值）。
    """
    if not price_map:
        return 0
    updated = 0
    for pid, price in price_map.items():
        if not price:
            continue
        cur = conn.execute(
            "UPDATE ranking_event SET price=? WHERE run_id=? AND product_id=?",
            (price, run_id, pid),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


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


# ── 详情页富化（属性 / 详情图 / 主图集）───────────────────────────────

def upsert_enrichment(
    conn: sqlite3.Connection,
    product_id: str,
    attrs: dict | None = None,
    detail_images: list | None = None,
    main_images: list | None = None,
    source_url: str = "",
    updated_at: str = "",
) -> None:
    """写入/合并一个商品的详情页富化结果（product_id 为主键）。

    attrs/detail_images/main_images 统一以 JSON 文本落库；传 None/空 表示本次没
    抓到该项——**逐列合并，不整行覆盖**：某一列这次没抓到就保留上次落库的旧值，
    不会因为"这次只抓到属性、没抓到详情图"就把上次成功抓到的详情图冲掉（反之
    亦然）。首次插入（无旧值可保留）时空列落 ''，与建表默认一致，查询侧按空处理。
    updated_at 由调用方传 ISO 时间串（本层不取系统时间，保持可测/可复现）。
    """
    attrs_json = json.dumps(attrs, ensure_ascii=False) if attrs else ""
    detail_json = json.dumps(detail_images, ensure_ascii=False) if detail_images else ""
    main_json = json.dumps(main_images, ensure_ascii=False) if main_images else ""
    conn.execute(
        """
        INSERT INTO product_enrichment
            (product_id, attrs_json, detail_images_json, main_images_json,
             source_url, updated_at)
        VALUES (:pid, :attrs, :detail, :main, :src, :ts)
        ON CONFLICT(product_id) DO UPDATE SET
            attrs_json         = CASE WHEN excluded.attrs_json != '' THEN excluded.attrs_json ELSE product_enrichment.attrs_json END,
            detail_images_json = CASE WHEN excluded.detail_images_json != '' THEN excluded.detail_images_json ELSE product_enrichment.detail_images_json END,
            main_images_json   = CASE WHEN excluded.main_images_json != '' THEN excluded.main_images_json ELSE product_enrichment.main_images_json END,
            source_url         = excluded.source_url,
            updated_at         = excluded.updated_at
        """,
        {"pid": product_id, "attrs": attrs_json, "detail": detail_json,
         "main": main_json, "src": source_url, "ts": updated_at},
    )
    conn.commit()


def get_enrichment(conn: sqlite3.Connection, product_id: str) -> Optional[dict]:
    """按 product_id 取富化结果，解析 JSON 后返回；无记录返回 None。

    返回 {"product_id","attributes"(dict),"detail_images"(list),
    "main_images"(list),"source_url","updated_at"}。JSON 解析失败的字段退回空容器，
    不抛异常——富化数据脏不该拖垮查询。"""
    row = conn.execute(
        "SELECT * FROM product_enrichment WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if row is None:
        return None

    def _loads(s, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default

    return {
        "product_id": row["product_id"],
        "attributes": _loads(row["attrs_json"], {}),
        "detail_images": _loads(row["detail_images_json"], []),
        "main_images": _loads(row["main_images_json"], []),
        "source_url": row["source_url"],
        "updated_at": row["updated_at"],
    }
