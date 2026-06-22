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

from datetime import datetime, timezone

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
    scope_prefix: str = "card_order",
) -> dict:
    """
    多类目模式：自动发现目标一级类目下所有二级类目，逐个采集并生成报告。

    Returns
    -------
    dict  {"run_id", "categories_collected", "total_products", "all_events", "excel_path"}
    """
    run_id = datetime.now(timezone.utc).isoformat()
    ts = datetime.now()
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
        else:
            recent_runs = set(database.get_latest_run_ids(conn, 2))
            to_send = [
                e for e in database.get_pending_events(conn)
                if e.get("run_id") in recent_runs
            ]
            backlog = len(to_send) - len(all_events)
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

            # ── 飞书多维表格同步（独立于企微通知，配齐 token 才执行）──────
            if to_send and settings.LARK_BASE_APP_TOKEN and settings.LARK_TABLE_ID:
                from notify.lark import sync_events_to_base
                written = sync_events_to_base(to_send, run_id)
                logger.info("飞书 Base 同步: 写入 %d / %d 条事件", written, len(to_send))

            # ── 企微智能表格同步（通过 wecom-cli，配齐 docid+sheet_id 即启用）──
            # 注意：不依赖 to_send，即使无新事件也同步最新数据到智能表格
            if settings.WECOM_SMARTSHEET_DOCID and settings.WECOM_SMARTSHEET_SHEET_ID:
                from notify.wecom_smartsheet import sync_to_smartsheet
                # 使用 all_events（本轮采集的所有事件）而非 to_send（待推送事件）
                sm_events = all_events if all_events else to_send
                written_sm = sync_to_smartsheet(sm_events, run_id)
                if written_sm:
                    logger.info("企微智能表格同步: 写入 %d 条事件", written_sm)

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
            logger.warning("补充发现未找到任何缺失类目")

    # 展平为列表
    flat = []
    for l1_name, l2_list in tree.items():
        if not discover_all and l1_name not in target_l1:
            continue
        for l2 in l2_list:
            flat.append({
                "industry_name": l1_name,
                "category_name": l2["name"],
                "industry_id": l2.get("industry_id", ""),
                "category_id": l2.get("category_id", l2.get("id", "")),
            })
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音罗盘商品榜 TOP200 监控系统")
    parser.add_argument("--scope", default="card_order", help="榜单维度 scope_key（默认 card_order）")
    parser.add_argument("--multi", action="store_true", help="多类目模式：自动发现并遍历所有目标类目")
    parser.add_argument("--dry-run", action="store_true", help="只采集差分，不推送")
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

    if args.multi:
        result = asyncio.run(
            run_multi(
                dry_run=args.dry_run,
                mock=args.mock,
                scope_prefix=args.scope,
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
