"""Supplement one main-lane category into the latest video_order run.

This is an operational repair tool for cases where a full --multi run skipped
one category after collecting the rest of the run successfully. It collects only
the requested L2 category, writes its snapshot/events into the latest main run,
and rewrites the main Lark Base table for that same run. It never pushes WeCom.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.douyin_compass import DouyinCompassCollector
from config import settings
from db import database
from main import _business_now, _process_category, _save_summary_sidecar
from notify.lark import sync_events_to_base


SCOPE_PREFIX = "video_order"


def _find_category(category_id: str) -> dict:
    tree = json.loads(Path(settings.CATEGORY_TREE_CACHE).read_text(encoding="utf-8"))
    for industry_name, items in tree.items():
        for item in items:
            if str(item.get("category_id", item.get("id", ""))) == category_id:
                return {
                    "industry_name": industry_name,
                    "category_name": item["name"],
                    "industry_id": str(item["industry_id"]),
                    "category_id": str(item.get("category_id", item.get("id", ""))),
                }
    raise SystemExit(f"category_id not found in cache: {category_id}")


async def _collect_category(cat: dict, scope_key: str) -> list[dict]:
    async with DouyinCompassCollector() as collector:
        return await collector.collect(
            scope_key=scope_key,
            industry_id=cat["industry_id"],
            category_id=cat["category_id"],
            industry_name=cat["industry_name"],
            category_name=cat["category_name"],
            min_products=160,
        )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category-id", required=True)
    parser.add_argument("--run-id", default="", help="Defaults to latest video_order snapshot run")
    parser.add_argument("--min-products", type=int, default=160)
    args = parser.parse_args()

    database.init_db(settings.DB_PATH)
    conn = database.get_connection(settings.DB_PATH)
    try:
        run_id = args.run_id
        if not run_id:
            row = conn.execute(
                "select max(run_id) from products_snapshot where scope_key like 'video_order_%'"
            ).fetchone()
            run_id = row[0] if row else ""
        if not run_id:
            raise SystemExit("no video_order run found")

        cat = _find_category(args.category_id)
        scope_key = f"{SCOPE_PREFIX}_{cat['industry_name']}_{cat['category_name']}"
        existing = conn.execute(
            "select count(*) from products_snapshot where run_id=? and scope_key=?",
            (run_id, scope_key),
        ).fetchone()[0]

        print(f"target_run={run_id}")
        print(f"target_scope={scope_key}")
        print(f"existing_snapshot_rows={existing}")

        if existing == 0:
            products = await _collect_category(cat, scope_key)
            print(f"supplement_products={len(products)}")
            if len(products) < args.min_products:
                raise SystemExit(2)
            events, total, status = _process_category(
                conn, run_id, scope_key, products, dry_run=False,
            )
            print(f"supplement_events={len(events)}")
            print(f"supplement_status={status}")
        else:
            print("supplement_skipped=already_present")

        to_sync = [
            e for e in database.get_pending_events(conn)
            if e.get("run_id") == run_id
            and (e.get("scope_key") or "").startswith(SCOPE_PREFIX)
        ]
        print(f"sync_events={len(to_sync)}")
        written = sync_events_to_base(to_sync, run_id)
        print(f"sync_written={written}")

        snap_scopes, snap_rows = conn.execute(
            """
            select count(distinct scope_key), count(*)
            from products_snapshot
            where run_id=? and scope_key like 'video_order_%'
            """,
            (run_id,),
        ).fetchone()
        ev_total, pending, ev_scopes = conn.execute(
            """
            select count(*), sum(case when notified=0 then 1 else 0 end), count(distinct scope_key)
            from ranking_event
            where run_id=? and scope_key like 'video_order_%'
            """,
            (run_id,),
        ).fetchone()
        print(f"snapshot_scopes={snap_scopes}")
        print(f"snapshot_rows={snap_rows}")
        print(f"event_total={ev_total}")
        print(f"event_pending={pending}")
        print(f"event_scopes={ev_scopes}")

        _save_summary_sidecar(
            "multi", run_id, _business_now(), ev_scopes,
            [{"scope_key": "manual_supplement", "products": snap_rows, "events": ev_total}],
            scope_prefix=SCOPE_PREFIX, new_event_count=ev_total,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
