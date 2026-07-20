"""
待发布商品详情页属性抓取（品牌/材质/产地/适用人群/货号等）+ 详情图 URL 抓取。

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

详情图提取（2026-07-14 加，**尚未做过真机 DOM 选择器验证**，见下方
_extract_detail_images_from_page 的说明——按 class 名关键词猜容器的启发式，
命中不到就是空列表，不影响已验证工作的属性抓取部分）。
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

# 详情图容器的启发式关键词（class 名小写子串匹配）。没有做过真机验证——
# 抓取时没滚动、没等懒加载（跟属性抓取用一样的"打开页面等4秒"节奏，刻意不加
# 额外行为，2026-07-14 因为探测脚本多做了滚动动作就撞了一次风控），所以这里
# 大概率只能抓到首屏已渲染的详情图，翻页/滚动懒加载的抓不全。命中率和准确率
# 都待下次可以安全做真机验证时再核实、收窄成精确选择器。
_DETAIL_IMG_HINT_KEYWORDS = ("desc", "detail", "rich-text", "richtext", "graphic")


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


async def _extract_detail_images_from_page(page) -> list:
    """启发式抓「图文详情」区域的图片 URL：找祖先链 class 名包含常见详情/描述
    关键词的 <img>。未做过真机 DOM 选择器确认，命中不到就返回空列表——不影响
    属性抓取那部分，也不会因为猜错元素混进无关图片（宁可抓不到，不猜配料表/
    店铺logo这类无关小图）。"""
    try:
        urls = await page.evaluate(
            """
            (kws) => {
                const imgs = Array.from(document.querySelectorAll('img'));
                const hit = [];
                for (const img of imgs) {
                    let el = img, matched = false;
                    for (let d = 0; d < 8 && el && !matched; d++) {
                        const cls = (el.className || '').toString().toLowerCase();
                        if (kws.some(k => cls.includes(k))) matched = true;
                        el = el.parentElement;
                    }
                    if (!matched) continue;
                    const r = img.getBoundingClientRect();
                    if (r.width < 100 || r.height < 100) continue;  // 滤掉图标类小图
                    const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                    if (src && src.startsWith('http')) hit.push(src);
                }
                return [...new Set(hit)];  // 去重，保持出现顺序
            }
            """,
            list(_DETAIL_IMG_HINT_KEYWORDS),
        )
    except Exception as exc:
        logger.warning("解析详情图 DOM 失败: %s", exc)
        return []
    return urls or []


async def _extract_full_from_page(page) -> dict:
    """一次页面访问里同时拿属性 + 详情图（避免详情图和属性分两次开 PDP——
    详情页访问本身有风控成本，能合并就合并，不多访问）。"""
    attrs = await _extract_attrs_from_page(page)
    detail_images = await _extract_detail_images_from_page(page)
    return {"attrs": attrs, "detail_images": detail_images}


async def fetch_products_full(urls: list[str]) -> dict[str, dict]:
    """
    对给定的商品详情 URL 逐个打开抓「产品参数」+「详情图」。

    Returns
    -------
    dict  {product_id: {"attrs": {...}, "detail_images": [...]}}；抓不到的字段
    对应空 dict/空 list（不代表失败，可能该商品本身没有这个区块，或详情图启发式
    没命中）；整个详情页打开失败的商品不进返回值。
    """
    targets: dict[str, str] = {}
    for url in urls:
        pid = _extract_product_id(url)
        if pid not in targets:
            targets[pid] = url
    if not targets:
        return {}

    logger.info("开始为 %d 个商品抓取详情页产品参数+详情图", len(targets))
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
                full = await _extract_full_from_page(page)
                results[pid] = full
                logger.debug("[%d/%d] %s -> %d 个属性字段, %d 张详情图",
                            i, len(targets), pid, len(full["attrs"]), len(full["detail_images"]))
            except Exception as exc:
                logger.warning("[%d/%d] 商品 %s 详情页抓取失败: %s", i, len(targets), pid, exc)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        if pw:
            await pw.stop()

    logger.info("详情页抓取完成: %d/%d 个商品有结果", len(results), len(targets))
    return results


def fetch_products_full_sync(urls: list[str]) -> dict[str, dict]:
    """同步封装。"""
    return asyncio.run(fetch_products_full(urls))


async def fetch_products_attrs(urls: list[str]) -> dict[str, dict]:
    """向后兼容旧接口：只返回属性部分，形状跟改动前完全一样
    （{product_id: {字段名: 字段值}}），fetch_attrs.py 这个老入口不用跟着改。"""
    full = await fetch_products_full(urls)
    return {pid: v["attrs"] for pid, v in full.items()}


def fetch_products_attrs_sync(urls: list[str]) -> dict[str, dict]:
    """同步封装。"""
    return asyncio.run(fetch_products_attrs(urls))
