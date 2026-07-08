"""
抖音罗盘商品榜 TOP200 监控系统 — 主入口。

两种运行模式：
  - 单类目模式（默认）：采集 .env 中配置的单个类目
  - 多类目模式（--multi）：自动发现目标一级类目下所有二级类目，逐个采集

流程：
    采集 → 写快照 → 差分（第一轮跳过）→ 写事件 → Excel 报告 → 企微摘要推送

用法示例：
    python main.py                          # 单类目模式
    python main.py --multi                  # 多类目模式
    python main.py --multi --dry-run        # 多类目，不推送
    python main.py --mock                   # 用 mock 数据（调试用）
    python main.py --list-runs              # 查看历史 run_id
    python main.py --login                  # 打开浏览器手动登录
    python main.py --discover               # 仅发现并打印类目树（不采集）
"""
import argparse
import asyncio
import json
import logging
import os
import sys

# 确保项目根目录在 sys.path 中，避免从外部执行时找不到模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from datetime import datetime, timezone, timedelta

from config import settings
from db import database
from monitor.diff import compute_diff
from notify import dispatcher
from notify.excel import generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

_BUSINESS_TZ = timezone(timedelta(hours=8))


def _business_now() -> datetime:
    """Return the business timestamp used in reports and push summaries."""
    return datetime.now(_BUSINESS_TZ)


# ── Mock 数据 ──────────────────────────────────────────────────────────

def _generate_mock_products(
    scope_key: str, seed_shift: int = 0,
    industry_name: str = "", category_name: str = "",
) -> list[dict]:
    """生成 200 条 mock 商品，seed_shift 用于模拟排名变化。"""
    captured_at = datetime.now(timezone.utc).isoformat()
    products = []
    for i in range(1, 201):
        shifted_rank = max(1, i - seed_shift) if seed_shift > 0 else i
        products.append({
            "rank": i,
            "product_id": f"mock_pid_{shifted_rank:04d}",
            "product_title": f"Mock商品 #{shifted_rank:04d}",
            "product_url": f"https://example.com/product/{shifted_rank}",
            "price_range": "¥100-200",
            "pay_amount": str(10000 - i * 10),
            "clicks": str(50000 - i * 100),
            "conversion_rate": f"{20 - i * 0.05:.1f}%",
            "card_order_count": str(5000 - i * 20),
            "captured_at": captured_at,
            "scope_key": scope_key,
            "industry_name": industry_name,
            "category_name": category_name,
            "leaf_category_id": "",
            "category_l3_name": "Mock三级",
            "leaf_category_name": "Mock叶子",
        })
    if seed_shift > 0:
        for j in range(3):
            products[j]["product_id"] = f"mock_new_{j:04d}"
            products[j]["product_title"] = f"新进榜商品 #{j}"
    return products


def _enrich_event_prices(events: list[dict]) -> None:
    """对事件商品逐个打开详情页抓真实价格，就地回填到事件 dict 的 price 字段。

    抓价独立开一个浏览器会话（在主采集会话关闭后调用，避免 profile 冲突）；整体失败
    不抛出，事件保留 compute_diff 写入的 price_bin 回退值。
    """
    try:
        from collector.product_price import fetch_event_prices_sync
        price_map = fetch_event_prices_sync(events)
    except Exception as exc:
        logger.warning("详情页拓价整体失败，保留价格带回退值: %s", exc)
        return
    for e in events:
        p = price_map.get(e.get("product_id"))
        if p:
            e["price"] = p


# ── 单类目流程（向后兼容）─────────────────────────────────────────────

async def run_once(
    scope_key: str,
    dry_run: bool = False,
    mock: bool = False,
) -> dict:
    """
    执行一次完整监控流程（单类目）。

    Returns
    -------
    dict  {"run_id", "total_products", "events", "is_baseline"}
    """
    run_id = datetime.now(timezone.utc).isoformat()
    logger.info("=== 本轮 run_id: %s  scope_key: %s ===", run_id, scope_key)

    db_path = settings.DB_PATH
    database.init_db(db_path)
    conn = database.get_connection(db_path)

    try:
        if mock:
            logger.info("使用 Mock 数据")
            existing_runs = database.get_all_run_ids(conn, scope_key)
            shift = len(existing_runs) * 30
            products = _generate_mock_products(scope_key, seed_shift=shift)
        else:
            from collector.douyin_compass import DouyinCompassCollector
            logger.info("启动 Playwright 采集...")
            async with DouyinCompassCollector() as collector:
                products = await collector.collect(scope_key=scope_key)

        total = len(products)
        logger.info("采集完成，共 %d 条商品", total)

        if total < settings.MIN_PRODUCTS:
            logger.error(
                "采集条数 %d 低于下限 %d，判为本轮失败",
                total, settings.MIN_PRODUCTS,
            )
            return {"run_id": run_id, "total_products": total, "events": [], "is_baseline": False}

        database.insert_snapshot(conn, run_id, products)
        logger.info("快照已写入数据库")

        latest_run, previous_run = database.get_latest_two_run_ids(conn, scope_key)

        is_baseline = previous_run is None
        if is_baseline:
            logger.info("首次运行，仅建立 baseline，不执行差分与推送")
            return {
                "run_id": run_id,
                "total_products": total,
                "events": [],
                "is_baseline": True,
            }

        logger.info("与上轮 %s 执行差分...", previous_run)
        current_snapshot = database.get_snapshot(conn, latest_run, scope_key)
        previous_snapshot = database.get_snapshot(conn, previous_run, scope_key)
        events = compute_diff(run_id, scope_key, current_snapshot, previous_snapshot)
        logger.info("差分完成，发现 %d 条事件", len(events))

        # 详情页拓价已停用（2026-06-23）：价格列用脱敏价格带（price_bin）。
        # 风控+大量事件下逐条开详情页会空转数小时且拿不到价。如需恢复到手价（需先
        # 解决详情页风控），取消下面两行注释即可（_enrich_event_prices/product_price.py 保留）。
        # if events and not mock:
        #     _enrich_event_prices(events)

        database.insert_events(conn, events)

        event_summary = {}
        for e in events:
            event_summary[e["event_type"]] = event_summary.get(e["event_type"], 0) + 1
        logger.info("事件分布: %s", event_summary)

        if not dry_run:
            _dispatch_events(conn, events, scope_key)
        else:
            logger.info("[DRY-RUN] 跳过推送，以下事件将被触发：")
            for e in events:
                logger.info("  [%s] rank=%s product=%s", e["event_type"], e["rank_current"], e["product_title"])

        return {
            "run_id": run_id,
            "total_products": total,
            "events": events,
            "is_baseline": False,
        }

    finally:
        conn.close()


