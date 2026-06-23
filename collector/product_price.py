"""
异动商品实际价格抓取。

榜单列表接口只给脱敏价格带（price_bin，如「¥20.9-¥480」），商品真实售价（如「¥59.9 起」）
只在商品详情 H5 页上。本模块只对**本轮产生异动事件的商品**逐个打开详情页抓价，数量少
（每轮几个~几十个），成本可控。

设计要点：
  - 复用已登录 profile 的 persistent_context（与采集器同一隔离目录），新开一个浏览器会话。
    调用时机在主采集会话关闭之后、推送之前，避免 ProcessSingleton 冲突。
  - 逐个详情页礼貌间隔；单个失败不影响其它，拿不到价格返回空串（调用方回退 price_bin）。
"""
import asyncio
import logging
import re
import threading
from typing import Optional

from playwright.async_api import async_playwright

from config import settings

logger = logging.getLogger(__name__)

# 详情页价格：「¥59.9 起」/「¥59.9」/「￥1,299」等。\s* 跨换行，因 H5 把 ￥/数字/起 拆成多节点。
_PRICE_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)\s*(起)?")
# 券后到手价：「券后 ￥59.9 起」。用户图3 要的是这个到手价，不是吊牌原价（取第一处 ¥ 会拿到原价）。
_COUPON_RE = re.compile(r"券后\s*[¥￥]\s*([\d,]+(?:\.\d+)?)\s*(起)?")

_NAV_TIMEOUT = 20000
# H5 详情页 domcontentloaded 后还要等 JS 水合价格才渲染（1.2s 时 body 仍是「打开抖音APP」加载壳）。
# 轮询整页文本直到出现 ¥数字，最长等 _RENDER_BUDGET 秒。
_RENDER_BUDGET = 8.0
_POLL_INTERVAL = 0.5


def _format_price(num: str, has_qi: bool) -> str:
    return f"¥{num}{'起' if has_qi else ''}"


async def _extract_price_from_page(page) -> str:
    """从当前详情页提取到手价字符串，失败返回空串。

    H5 价格异步水合，先轮询 body 直到出现 ¥数字；优先取「券后」到手价，否则取第一处 ¥。
    """
    body = ""
    waited = 0.0
    while True:
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        if _PRICE_RE.search(body):
            break
        if waited >= _RENDER_BUDGET:
            break
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    m = _COUPON_RE.search(body) or _PRICE_RE.search(body)
    return _format_price(m.group(1), bool(m.group(2))) if m else ""


async def fetch_event_prices(events: list[dict]) -> dict[str, str]:
    """
    对事件商品逐个打开详情页抓真实价格。

    Parameters
    ----------
    events : 事件列表，每条需含 product_id 与 product_url（详情 H5 URL）。

    Returns
    -------
    dict  {product_id: "¥59.9起"}；抓不到的商品不入字典（调用方回退 price_bin）。
    """
    # 按 product_id 去重，避免同一商品多事件重复抓
    targets: dict[str, str] = {}
    for e in events:
        pid = e.get("product_id")
        url = e.get("product_url")
        if pid and url and pid not in targets:
            targets[pid] = url
    if not targets:
        return {}

    logger.info("开始为 %d 个异动商品抓取详情页价格", len(targets))
    prices: dict[str, str] = {}
    pw = None
    ctx = None
    try:
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=settings.BROWSER_USER_DATA_DIR,
            channel=settings.BROWSER_CHANNEL if settings.BROWSER_CHANNEL != "chromium" else None,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for i, (pid, url) in enumerate(targets.items(), 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                price = await _extract_price_from_page(page)
                if price:
                    prices[pid] = price
                    logger.debug("[%d/%d] %s -> %s", i, len(targets), pid, price)
                else:
                    logger.debug("[%d/%d] %s 未解析到价格", i, len(targets), pid)
            except Exception as exc:
                logger.warning("[%d/%d] 商品 %s 详情页抓价失败: %s", i, len(targets), pid, exc)
    except Exception as exc:
        logger.error("详情页抓价会话异常: %s", exc)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        if pw:
            await pw.stop()

    logger.info("详情页抓价完成: %d/%d 个拿到价格", len(prices), len(targets))
    return prices


def fetch_event_prices_sync(events: list[dict]) -> dict[str, str]:
    """同步封装。"""
    async def _run():
        return await fetch_event_prices(events)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        result: dict[str, str] = {}
        error: list[BaseException] = []

        def runner() -> None:
            try:
                result.update(asyncio.run(_run()))
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=runner, name="fetch-event-prices", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result
    return asyncio.run(_run())
