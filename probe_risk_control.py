"""诊断脚本：探测「详情页拓价」风控是否仍存在。

背景（见 main.py 中 _enrich_event_prices 调用点的注释）：2026-06-23 停用了
逐条打开商品详情页抓真实到手价的功能，因为触发风控后会空转 ~90 分钟且拿不到价，
拖垮定时窗口。此后一直用 price_bin（脱敏价格带）代替。

本脚本用少量样本（默认 8 个真实商品详情页 URL，从本地 SQLite 最近事件/快照里取）
做一次「投石问路」式探测：逐个打开详情页，记录每个请求的耗时、是否成功解析到价格、
是否被重定向到登录/验证页。不做批量、不做高频，避免二次触发或加重风控。

用法（需在配置了 BROWSER_USER_DATA_DIR 且已登录的机器上运行，例如 Windows 定时任务机）：
    python probe_risk_control.py                # 默认取最近 8 个商品
    python probe_risk_control.py --count 5
    python probe_risk_control.py --delay 3       # 请求间隔秒数（默认 2）

结果打印到终端，并写入 data/probe_risk_control_<run_id>.json 留档。
不修改任何生产代码路径；main.py 里 _enrich_event_prices 是否重新启用由人工决定。
"""
import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from config import settings
from db import database
from collector.product_price import _extract_price_from_page, _NAV_TIMEOUT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe_risk_control")

# 疑似被风控拦截/跳转验证页的标志：标题或落地 URL 命中即视为命中风控
_RISK_TITLE_HINTS = ("验证", "安全验证", "环境异常", "访问异常", "拒绝访问")
_RISK_URL_HINTS = ("/captcha", "/security", "/verify", "login")


def _load_recent_product_urls(db_path: str, count: int) -> list[tuple[str, str]]:
    """从本地 DB 取最近的商品详情页 URL，优先事件表（更贴近实际拓价场景），
    事件表为空则退回最近一轮快照。返回 [(product_id, product_url), ...]，按 product_id 去重。
    """
    conn = database.get_connection(db_path)
    try:
        try:
            cur = conn.execute(
                """
                SELECT product_id, product_url FROM ranking_event
                WHERE product_url IS NOT NULL AND product_url != ''
                ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            try:
                cur = conn.execute(
                    """
                    SELECT product_id, product_url FROM products_snapshot
                    WHERE product_url IS NOT NULL AND product_url != ''
                    ORDER BY captured_at DESC
                    """
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = []
    finally:
        conn.close()

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for row in rows:
        pid, url = row["product_id"], row["product_url"]
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, url))
        if len(out) >= count:
            break
    return out


async def _probe_one(page, pid: str, url: str) -> dict:
    result = {"product_id": pid, "url": url}
    t0 = time.monotonic()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
    except Exception as exc:
        result["nav_ok"] = False
        result["error"] = str(exc)
        result["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return result

    price = await _extract_price_from_page(page)
    elapsed_ms = round((time.monotonic() - t0) * 1000)
    title = ""
    try:
        title = await page.title()
    except Exception:
        pass
    final_url = page.url

    risk_hit = any(h in title for h in _RISK_TITLE_HINTS) or any(
        h in final_url for h in _RISK_URL_HINTS
    )

    result.update(
        {
            "nav_ok": True,
            "elapsed_ms": elapsed_ms,
            "final_url": final_url,
            "title": title,
            "price": price,
            "price_ok": bool(price),
            "risk_hit": risk_hit,
        }
    )
    return result


async def probe(count: int, delay: float) -> dict:
    targets = _load_recent_product_urls(settings.DB_PATH, count)
    if not targets:
        logger.error("本地 DB 里没有可用的 product_url，无法探测。先跑一轮 python run.py --multi 采集。")
        return {"targets": 0, "results": []}

    logger.info("取到 %d 个商品详情页 URL，开始逐个探测（间隔 %.1fs）...", len(targets), delay)

    results: list[dict] = []
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=settings.BROWSER_USER_DATA_DIR,
        channel=settings.BROWSER_CHANNEL if settings.BROWSER_CHANNEL != "chromium" else None,
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_https_errors=True,
    )
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for i, (pid, url) in enumerate(targets, 1):
            r = await _probe_one(page, pid, url)
            results.append(r)
            status = "OK" if r.get("nav_ok") and not r.get("risk_hit") else "SUSPECT"
            logger.info(
                "[%d/%d] %s -> %s  耗时=%dms  价格=%r  %s",
                i, len(targets), pid, r.get("final_url", ""),
                r.get("elapsed_ms", -1), r.get("price", ""), status,
            )
            if i < len(targets):
                await asyncio.sleep(delay)
    finally:
        await ctx.close()
        await pw.stop()

    return {"targets": len(targets), "results": results}


def _summarize(report: dict) -> str:
    results = report["results"]
    if not results:
        return "无样本，未探测。"
    n = len(results)
    nav_fail = sum(1 for r in results if not r.get("nav_ok"))
    risk_hit = sum(1 for r in results if r.get("risk_hit"))
    price_ok = sum(1 for r in results if r.get("price_ok"))
    slow = sum(1 for r in results if r.get("elapsed_ms", 0) > 15000)
    avg_ms = round(sum(r.get("elapsed_ms", 0) for r in results) / n)

    verdict = "风控疑似仍存在，不建议重新启用批量详情页拓价。"
    if nav_fail == 0 and risk_hit == 0 and price_ok >= n * 0.7 and slow == 0:
        verdict = "本次样本均正常，风控疑似已解除；建议小规模灰度验证后再考虑恢复拓价。"
    elif nav_fail + risk_hit >= 1:
        verdict = "出现导航失败或疑似验证页跳转，风控大概率仍存在。"
    elif slow > 0:
        verdict = "部分请求异常缓慢（>15s），可能是风控限速前兆，谨慎对待。"

    return (
        f"样本数={n}  导航失败={nav_fail}  疑似风控命中={risk_hit}  "
        f"取到价格={price_ok}  慢请求(>15s)={slow}  平均耗时={avg_ms}ms\n"
        f"结论：{verdict}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="探测详情页风控是否仍存在")
    parser.add_argument("--count", type=int, default=8, help="探测的商品数量（默认 8，不宜过大）")
    parser.add_argument("--delay", type=float, default=2.0, help="请求间隔秒数（默认 2）")
    args = parser.parse_args()

    errors = settings.validate()
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    report = asyncio.run(probe(args.count, args.delay))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(os.path.join(settings.BASE_DIR, "data"), exist_ok=True)
    out_path = os.path.join(settings.BASE_DIR, "data", f"probe_risk_control_{run_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print(_summarize(report))
    print(f"详情已写入: {out_path}")


if __name__ == "__main__":
    main()