# ── 多类目流程 ────────────────────────────────────────────────────────

async def run_multi(
    dry_run: bool = False,
    mock: bool = False,
    scope_prefix: str = "video_order",
    do_collect: bool = True,
    do_push: bool = True,
) -> dict:
    """
    多类目模式：自动发现目标一级类目下所有二级类目，逐个采集并生成报告。

    Returns
    -------
    dict  {"run_id", "categories_collected", "total_products", "all_events", "excel_path"}
    """
    run_id = datetime.now(timezone.utc).isoformat()
    ts = _business_now()
    logger.info("═══ 多类目模式 run_id: %s ═══", run_id)
    logger.info(
        "目标一级类目: %s",
        "账号可见的全部一级类目" if settings.TARGET_ALL_L1_CATEGORIES
        else settings.TARGET_L1_CATEGORIES,
    )

    db_path = settings.DB_PATH
    database.init_db(db_path)
    conn = database.get_connection(db_path)

    all_events: list[dict] = []
    total_products = 0
    categories_collected = 0
    baseline_count = 0
    excel_path = ""
    category_results: list[dict] = []

    try:
        if not do_collect:
            # flush 模式：不采集，仅把 DB 中待送达事件推出去（读 sidecar 还原摘要上下文）
            sc = _load_summary_sidecar("multi") or {}
            f_run_id = sc.get("run_id", run_id)
            f_ts = sc.get("ts", ts)
            f_cat = sc.get("categories_collected", 0)
            f_cr = sc.get("category_results", [])
            if not dry_run:
                excel_path = _finalize_multi_push(conn, f_run_id, f_ts, f_cat, f_cr)
            logger.info("═══ flush 推送完成（多类目）═══")
            return {
                "run_id": f_run_id, "categories_collected": f_cat,
                "total_products": 0, "all_events": [], "excel_path": excel_path,
            }

        if mock:
            # Mock 模式：为每个目标 L1 生成 2 个 L2 的 mock 数据
            categories = []
            mock_l1_categories = settings.TARGET_L1_CATEGORIES
            if settings.TARGET_ALL_L1_CATEGORIES:
                from collector.category_discovery import load_category_tree
                cached_tree = load_category_tree(settings.CATEGORY_TREE_CACHE) or {}
                mock_l1_categories = list(cached_tree.keys())
            for l1 in mock_l1_categories:
                for l2_suffix in ["类目A", "类目B"]:
                    categories.append({
                        "industry_name": l1,
                        "category_name": l2_suffix,
                        "industry_id": f"mock_ind_{l1}",
                        "category_id": f"mock_cat_{l2_suffix}",
                    })
            results = {}
            for cat in categories:
                sk = f"{scope_prefix}_{cat['industry_name']}_{cat['category_name']}"
                existing = database.get_all_run_ids(conn, sk)
                shift = len(existing) * 30
                results[sk] = _generate_mock_products(
                    sk, seed_shift=shift,
                    industry_name=cat["industry_name"],
                    category_name=cat["category_name"],
                )
        else:
            # 真实采集：发现类目 → 批量采集
            categories = await _resolve_categories()
            if not categories:
                logger.error("未发现任何目标类目，终止")
                return {
                    "run_id": run_id, "categories_collected": 0,
                    "total_products": 0, "all_events": [], "excel_path": "",
                }

            logger.info("共发现 %d 个二级类目待采集", len(categories))
            from collector.douyin_compass import DouyinCompassCollector
            import asyncio as _aio
            async with DouyinCompassCollector() as collector:
                total_cats = len(categories)
                for idx, cat in enumerate(categories, 1):
                    ind_name = cat.get("industry_name", "")
                    cat_name = cat.get("category_name", "")
                    ind_id = cat.get("industry_id", "")
                    cat_id = cat.get("category_id", "")
                    scope_key = f"{scope_prefix}_{ind_name}_{cat_name}"

                    logger.info("═══ [%d/%d] 采集 %s > %s ═══", idx, total_cats, ind_name, cat_name)
                    try:
                        products = await collector.collect(
                            scope_key=scope_key,
                            industry_id=ind_id,
                            category_id=cat_id,
                            industry_name=ind_name,
                            category_name=cat_name,
                            _reuse_page=(idx > 1),
                        )
                    except Exception as e:
                        logger.error("[%d/%d] %s > %s 采集异常: %s", idx, total_cats, ind_name, cat_name, e)
                        category_results.append({
                            "industry_name": ind_name, "category_name": cat_name,
                            "status": "采集异常", "products": 0, "events": 0,
                        })
                        continue

                    logger.info("[%d/%d] %s > %s 完成: %d 条", idx, total_cats, ind_name, cat_name, len(products))

                    # 即时写入 + 差分，不等全部完成
                    cat_total = len(products)
                    if cat_total < settings.MIN_PRODUCTS:
                        logger.warning("[%s] 采集 %d 条低于下限，跳过", scope_key, cat_total)
                        category_results.append({
                            "industry_name": ind_name, "category_name": cat_name,
                            "status": "采集失败", "products": cat_total, "events": 0,
                        })
                        continue

                    categories_collected += 1
                    total_products += cat_total
                    logger.info("[%s] 写入快照 (%d 条)", scope_key, cat_total)
                    database.insert_snapshot(conn, run_id, products)

                    latest_run, previous_run = database.get_latest_two_run_ids(conn, scope_key)
                    if previous_run is None:
                        logger.info("[%s] 首次运行，仅 baseline", scope_key)
                        baseline_count += 1
                        category_results.append({
                            "industry_name": ind_name, "category_name": cat_name,
                            "status": "首次基线", "products": cat_total, "events": 0,
                        })
                        continue

                    current_snap = database.get_snapshot(conn, latest_run, scope_key)
                    previous_snap = database.get_snapshot(conn, previous_run, scope_key)

                    events = compute_diff(
                        run_id, scope_key, current_snap, previous_snap,
                    )
                    logger.info("[%s] 差分: %d 条事件", scope_key, len(events))

                    if events:
                        # dry-run 不落库（遵守「不写 notified」契约，避免留下永不送达的残留事件）
                        if not dry_run:
                            database.insert_events(conn, events)
                        all_events.extend(events)
                    category_results.append({
                        "industry_name": ind_name, "category_name": cat_name,
                        "status": "有异动" if events else "无异动",
                        "products": cat_total, "events": len(events),
                    })

                    if idx < total_cats:
                        await _aio.sleep(3)

        # ── 逐类目处理（仅 mock 模式走这里，真实采集已在上面循环中处理）──
        if mock:
            for scope_key, products in results.items():
                sample = products[0] if products else {}
                ind_name = sample.get("industry_name", "")
                cat_name = sample.get("category_name", "")
                cat_total = len(products)
                if cat_total < settings.MIN_PRODUCTS:
                    logger.warning("[%s] 采集 %d 条低于下限，跳过", scope_key, cat_total)
                    category_results.append({
                        "industry_name": ind_name, "category_name": cat_name,
                        "status": "采集失败", "products": cat_total, "events": 0,
                    })
                    continue

                categories_collected += 1
                total_products += cat_total
                logger.info("[%s] 写入快照 (%d 条)", scope_key, cat_total)
                database.insert_snapshot(conn, run_id, products)

                latest_run, previous_run = database.get_latest_two_run_ids(conn, scope_key)
                if previous_run is None:
                    logger.info("[%s] 首次运行，仅 baseline", scope_key)
                    baseline_count += 1
                    category_results.append({
                        "industry_name": ind_name, "category_name": cat_name,
                        "status": "首次基线", "products": cat_total, "events": 0,
                    })
                    continue

                current_snap = database.get_snapshot(conn, latest_run, scope_key)
                previous_snap = database.get_snapshot(conn, previous_run, scope_key)

                events = compute_diff(
                    run_id, scope_key, current_snap, previous_snap,
                )
                logger.info("[%s] 差分: %d 条事件", scope_key, len(events))

                if events:
                    if not dry_run:
                        database.insert_events(conn, events)
                    all_events.extend(events)
                category_results.append({
                    "industry_name": ind_name, "category_name": cat_name,
                    "status": "有异动" if events else "无异动",
                    "products": cat_total, "events": len(events),
                })

        # ── 详情页拓价已停用（2026-06-23）──────────────────────────
        # 价格列用脱敏价格带（price_bin，compute_diff 已回填）。原因：风控下逐条开详情页
        # 对几百条异动会空转 ~90 分钟且拿不到到手价（今日两次实测），拖垮定时窗口。
        # 如需恢复到手价（须先解决详情页风控），取消下面整段注释即可；_enrich_event_prices
        # 与 collector/product_price.py 保留备用。
        # if all_events and not mock:
        #     _enrich_event_prices(all_events)
        #     if not dry_run:
        #         price_map = {
        #             e["product_id"]: e.get("price", "")
        #             for e in all_events if e.get("price")
        #         }
        #         updated = database.update_event_prices(conn, run_id, price_map)
        #         logger.info("详情页价格回填 DB: %d 条", updated)

        # ── 飞书 Base 同步：采集即写（与企微推送时序解耦）────────────────────
        # 让在线表在「采集完—延后推送」的等待窗口内就是最新数据，而非等到 flush 才更新。
        # overwrite 模式幂等：flush 里仍保留同步作为兜底，同一批事件重写结果一致。
        if not dry_run:
            # 按 video_order 前缀隔离，避免把交错运行的服配(video_acc)事件卷进大盘飞书表
            recent_runs = set(database.get_latest_run_ids_for_prefix(conn, scope_prefix, 2))
            to_sync = [
                e for e in database.get_pending_events(conn)
                if e.get("run_id") in recent_runs
                and (e.get("scope_key") or "").startswith(scope_prefix)
            ]
            if to_sync and settings.LARK_BASE_APP_TOKEN and settings.LARK_TABLE_ID:
                from notify.lark import sync_events_to_base
                written = sync_events_to_base(to_sync, run_id)
                logger.info("飞书 Base 同步（采集即写）: 写入 %d / %d 条事件", written, len(to_sync))

        # ── 推送 + 同步 ──────────────────────────────────────────
        # 关键：以「数据库里仍待送达的事件」为准，而非仅本进程内存中的 all_events。
        # 推送/同步排在整轮采集之后，若上一轮在中途被硬杀（没走到这一步），其已落库的
        # 事件会在本轮一并补发，做到「跑完即补齐」。范围限定最近两轮（get_latest_run_ids），
        # 避免把更早的历史残留也一起翻出来重推。
        excel_path = ""
        if dry_run:
            logger.info("[DRY-RUN] 跳过推送，共 %d 条事件", len(all_events))
            for e in all_events[:10]:
                logger.info("  [%s] %s>%s rank=%s %s",
                            e["event_type"], e.get("industry_name", ""),
                            e.get("category_name", ""), e["rank_current"],
                            e["product_title"][:30])
        elif not do_push:
            # --no-push：采集已入库（事件 notified=0），落盘摘要上下文，待 --flush 推送
            _save_summary_sidecar(
                "multi", run_id, ts, categories_collected, category_results,
                scope_prefix=scope_prefix, new_event_count=len(all_events),
            )
        else:
            excel_path = _finalize_multi_push(
                conn, run_id, ts, categories_collected, category_results,
                collected_event_count=len(all_events),
            )

            # ── 企微智能表格同步（已停用，2026-06-23）─────────────────────
            # 运营决定异动数据统一落飞书表（大盘表 + 服配表），企微侧只保留 Webhook 摘要
            # 推送，不再写企微智能表格。下面同步逻辑暂注释保留，恢复时取消注释即可（同时需
            # 在 .env 配齐 WECOM_SMARTSHEET_DOCID / WECOM_SMARTSHEET_SHEET_ID）。
            # if settings.WECOM_SMARTSHEET_DOCID and settings.WECOM_SMARTSHEET_SHEET_ID:
            #     from notify.wecom_smartsheet import sync_to_smartsheet
            #     # 使用 all_events（本轮采集的所有事件）而非 to_send（待推送事件）
            #     sm_events = all_events if all_events else to_send
            #     written_sm = sync_to_smartsheet(sm_events, run_id)
            #     if written_sm:
            #         logger.info("企微智能表格同步: 写入 %d 条事件", written_sm)

        logger.info(
            "═══ 多类目采集完成 | %d 个类目 | %d 条商品 | %d 条事件 | baseline=%d ═══",
            categories_collected, total_products, len(all_events), baseline_count,
        )

        return {
            "run_id": run_id,
            "categories_collected": categories_collected,
            "total_products": total_products,
            "all_events": all_events,
            "excel_path": excel_path,
            "category_results": category_results,
        }

    finally:
        conn.close()


