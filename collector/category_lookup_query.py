"""
category_lookup_query.py — 部署在 AILAB 服务机 luopan 项目里，就地查询采集库
(data/compass.db) 里某个商品的真实抖音类目全路径，供 auto_pdd 项目通过 SSH 远程调用。
放在 collector/ 下跟 category_discovery.py 归一类；data/ 整体在 .gitignore 里，
放那边进不了仓库。

匹配策略（优先级从高到低，命中即停）：
    1. 商品主图 URL 归一化后做子串匹配（去掉 CDN 域名前缀差异，
       只比较 ecom-shop-material/ 之后的文件名部分，这部分是稳定的）。
    2. 标题精确相等匹配。
不做模糊/相似度匹配 —— 宁可不命中回退 AI 猜测，也不要命中了却是别的商品。

用法（本地即可测试）：
    python category_lookup_query.py --title "商品标题" --image "https://..."
    无命中输出 "null"，命中输出一行 JSON。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "compass.db"

_COLUMNS = (
    "product_id, product_title, industry_name, category_name, "
    "category_l3_name, leaf_category_name, run_id"
)


def normalize_image_key(url: str) -> str:
    """CDN 域名前缀 (p3-aio/p9-aio/...) 不稳定，取 ecom-shop-material/ 之后的
    文件名部分（含 hash+尺寸后缀）作为跨域名稳定的匹配 key。"""
    if not url:
        return ""
    m = re.search(r"ecom-shop-material/([^?\s]+)", url)
    if m:
        return m.group(1)
    return url.rstrip("/").rsplit("/", 1)[-1]


def lookup(title: str, image_url: str) -> dict | None:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = None
    match_by = None

    img_key = normalize_image_key(image_url)
    if img_key:
        row = cur.execute(
            f"SELECT {_COLUMNS} FROM products_snapshot "
            f"WHERE image LIKE ? ORDER BY run_id DESC LIMIT 1",
            (f"%{img_key}%",),
        ).fetchone()
        if row:
            match_by = "image"

    if row is None and title:
        row = cur.execute(
            f"SELECT {_COLUMNS} FROM products_snapshot "
            f"WHERE product_title = ? ORDER BY run_id DESC LIMIT 1",
            (title,),
        ).fetchone()
        if row:
            match_by = "title"

    conn.close()
    if row is None:
        return None
    return {
        "product_id": row["product_id"],
        "matched_title": row["product_title"],
        "l1": row["industry_name"],
        "l2": row["category_name"],
        "l3": row["category_l3_name"],
        "leaf": row["leaf_category_name"],
        "run_id": row["run_id"],
        "match_by": match_by,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="")
    ap.add_argument("--image", default="")
    args = ap.parse_args()
    result = lookup(args.title, args.image)
    # ensure_ascii=True（默认）：非 ASCII 一律转义成 \uXXXX，避免 SSH 管道两端
    # 控制台代码页（GBK/UTF-8）不一致导致中文乱码甚至解析失败。
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
