"""
待发布商品详情页属性抓取（品牌/材质/产地/适用人群/货号等）。

不属于主采集流水线（run.py --multi / --acc），由独立入口 fetch_attrs.py 手动触发：
只对飞书表里标记「待发布」的商品按需抓取，跟主采集的定时窗口无关。

详情 H5 页要求罗盘平台登录态（无登录态会被拦截到扫码页），故仍复用与主采集器
相同的 persistent profile（BROWSER_USER_DATA_DIR）。与主采集互斥：同一 profile
同时只能被一个 Chrome 进程持有，主任务运行期间调用本模块会因 profile 被占用而
启动失败，需等主任务结束后重试。

产品参数区块 DOM 结构（探测得到，不同商品字段数量/种类可能不同，某些类目可能没有）：
  .product-param__params__content__item__content__row
    .product-param__params__content__item__content__row__key    -> 字段名
    .product-param__params__content__item__content__row__value  -> 字段值
"""
import asyncio
import logging
import re

from playwright.async_api import async_playwright

from config import settings

logger = logging.getLogger(__name__)

_NAV_TIMEOUT = 20000
_RENDER_WAIT = 4.0
_ROW_SELECTOR = ".product-param__params__content__item__content__row"


def _extract_product_id(url: str) -> str:
    m = re.search(r"id=(\d+)", url)
    return m.group(1) if m else url


async def _extract_attrs_from_page(page) -> dict:
    """返回 {字段名: 字段值}；无「产品参数」区块或抓取失败返回空 dict。"""
    try:
        rows = await page.eval_on_selector_all(
            _ROW_SELECTOR,
            """
            (rows) => rows.map(r => {
                const key = r.querySelector('[class$="__key"]');
                const value = r.querySelector('[class$="__value"]');
                return [key ? key.textContent.trim() : '', value ? value.textContent.trim() : ''];
            })
            """,
        )
    except Exception as exc:
        logger.warning("解析产品参数 DOM 失败: %s", exc)
        return {}
    return {k: v for k, v in rows if k}


async def fetch_products_attrs(urls: list[str]) -> dict[str, dict]:
    """
    对给定的商品详情 URL 逐个打开抓「产品参数」。

    Returns
    -------
    dict  {product_id: {字段名: 字段值}}；抓不到参数的商品对应空 dict（不代表失败，
    可能该商品本身没有这个区块）；整个详情页打开失败的商品不进返回值。
    """
    targets: dict[str, str] = {}
    for url in urls:
        pid = _extract_product_id(url)
        if pid not in targets:
            targets[pid] = url
    if not targets:
        return {}

    logger.info("开始为 %d 个商品抓取详情页产品参数", len(targets))
    results: dict[str, dict] = {}
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
    except Exception as exc:
        raise RuntimeError(
            "无法打开浏览器 profile，可能是主采集任务(run.py --multi/--acc)正在运行、"
            "占用了同一个 BROWSER_USER_DATA_DIR，请等主任务结束后重试。"
            f" 原始错误: {exc}"
        ) from exc

    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for i, (pid, url) in enumerate(targets.items(), 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                await asyncio.sleep(_RENDER_WAIT)
                attrs = await _extract_attrs_from_page(page)
                results[pid] = attrs
                logger.debug("[%d/%d] %s -> %d 个属性字段", i, len(targets), pid, len(attrs))
            except Exception as exc:
                logger.warning("[%d/%d] 商品 %s 详情页抓属性失败: %s", i, len(targets), pid, exc)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        if pw:
            await pw.stop()

    logger.info("详情页抓属性完成: %d/%d 个商品有结果", len(results), len(targets))
    return results


def fetch_products_attrs_sync(urls: list[str]) -> dict[str, dict]:
    """同步封装。"""
    return asyncio.run(fetch_products_attrs(urls))