async def _resolve_categories() -> list[dict]:
    """
    解析目标类目列表：优先读缓存，否则启动浏览器自动发现。

    Returns
    -------
    list[dict]
        [{"industry_name", "category_name", "industry_id", "category_id"}, ...]
    """
    from collector.category_discovery import (
        load_category_tree, save_category_tree, discover_categories,
    )

    cache_path = settings.CATEGORY_TREE_CACHE
    tree = load_category_tree(cache_path)
    stale_tree = None
    if not tree:
        stale_tree = load_category_tree(cache_path, allow_expired=True)

    # 检测缺失的 L1（配置了但缓存中没有）
    target_l1 = set(settings.TARGET_L1_CATEGORIES)
    discover_all = settings.TARGET_ALL_L1_CATEGORIES
    if discover_all:
        # 旧缓存可能是按目标名单过滤后的子集，全部类目模式必须重新发现。
        tree = None
    cached_l1 = set(tree.keys()) if tree else set()
    missing_l1 = target_l1 - cached_l1 if not discover_all else set()

    if not tree:
        logger.info("类目树缓存不存在或为空，启动浏览器自动发现...")
        from collector.douyin_compass import DouyinCompassCollector
        async with DouyinCompassCollector() as collector:
            page = collector._page
            await page.goto(
                settings.RANK_ENTRY_URL,
                wait_until="domcontentloaded", timeout=60000,
            )
            await page.wait_for_timeout(3000)
            tree = await discover_categories(page, settings.TARGET_L1_CATEGORIES)

        if tree:
            save_category_tree(cache_path, tree)
        else:
            if stale_tree:
                logger.warning("自动发现未找到任何目标类目，回退使用过期类目缓存")
                tree = stale_tree
            else:
                logger.warning("自动发现未找到任何目标类目")
                return []

    elif missing_l1:
        logger.info("类目树缓存缺少 %d 个一级类目: %s，补充发现...", len(missing_l1), list(missing_l1))
        from collector.douyin_compass import DouyinCompassCollector
        async with DouyinCompassCollector() as collector:
            page = collector._page
            await page.goto(
                settings.RANK_ENTRY_URL,
                wait_until="domcontentloaded", timeout=60000,
            )
            await page.wait_for_timeout(3000)
            new_tree = await discover_categories(page, list(missing_l1))

        if new_tree:
            tree.update(new_tree)
            save_category_tree(cache_path, tree)
            logger.info("补充发现 %d 个一级类目: %s", len(new_tree), list(new_tree.keys()))
        else:
            logger.warning("补充发现未找到任何缺失类目，继续使用现有类目缓存")

    # 展平为列表；跳过 EXCLUDE_L2_CATEGORIES 命中的二级类目（不采集、不推送）
    exclude_l2 = set(settings.EXCLUDE_L2_CATEGORIES)
    flat = []
    skipped = []
    for l1_name, l2_list in tree.items():
        if not discover_all and l1_name not in target_l1:
            continue
        for l2 in l2_list:
            if l2["name"] in exclude_l2:
                skipped.append(f"{l1_name}>{l2['name']}")
                continue
            flat.append({
                "industry_name": l1_name,
                "category_name": l2["name"],
                "industry_id": l2.get("industry_id", ""),
                "category_id": l2.get("category_id", l2.get("id", "")),
            })
    if skipped:
        logger.info("按 EXCLUDE_L2_CATEGORIES 跳过 %d 个二级类目: %s", len(skipped), skipped)
    return flat


async def do_discover() -> None:
    """仅发现并打印类目树（不采集）。"""
    from collector.category_discovery import discover_categories, save_category_tree
    from collector.douyin_compass import DouyinCompassCollector

    logger.info("启动浏览器，发现类目树...")
    async with DouyinCompassCollector() as collector:
        page = collector._page
        await page.goto(
            settings.RANK_ENTRY_URL,
            wait_until="domcontentloaded", timeout=60000,
        )
        await page.wait_for_timeout(3000)
        tree = await discover_categories(page, settings.TARGET_L1_CATEGORIES)

    if tree:
        save_category_tree(settings.CATEGORY_TREE_CACHE, tree)
        print("\n═══ 类目树 ═══")
        for l1, l2s in tree.items():
            print(f"\n【{l1}】({len(l2s)} 个二级类目)")
            for l2 in l2s:
                print(f"  - {l2['name']}  (id={l2.get('category_id', l2.get('id', '?'))}, "
                      f"industry_id={l2.get('industry_id', '?')})")
    else:
        print("未发现任何目标类目")


# ── 推送辅助 ──────────────────────────────────────────────────────────

def _dispatch_events(conn, events: list[dict], scope_key: str) -> None:
    """推送事件到配置的渠道（仅推送当前 scope 的 pending 事件）。"""
    pending = [
        e for e in database.get_pending_events(conn)
        if e.get("scope_key") == scope_key
    ]
    if not pending:
        return
    delivered = dispatcher.dispatch(pending, scope_key=scope_key)
    if delivered:
        database.mark_events_notified(conn, list(delivered))
    remaining = len(pending) - len(delivered)
    if remaining > 0:
        logger.warning("已送达 %d 条，仍有 %d 条未送达", len(delivered), remaining)
    else:
        logger.info("已推送并标记 %d 条事件", len(delivered))


def _dispatch_summary(
    conn, events: list[dict], cat_count: int,
    ts: datetime,
    category_results: list[dict] | None = None,
) -> None:
    """多类目模式：只推送企微摘要（含在线表格链接），不逐条推送事件。"""
    from notify.wecom import send_summary

    # 构建在线表格链接
    lark_url = ""
    if settings.LARK_BASE_APP_TOKEN and settings.LARK_TABLE_ID:
        lark_url = f"https://feishu.cn/base/{settings.LARK_BASE_APP_TOKEN}?table={settings.LARK_TABLE_ID}"

    wecom_sheet_url = settings.WECOM_SMARTSHEET_URL

    delivered = send_summary(
        settings.WECOM_WEBHOOK_URL,
        events=events,
        categories_count=cat_count,
        timestamp=ts,
        category_results=category_results,
        lark_url=lark_url,
        wecom_sheet_url=wecom_sheet_url,
    )

    if not delivered:
        logger.warning("摘要或 Excel 未完整送达，本轮事件保留为待通知")
        return

    # 只标记本次实际推送的这批事件（含补发的上一轮残留），失败的留待下轮重试
    ids = [e["id"] for e in events if e.get("id") is not None]
    if ids:
        database.mark_events_notified(conn, ids)
        logger.info("摘要模式: 标记 %d 条事件为已通知", len(ids))


# ── 延后推送：采集(--no-push)与推送(--flush)解耦 ──────────────────────────
# --no-push 采集入库后把摘要所需上下文（时间戳/类目数/覆盖明细）落到 sidecar，
# --flush 不采集，仅读 sidecar 还原摘要并把 DB 中 notified=0 的待送达事件推出去。
# 用于「采集在原定时间跑、推送延后半小时」的定时编排（见 run_multi_then_acc.ps1）。

def _summary_sidecar_path(lane: str) -> str:
    return os.path.join(settings.BASE_DIR, "data", f"pending_summary_{lane}.json")


def _save_summary_sidecar(
    lane: str, run_id: str, ts: datetime,
    categories_collected: int, category_results: list[dict],
    scope_prefix: str = "", new_event_count: int = 0,
) -> None:
    """采集完成（--no-push）后落盘摘要上下文，供后续 --flush 还原推送。"""
    data = {
        "run_id": run_id,
        "ts": ts.isoformat(),
        "categories_collected": categories_collected,
        "category_results": category_results,
        "scope_prefix": scope_prefix,
    }
    try:
        with open(_summary_sidecar_path(lane), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(
            "[%s] --no-push 采集入库完成，%d 条新事件待 flush 推送", lane, new_event_count,
        )
    except Exception as e:
        logger.warning("[%s] 写入待推送 sidecar 失败: %s", lane, e)


def _load_summary_sidecar(lane: str) -> dict | None:
    """读取 --no-push 落盘的摘要上下文；缺失/损坏返回 None。"""
    try:
        with open(_summary_sidecar_path(lane), "r", encoding="utf-8") as f:
            data = json.load(f)
        data["ts"] = datetime.fromisoformat(data["ts"])
        return data
    except FileNotFoundError:
        logger.warning("[%s] 未找到待推送 sidecar（无 --no-push 采集？），按空摘要推送", lane)
        return None
    except Exception as e:
        logger.warning("[%s] 读取待推送 sidecar 失败: %s，按空摘要推送", lane, e)
        return None


def _finalize_multi_push(
    conn, run_id: str, ts: datetime,
    categories_collected: int, category_results: list[dict],
    collected_event_count: int = 0,
    scope_prefix: str = "video_order",
) -> str:
    """大盘推送 + 同步：以 DB 中待送达事件为准，生成 Excel、推企微摘要、同步飞书表。

    正常采集路径与 --flush 路径共用本函数。返回 Excel 报告路径。
    """
    # 必须按大盘前缀隔离：延后推送下，服配(video_acc)的 run_id 往往比大盘更新，
    # 用全局 get_latest_run_ids 会把服配事件卷进大盘摘要、推到大盘群并标记已通知，
    # 导致服配 flush 捞不到事件、服配群漏推。前缀隔离与 _finalize_acc_push 对称。
    recent_runs = set(database.get_latest_run_ids_for_prefix(conn, scope_prefix, 2))
    to_send = [
        e for e in database.get_pending_events(conn)
        if e.get("run_id") in recent_runs
        and (e.get("scope_key") or "").startswith(scope_prefix)
    ]
    backlog = len(to_send) - collected_event_count
    if backlog > 0:
        logger.info("检测到上一轮未送达事件 %d 条，本轮一并补发", backlog)

    report_dir = os.path.join(settings.BASE_DIR, "data", "reports")
    excel_path = generate_report(
        to_send, report_dir, timestamp=ts,
        category_results=category_results,
    )

    _dispatch_summary(
        conn, to_send, categories_collected, ts,
        category_results=category_results,
    )

    # 飞书 Base 已在采集阶段写入（见 run_multi 采集后的「采集即写」），此处不再重复写。
    return excel_path


def _finalize_acc_push(
    conn, run_id: str, ts: datetime,
    categories_collected: int, scope_prefix: str,
    collected_event_count: int = 0,
) -> None:
    """服配推送 + 同步：服配企微群 + 服配飞书表（不生成 Excel、不写企微智能表格）。

    正常采集路径与 --flush 路径共用本函数。补发窗口按 scope_prefix 前缀隔离。
    """
    recent_runs = set(database.get_latest_run_ids_for_prefix(conn, scope_prefix, 2))
    to_send = [
        e for e in database.get_pending_events(conn)
        if e.get("run_id") in recent_runs
        and (e.get("scope_key") or "").startswith(scope_prefix)
    ]
    backlog = len(to_send) - collected_event_count
    if backlog > 0:
        logger.info("检测到上一轮未送达事件 %d 条，本轮一并补发", backlog)

    # 飞书 Base 已在采集阶段写入（见 run_acc 采集后的「采集即写」），此处不再重复写。
    wecom_ok = False
    if to_send:
        from notify.wecom import send_summary
        lark_url = ""
        if settings.LARK_BASE_APP_TOKEN and settings.LARK_ACC_TABLE_ID:
            lark_url = (
                f"https://feishu.cn/base/{settings.LARK_BASE_APP_TOKEN}"
                f"?table={settings.LARK_ACC_TABLE_ID}"
            )
        wecom_ok = send_summary(
            settings.WECOM_ACC_WEBHOOK_URL,
            events=to_send,
            categories_count=categories_collected,
            timestamp=ts,
            category_results=[],
            lark_url=lark_url,
            wecom_sheet_url="",
        )
        logger.info("服配企微摘要推送: %s（%d 条事件）", "成功" if wecom_ok else "失败", len(to_send))

    # 企微推送成功才标记（飞书已在采集阶段写入，不再纳入此处门槛）；失败则保留待下轮补发。
    if to_send and wecom_ok:
        ids = [e["id"] for e in to_send if e.get("id") is not None]
        if ids:
            database.mark_events_notified(conn, ids)
            logger.info("服配支线: 标记 %d 条事件为已通知", len(ids))


# ── CLI ───────────────────────────────────────────────────────────────

async def do_login() -> None:
    """打开浏览器让用户手动登录抖音罗盘。"""
    from collector.douyin_compass import DouyinCompassCollector
    logger.info("打开浏览器，请手动登录抖音罗盘，登录完成后回到这里按 Enter...")
    async with DouyinCompassCollector() as collector:
        page = collector._page
        await page.goto(settings.COMPASS_URL, timeout=60000)
        await asyncio.to_thread(
            input, "\n>>> 浏览器已打开，完成抖音登录后按 Enter 关闭并保存登录态..."
        )
        logger.info("登录态已保存至: %s", settings.BROWSER_USER_DATA_DIR)


def list_runs(scope_key: str) -> None:
    """打印历史 run_id 列表。"""
    database.init_db(settings.DB_PATH)
    conn = database.get_connection(settings.DB_PATH)
    runs = database.get_all_run_ids(conn, scope_key)
    conn.close()
    if not runs:
        print(f"[{scope_key}] 暂无历史记录")
        return
    print(f"[{scope_key}] 历史 run_id（共 {len(runs)} 轮，最新在前）：")
    for r in runs:
        print(f"  {r}")


# ── 服配叶子类目支线 ─────────────────────────────────────────────────


def _process_category(
    conn, run_id: str, scope_key: str,
    products: list[dict], dry_run: bool,
    emit_initial_events: bool = False,
) -> tuple[list[dict], int, str]:
    """单类目「写快照 → 差分 → 写事件」管道。返回 (events, 商品数, status)。

    status: "baseline" | "有异动" | "无异动"。dry_run 时不落库事件（遵守不写 notified 契约）。
    emit_initial_events=True 时，首轮也按 NEW_ENTRY 写事件；仅用于服配支线补齐明细表。
    """
    cat_total = len(products)
    database.insert_snapshot(conn, run_id, products)

    latest_run, previous_run = database.get_latest_two_run_ids(conn, scope_key)
    if previous_run is None:
        if not emit_initial_events:
            logger.info("[%s] 首次运行，仅 baseline", scope_key)
            return [], cat_total, "baseline"
        current_snap = database.get_snapshot(conn, latest_run, scope_key)
        events = compute_diff(run_id, scope_key, current_snap, [])
        logger.info("[%s] 首次运行，按 NEW_ENTRY 写入 %d 条事件", scope_key, len(events))
        if events and not dry_run:
            database.insert_events(conn, events)
        return events, cat_total, "有异动" if events else "无异动"

    current_snap = database.get_snapshot(conn, latest_run, scope_key)
    previous_snap = database.get_snapshot(conn, previous_run, scope_key)
    events = compute_diff(run_id, scope_key, current_snap, previous_snap)
    logger.info("[%s] 差分: %d 条事件", scope_key, len(events))

    if events and not dry_run:
        database.insert_events(conn, events)

    return events, cat_total, "有异动" if events else "无异动"


async def run_acc(
    dry_run: bool = False, mock: bool = False,
    do_collect: bool = True, do_push: bool = True,
) -> dict:
    """
    服配叶子类目支线：监控指定叶子类目的榜单异动，写入专属飞书表。

    与大盘（run_multi）完全隔离：scope_key 前缀 video_acc，推送企微消息并写服配飞书表，
    不写企微智能表格、不生成 Excel。采集源跟随全局配置（与大盘一致，当前为短视频榜）。

    叶子直采：短视频榜 API 的 category_id 接受「L2,L3,...,叶子」完整路径，故每个叶子
    用 resolve_leaf_targets 给出的 rank_category_id 直采该叶子专属 TOP200，不再采 L2
    整榜后本地过滤（旧法 TOP200 内常 0 命中配饰叶子）。
    """
    run_id = datetime.now(timezone.utc).isoformat()
    ts = _business_now()
    scope_prefix = "video_acc"
    logger.info("═══ 服配支线 run_id: %s ═══", run_id)

    if not do_collect:
        # flush 模式：不采集叶子，仅把 DB 中待送达的服配事件推出去（读 sidecar 还原摘要）
        database.init_db(settings.DB_PATH)
        conn = database.get_connection(settings.DB_PATH)
        try:
            sc = _load_summary_sidecar("acc") or {}
            f_run_id = sc.get("run_id", run_id)
            f_ts = sc.get("ts", ts)
            f_cat = sc.get("categories_collected", 0)
            if not dry_run:
                _finalize_acc_push(conn, f_run_id, f_ts, f_cat, scope_prefix)
            logger.info("═══ flush 推送完成（服配支线）═══")
            return {
                "run_id": f_run_id, "categories_collected": f_cat,
                "total_products": 0, "all_events": [],
            }
        finally:
            conn.close()

    # ── 解析叶子目标 ──────────────────────────────────────────────────
    if mock:
        l1 = settings.ACC_PATH[0] if settings.ACC_PATH else "服饰内衣"
        l2 = settings.ACC_PATH[1] if len(settings.ACC_PATH) >= 2 else l1
        targets = [
            {
                "industry_name": l1, "category_name": l2, "leaf_name": name,
                "industry_id": "4", "category_id": "mock_acc_l2",
                "leaf_category_id": f"mock_acc_leaf_{i}",
            }
            for i, name in enumerate(settings.ACC_LEAF_NAMES)
        ]
    else:
        dump_path = os.path.join(settings.BASE_DIR, "data", "category_raw_dump.json")
        if not os.path.exists(dump_path):
            logger.error("类目原始树 dump 不存在: %s，请先运行 --discover 或 --multi 建立缓存", dump_path)
            return {"run_id": run_id, "categories_collected": 0, "total_products": 0, "all_events": []}
        with open(dump_path, "r", encoding="utf-8") as f:
            raw_options = json.load(f)

        from collector.category_discovery import resolve_leaf_targets
        targets = resolve_leaf_targets(raw_options, settings.ACC_PATH, settings.ACC_LEAF_NAMES)
        if not targets:
            logger.error("未匹配到任何叶子类目目标，终止")
            return {"run_id": run_id, "categories_collected": 0, "total_products": 0, "all_events": []}

    logger.info("服配支线目标: %s", [t["leaf_name"] for t in targets])

    db_path = settings.DB_PATH
    database.init_db(db_path)
    conn = database.get_connection(db_path)

    all_events: list[dict] = []
    total_products = 0
    categories_collected = 0

    try:
        if mock:
            for cat in targets:
                scope_key = f"{scope_prefix}_{cat['leaf_name']}"
                existing = database.get_all_run_ids(conn, scope_key)
                shift = len(existing) * 30
                products = _generate_mock_products(
                    scope_key, seed_shift=shift,
                    industry_name=cat["industry_name"],
                    category_name=cat["category_name"],
                )
                events, cat_total, _ = _process_category(conn, run_id, scope_key, products, dry_run)
                categories_collected += 1
                total_products += cat_total
                all_events.extend(events)
        else:
            from collector.douyin_compass import DouyinCompassCollector
            async with DouyinCompassCollector() as collector:
                # 预热：先做一次冷导航建立 base API URL + 选中「短视频榜」tab。
                # 实测首个叶子若走冷启动（_reuse_page=False）拿不到数据，故先预热再
                # 逐叶子以 _reuse_page=True 直采，让 SPA 按完整类目路径原生加载叶子榜。
                # 预热用「父级路径」(L2,L3 去掉叶子)，与各叶子路径都不同，纯粹建链不抢叶子。
                warm = targets[0]
                warm_path = ",".join(warm["rank_category_id"].split(",")[:-1]) or warm["rank_category_id"]
                try:
                    await collector.collect(
                        scope_key=f"{scope_prefix}__warmup",
                        industry_id=warm["industry_id"],
                        category_id=warm_path,
                        industry_name=warm["industry_name"],
                        category_name=warm["category_name"],
                        _reuse_page=False,
                    )
                except Exception as e:
                    logger.warning("服配预热导航失败（继续逐叶采集）: %s", e)

                total = len(targets)
                for idx, target in enumerate(targets, 1):
                    scope_key = f"{scope_prefix}_{target['leaf_name']}"
                    logger.info(
                        "═══ [%d/%d] 服配叶子直采 %s（path=%s）═══",
                        idx, total, target["leaf_name"], target["rank_category_id"],
                    )
                    # 0 条时重试一次，兜底网络抖动/偶发空页等瞬时失败（此时页面已热）。
                    # 注：面罩长期「返回 0」的真因不是瞬时失败，而是它的榜单天然只有 156 条 <
                    # 大盘下限 160 被误判残缺——已由上面 min_products=1 修掉，这里的重试只兜真瞬时 0。
                    products = []
                    for attempt in range(2):
                        try:
                            # 关键：category_id 传完整路径 L2,L3,叶子 → 直接拉该叶子专属 TOP200
                            products = await collector.collect(
                                scope_key=scope_key,
                                industry_id=target["industry_id"],
                                category_id=target["rank_category_id"],
                                industry_name=target["industry_name"],
                                category_name=target["category_name"],
                                _reuse_page=True,
                                # 服配叶子榜天然可能 <160（面罩仅 156），不能套大盘的 160 下限，
                                # 否则会被误判残缺丢弃。中途页失败仍由 collection_failed 兜底。
                                min_products=1,
                            )
                        except Exception as e:
                            logger.error("[%s] 采集异常（第 %d 次）: %s", scope_key, attempt + 1, e)
                            products = []
                        if products:
                            break
                        if attempt == 0:
                            logger.warning("[%s] 返回 0 条，2s 后重试一次（疑似首叶空窗）", scope_key)
                            await asyncio.sleep(2)

                    if not products:
                        logger.warning("[%s] 重试后仍返回 0 条，跳过", scope_key)
                        continue

                    # 直采结果已是该叶子专属榜；强制回填叶子名（避免 lookup 索引缺失时为空）
                    for p in products:
                        p["leaf_category_name"] = target["leaf_name"]

                    logger.info("[%s] 采到 %d 条", scope_key, len(products))
                    events, cat_total, _ = _process_category(
                        conn, run_id, scope_key, products, dry_run,
                        emit_initial_events=True,
                    )
                    categories_collected += 1
                    total_products += cat_total
                    all_events.extend(events)

                    if idx < total:
                        await asyncio.sleep(3)

        # ── 服配不做详情页拓价（运营决定，2026-06-23）──────────────────
        # 叶子直采后首轮即 5×TOP200≈千条 NEW_ENTRY，逐条开详情页拓价会撞风控空转数小时；
        # 配饰监控用脱敏价格带（price_bin，如 ¥59.9-¥89.9）已够用。价格列即 compute_diff
        # 写入的 price_range 回退值，无需回填。支付金额/商品图不受影响，照常落表。

        # ── 服配飞书 Base 同步：采集即写（与企微推送时序解耦，等待窗口内表即最新）──
        # overwrite 模式幂等：flush 里仍保留同步作为兜底，同一批事件重写结果一致。
        if not dry_run:
            recent_runs = set(database.get_latest_run_ids_for_prefix(conn, scope_prefix, 2))
            to_sync = [
                e for e in database.get_pending_events(conn)
                if e.get("run_id") in recent_runs
                and (e.get("scope_key") or "").startswith(scope_prefix)
            ]
            if to_sync and settings.LARK_BASE_APP_TOKEN and settings.LARK_ACC_TABLE_ID:
                from notify.lark import sync_events_to_base
                written = sync_events_to_base(
                    to_sync, run_id, table_id=settings.LARK_ACC_TABLE_ID, include_leaf=True,
                )
                logger.info("服配飞书 Base 同步（采集即写）: 写入 %d / %d 条事件", written, len(to_sync))

        # ── 推送：服配企微消息 + 服配飞书表；不写企微智能表格，不生成 Excel ──
        if dry_run:
            logger.info("[DRY-RUN] 跳过企微推送与飞书写入，共 %d 条事件", len(all_events))
            for e in all_events[:10]:
                logger.info("  [%s] %s rank=%s %s",
                            e["event_type"], e.get("leaf_category_name", "") or e.get("category_name", ""),
                            e["rank_current"], e["product_title"][:30])
        elif not do_push:
            # --no-push：采集已入库（事件 notified=0），落盘摘要上下文，待 --flush 推送
            _save_summary_sidecar(
                "acc", run_id, ts, categories_collected, [],
                scope_prefix=scope_prefix, new_event_count=len(all_events),
            )
        else:
            _finalize_acc_push(
                conn, run_id, ts, categories_collected, scope_prefix,
                collected_event_count=len(all_events),
            )

        logger.info(
            "═══ 服配支线完成 | %d 个叶子 | %d 条商品 | %d 条事件 ═══",
            categories_collected, total_products, len(all_events),
        )
        return {
            "run_id": run_id,
            "categories_collected": categories_collected,
            "total_products": total_products,
            "all_events": all_events,
        }

    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音罗盘商品榜 TOP200 监控系统")
    parser.add_argument("--scope", default="video_order", help="榜单维度 scope_key（默认 video_order=短视频榜；旧商品卡榜为 card_order）")
    parser.add_argument("--multi", action="store_true", help="多类目模式：自动发现并遍历所有目标类目")
    parser.add_argument("--acc", action="store_true", help="服配叶子类目支线：监控指定叶子类目，写入专属飞书表")
    parser.add_argument("--dry-run", action="store_true", help="只采集差分，不推送")
    parser.add_argument("--no-push", action="store_true", help="只采集入库（事件待送达），不推送；配合 --flush 实现延后推送")
    parser.add_argument("--flush", action="store_true", help="不采集，仅把 DB 中待送达事件推出去（延后推送的第二步）")
    parser.add_argument("--mock", action="store_true", help="使用 mock 数据，跳过 Playwright")
    parser.add_argument("--list-runs", action="store_true", help="查看历史 run_id")
    parser.add_argument("--login", action="store_true", help="打开浏览器手动登录，保存登录态后退出")
    parser.add_argument("--discover", action="store_true", help="仅发现并打印类目树（不采集）")
    parser.add_argument("--setup-smartsheet", action="store_true", help="一键创建企微智能表格并回写 .env")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_runs:
        list_runs(args.scope)
        return

    if args.login:
        asyncio.run(do_login())
        return

    if args.discover:
        asyncio.run(do_discover())
        return

    if args.setup_smartsheet:
        from notify.wecom_smartsheet import setup_smartsheet
        ok = setup_smartsheet()
        if ok:
            logger.info("智能表格创建成功！后续采集将自动同步到智能表格。")
        else:
            logger.error("智能表格创建失败，请检查 corpid / corpsecret 配置。")
        return

    if args.acc:
        result = asyncio.run(run_acc(
            dry_run=args.dry_run, mock=args.mock,
            do_collect=not args.flush, do_push=not args.no_push,
        ))
        logger.info(
            "=== 服配支线完成 | %d 个叶子 | %d 条商品 | %d 条事件 ===",
            result["categories_collected"],
            result["total_products"],
            len(result["all_events"]),
        )
        return

    if args.multi:
        result = asyncio.run(
            run_multi(
                dry_run=args.dry_run,
                mock=args.mock,
                scope_prefix=args.scope,
                do_collect=not args.flush,
                do_push=not args.no_push,
            )
        )
        logger.info(
            "=== 多类目完成 | %d 个类目 | %d 条商品 | %d 条事件 ===",
            result["categories_collected"],
            result["total_products"],
            len(result["all_events"]),
        )
        if result.get("excel_path"):
            logger.info("Excel 报告: %s", result["excel_path"])
    else:
        result = asyncio.run(
            run_once(
                scope_key=args.scope,
                dry_run=args.dry_run,
                mock=args.mock,
            )
        )
        status = "BASELINE" if result["is_baseline"] else f"{len(result['events'])} 条事件"
        logger.info("=== 本轮完成 | %s | 采集 %d 条商品 ===", status, result["total_products"])


if __name__ == "__main__":
    main()
