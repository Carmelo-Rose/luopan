"""
抖音罗盘「商品卡榜」采集模块。

采集策略：
  - 使用 persistent_context 复用已登录 Chrome profile
  - 导航至真实榜单页，监听 XHR 响应拦截 product_card_hot_v2 接口
  - 接口拦截失败时降级为 DOM 解析（tr 行）
  - 翻页方式：直接调用接口 page_no 参数（不点击翻页按钮），每页拦截一次响应
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs, parse_qsl, urlunparse

from playwright.async_api import (
    async_playwright, Page, BrowserContext, Response
)

from config import settings

logger = logging.getLogger(__name__)

# ── 真实榜单入口 URL ───────────────────────────────────────────────────
_RANK_ENTRY_URL = (
    "https://compass.jinritemai.com/shop/chance/merchandise-product-rank"
    "?rank_type=3"
)

# ── 数据接口路径关键词 ─────────────────────────────────────────────────
_API_PATH = "product_card_hot_v2"


def _is_rank_api(url: str) -> bool:
    return _API_PATH in url


# ── 分页参数识别 ───────────────────────────────────────────────────────
# 罗盘接口的分页参数名在不同版本/榜单维度下可能不同，按优先级探测。
_PAGE_PARAM_CANDIDATES = ("page_no", "page", "page_num", "pageNo", "pageNum", "page_index")


def _detect_page_param(url: str) -> Optional[str]:
    """
    从真实 API URL 的 query 中识别分页参数名。
    返回候选参数名之一；若都不存在返回 None（交由调用方决定默认行为）。
    """
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return None
    for cand in _PAGE_PARAM_CANDIDATES:
        if cand in qs:
            return cand
    return None


def _build_paged_url(original_url: str, page_no: int, page_param: Optional[str]) -> str:
    """
    把 original_url 的分页参数替换/追加为目标页码。
    page_param 为 None 时回退到默认 'page_no'（保持旧行为，避免完全失败）。
    """
    param = page_param or "page_no"
    if re.search(rf"(?:^|[?&]){re.escape(param)}=\d+", original_url):
        return re.sub(rf"({re.escape(param)})=\d+", rf"\g<1>={page_no}", original_url)
    sep = "&" if "?" in original_url else "?"
    return f"{original_url}{sep}{param}={page_no}"


def _apply_query_overrides(url: str, overrides: dict[str, str]) -> str:
    """替换或追加 query 参数，空值不参与覆盖。"""
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({key: value for key, value in overrides.items() if value})
    return urlunparse(parsed._replace(query=urlencode(params)))


# ── 字段解析 ──────────────────────────────────────────────────────────

def _range_str(obj: Optional[dict]) -> str:
    """把 value_range 列表转为可读字符串，例如 '10000~25000'。"""
    if not isinstance(obj, dict):
        return ""
    vr = obj.get("value_range", [])
    if not vr:
        val = obj.get("value")
        return str(val) if val is not None else ""
    unit = vr[0].get("unit", "")
    vals = [str(v.get("value", "")) for v in vr]
    if unit == "ratio":
        # 转换为百分比
        try:
            pcts = [f"{float(v)*100:.1f}%" for v in vals]
            return "~".join(pcts)
        except Exception:
            return "~".join(vals)
    return "~".join(vals)


def _parse_card(
    card: dict, rank: int, scope_key: str, captured_at: str,
    industry_name: str = "", category_name: str = "",
) -> dict:
    """从 API 响应的单条 card 提取所有目标字段。"""
    info = card.get("product_info", {})
    product_id = str(info.get("id", ""))
    product_title = info.get("name", "")
    product_url = info.get("product_detail_h5_url", "")
    if not product_url and product_id:
        product_url = (
            f"https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html"
            f"?id={product_id}&origin_type=pc_compass_manage"
        )
    price_range = info.get("price_bin", "") or _range_str(info.get("price"))

    pay_amount = _range_str(card.get("pay_amt"))
    clicks = _range_str(card.get("product_click_cnt"))
    conversion_rate = _range_str(card.get("click_pay_rate"))
    card_order_count = _range_str(card.get("pay_combo_cnt"))

    return {
        "rank": rank,
        "product_id": product_id,
        "product_title": product_title,
        "product_url": product_url,
        "price_range": price_range,
        "pay_amount": pay_amount,
        "clicks": clicks,
        "conversion_rate": conversion_rate,
        "card_order_count": card_order_count,
        "captured_at": captured_at,
        "scope_key": scope_key,
        "industry_name": industry_name,
        "category_name": category_name,
    }


async def _parse_api_body(
    body: bytes, page_no: int, page_size: int, scope_key: str, captured_at: str,
    industry_name: str = "", category_name: str = "",
) -> list[dict]:
    """解析接口响应体，返回本页商品列表。"""
    try:
        data = json.loads(body)
    except Exception:
        return []

    card_list = data.get("data", {}).get("card_list", [])
    if not isinstance(card_list, list) or not card_list:
        return []
    return _parse_card_list(
        card_list, page_no, page_size, scope_key, captured_at,
        industry_name=industry_name, category_name=category_name,
    )


def _parse_card_list(
    card_list: list, page_no: int, page_size: int, scope_key: str, captured_at: str,
    industry_name: str = "", category_name: str = "",
) -> list[dict]:
    """把已解析出的 card_list 转为商品列表。"""
    start_rank = (page_no - 1) * page_size + 1
    products = []
    for i, card in enumerate(card_list[:page_size]):
        rank = start_rank + i
        p = _parse_card(
            card, rank, scope_key, captured_at,
            industry_name=industry_name, category_name=category_name,
        )
        if p["product_id"]:
            products.append(p)
    return products


# ── DOM 降级解析（备用） ───────────────────────────────────────────────

async def _parse_dom_rows(
    page: Page, page_no: int, page_size: int, scope_key: str, captured_at: str,
    industry_name: str = "", category_name: str = "",
) -> list[dict]:
    """
    DOM 降级：解析表格 tr 行获取商品信息。
    页面结构：表格含 class*=rankContent，每行为一个 tr（第一行是表头）。
    """
    try:
        await page.wait_for_selector("tr", timeout=10000)
    except Exception:
        return []

    rows = await page.query_selector_all("tr")
    # 跳过表头行
    data_rows = [r for r in rows if await r.query_selector("td")]

    start_rank = (page_no - 1) * page_size + 1
    products = []
    for i, row in enumerate(data_rows[:page_size]):
        rank = start_rank + i
        cells = await row.query_selector_all("td")
        texts = []
        for cell in cells:
            t = (await cell.inner_text()).strip()
            texts.append(t)

        # 从链接里提取 product_id
        link_el = await row.query_selector("a[href*='jinritemai']")
        url = await link_el.get_attribute("href") if link_el else ""
        pid_match = re.search(r"id=(\d+)", url)
        product_id = pid_match.group(1) if pid_match else f"dom_{page_no}_{i}"

        # texts 列顺序：排名 | 商品信息(含标题) | 店铺信息 | 支付金额 | 点击次数 | 转化率 | 成交件数 | 操作
        product_title = texts[1] if len(texts) > 1 else ""
        pay_amount = texts[3] if len(texts) > 3 else ""
        clicks = texts[4] if len(texts) > 4 else ""
        conversion_rate = texts[5] if len(texts) > 5 else ""
        card_order_count = texts[6] if len(texts) > 6 else ""

        products.append({
            "rank": rank,
            "product_id": product_id,
            "product_title": product_title,
            "product_url": url,
            "price_range": "",
            "pay_amount": pay_amount,
            "clicks": clicks,
            "conversion_rate": conversion_rate,
            "card_order_count": card_order_count,
            "captured_at": captured_at,
            "scope_key": scope_key,
            "industry_name": industry_name,
            "category_name": category_name,
        })
    return products


# ── 采集器主类 ────────────────────────────────────────────────────────

class DouyinCompassCollector:
    """
    抖音罗盘商品卡榜采集器。

    用法::

        async with DouyinCompassCollector() as collector:
            products = await collector.collect(scope_key="card_order")
    """

    def __init__(
        self,
        user_data_dir: Optional[str] = None,
        channel: Optional[str] = None,
        rank_url: Optional[str] = None,
        total_pages: Optional[int] = None,
        page_size: Optional[int] = None,
        headless: bool = False,
    ):
        self.user_data_dir = user_data_dir or settings.BROWSER_USER_DATA_DIR
        self.channel = channel or settings.BROWSER_CHANNEL
        self.rank_url = rank_url or _RANK_ENTRY_URL
        self.total_pages = total_pages or settings.TOTAL_PAGES
        self.page_size = page_size or settings.PAGE_SIZE
        self.headless = headless

        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # 缓存首页的请求参数（从拦截的 URL 中提取），用于后续翻页
        self._base_api_params: dict = {}
        self._base_api_url: str = ""

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            channel=self.channel if self.channel != "chromium" else None,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        return self

    async def __aexit__(self, *args):
        # close() 在浏览器已崩溃时可能抛异常，必须保证 stop() 仍执行，
        # 否则 Playwright 的 Node 子进程会泄漏成僵尸进程。
        try:
            if self._context:
                await self._context.close()
        except Exception:
            logger.warning("关闭浏览器上下文失败", exc_info=True)
        finally:
            if self._playwright:
                await self._playwright.stop()

    async def collect(
        self,
        scope_key: str = "card_order",
        industry_id: str = "",
        category_id: str = "",
        industry_name: str = "",
        category_name: str = "",
        _reuse_page: bool = False,
    ) -> list[dict]:
        """
        执行完整采集，返回 200 条商品列表（按 rank 升序）。

        Parameters
        ----------
        industry_id : str
            一级类目 ID（覆盖 settings.INDUSTRY_ID）
        category_id : str
            二级类目 ID（覆盖 settings.CATEGORY_ID）
        industry_name : str
            一级类目名（写入商品记录）
        category_name : str
            二级类目名（写入商品记录）
        _reuse_page : bool
            True = 跳过 goto 导航（多类目采集时复用已有页面）
        """
        captured_at = datetime.now(timezone.utc).isoformat()
        page = self._page
        all_products: list[dict] = []
        collection_failed = False

        ind_id = industry_id or settings.INDUSTRY_ID
        cat_id = category_id or settings.CATEGORY_ID
        ind_name = industry_name
        cat_name = category_name

        logger.info("采集类目: %s > %s (scope=%s)", ind_name, cat_name, scope_key)

        # ── 第 1 页 ─────────────────────────────────────────────
        page1_products, captured_url = await self._collect_page1(
            page, scope_key, captured_at,
            industry_id=ind_id, category_id=cat_id,
            industry_name=ind_name, category_name=cat_name,
            _reuse_page=_reuse_page,
        )
        if not page1_products:
            logger.warning("第 1 页 API 未捕获，尝试 DOM 降级")
            page1_products = await _parse_dom_rows(
                page, 1, self.page_size, scope_key, captured_at,
                industry_name=ind_name, category_name=cat_name,
            )
        all_products.extend(page1_products)
        logger.info("第 1/%d 页采集 %d 条", self.total_pages, len(page1_products))

        if not captured_url:
            logger.error("未能捕获 API URL，无法继续翻页，本轮采集判为不完整")
            collection_failed = True

        if not collection_failed:
            page_param = _detect_page_param(captured_url)
            if page_param:
                logger.info("识别到分页参数: %s", page_param)
            else:
                logger.warning(
                    "未在 API URL 中识别到已知分页参数 %s，将回退使用默认 'page_no'。URL=%s",
                    _PAGE_PARAM_CANDIDATES, captured_url,
                )

            for page_no in range(2, self.total_pages + 1):
                products = await self._collect_page_via_api(
                    page, captured_url, page_no, scope_key, captured_at, page_param,
                    industry_name=ind_name, category_name=cat_name,
                )
                if products is None:
                    logger.warning("第 %d 页采集失败，本轮判为不完整并停止", page_no)
                    collection_failed = True
                    break
                if not products:
                    logger.info("第 %d 页 API 返回空（已到末页），停止", page_no)
                    break
                all_products.extend(products)
                logger.info(
                    "第 %d/%d 页采集 %d 条，累计 %d 条",
                    page_no, self.total_pages, len(products), len(all_products),
                )
                await asyncio.sleep(1.5)

        # 去重 + 排序
        seen: dict[str, dict] = {}
        for p in sorted(all_products, key=lambda x: x["rank"]):
            if p["product_id"] not in seen:
                seen[p["product_id"]] = p
        result = sorted(seen.values(), key=lambda x: x["rank"])

        if collection_failed or len(result) < settings.MIN_PRODUCTS:
            logger.warning(
                "采集不完整（去重后 %d 条，下限 %d，中途失败=%s），返回空",
                len(result), settings.MIN_PRODUCTS, collection_failed,
            )
            return []

        logger.info("采集完成 [%s>%s]，去重后共 %d 条商品", ind_name, cat_name, len(result))
        return result

    async def collect_multi(
        self,
        categories: list[dict],
        scope_prefix: str = "card_order",
    ) -> dict[str, list[dict]]:
        """
        单会话内遍历多个类目采集。

        Parameters
        ----------
        categories : list[dict]
            [{"industry_name": "智能家居", "category_name": "五金",
              "industry_id": "123", "category_id": "456"}, ...]
        scope_prefix : str
            scope_key 前缀，实际 scope = f"{prefix}_{industry}_{category}"

        Returns
        -------
        dict[str, list[dict]]
            {scope_key: products_list}
        """
        results: dict[str, list[dict]] = {}
        total_cats = len(categories)

        for idx, cat in enumerate(categories, 1):
            ind_name = cat.get("industry_name", "")
            cat_name = cat.get("category_name", "")
            ind_id = cat.get("industry_id", "")
            cat_id = cat.get("category_id", "")
            scope_key = f"{scope_prefix}_{ind_name}_{cat_name}"

            logger.info("═══ [%d/%d] 采集 %s > %s ═══", idx, total_cats, ind_name, cat_name)

            try:
                products = await self.collect(
                    scope_key=scope_key,
                    industry_id=ind_id,
                    category_id=cat_id,
                    industry_name=ind_name,
                    category_name=cat_name,
                    _reuse_page=(idx > 1),
                )
                results[scope_key] = products
                logger.info(
                    "[%d/%d] %s > %s 完成: %d 条",
                    idx, total_cats, ind_name, cat_name, len(products),
                )
            except Exception as e:
                logger.error("[%d/%d] %s > %s 采集异常: %s", idx, total_cats, ind_name, cat_name, e)
                results[scope_key] = []

            # 类目间间隔，避免风控
            if idx < total_cats:
                await asyncio.sleep(3)

        return results

    async def _collect_page1(
        self,
        page: Page,
        scope_key: str,
        captured_at: str,
        industry_id: str = "",
        category_id: str = "",
        industry_name: str = "",
        category_name: str = "",
        _reuse_page: bool = False,
    ) -> tuple[list[dict], str]:
        """
        导航到榜单首页，拦截接口 URL 后切换为「实时」维度（date_type=1），
        然后通过 page.evaluate fetch 直接请求实时数据。
        返回 (商品列表, 实时 API URL)。
        """
        captured_url: str = ""

        async def on_response(resp: Response):
            nonlocal captured_url
            if _is_rank_api(resp.url) and resp.status == 200 and not captured_url:
                captured_url = resp.url
                logger.debug("捕获初始 API URL: %s", captured_url)

        page.on("response", on_response)
        try:
            if not _reuse_page:
                logger.info("导航至榜单页: %s", self.rank_url)
                await page.goto(self.rank_url, wait_until="domcontentloaded", timeout=60000)
            else:
                # 复用页面：通过 JS 触发一次新的 API 请求以捕获 URL
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):
                if captured_url:
                    break
                await asyncio.sleep(0.5)
        finally:
            page.remove_listener("response", on_response)

        if not captured_url:
            return [], ""

        # 覆盖类目参数
        ind_id = industry_id or settings.INDUSTRY_ID
        cat_id = category_id or settings.CATEGORY_ID

        realtime_url = _apply_query_overrides(
            captured_url,
            {
                "date_type": "1",
                "industry_id": ind_id,
                "category_id": cat_id,
            },
        )
        logger.info("切换为实时维度: date_type=1")
        if ind_id and cat_id:
            logger.info(
                "类目: %s>%s (industry_id=%s, category_id=%s)",
                industry_name or "未命名", category_name or "未命名",
                ind_id, cat_id,
            )

        products = await self._collect_page_via_api(
            page, realtime_url, 1, scope_key, captured_at, "page_no",
            industry_name=industry_name, category_name=category_name,
        )
        return products or [], realtime_url

    async def _collect_page_via_api(
        self,
        page: Page,
        original_url: str,
        page_no: int,
        scope_key: str,
        captured_at: str,
        page_param: Optional[str] = None,
        industry_name: str = "",
        category_name: str = "",
    ) -> Optional[list[dict]]:
        """
        通过在页面内执行 fetch，直接请求翻页 API（携带相同 Cookie/Token）。

        返回值区分三种语义：
          - None      请求失败
          - []        合法空页
          - [..]      本页商品
        """
        paged_url = _build_paged_url(original_url, page_no, page_param)
        logger.debug("第 %d 页请求 URL: %s", page_no, paged_url)

        try:
            result = await page.evaluate(
                """async (url) => {
                    const resp = await fetch(url, {
                        method: 'GET',
                        credentials: 'include',
                        headers: { 'Accept': 'application/json, text/plain, */*' }
                    });
                    const text = await resp.text();
                    return { ok: resp.ok, status: resp.status, text };
                }""",
                paged_url,
            )
        except Exception as exc:
            logger.error("第 %d 页 fetch 异常: %s", page_no, exc)
            return None

        if not result.get("ok"):
            logger.error("第 %d 页 HTTP %s，疑似登录失效或服务端限流", page_no, result.get("status"))
            return None

        text = result.get("text", "")
        try:
            data = json.loads(text)
        except Exception:
            logger.error(
                "第 %d 页响应非 JSON（疑似登录重定向 HTML），HTTP %s",
                page_no, result.get("status"),
            )
            return None

        card_list = data.get("data", {}).get("card_list", [])
        if not isinstance(card_list, list):
            logger.error("第 %d 页 card_list 结构异常", page_no)
            return None
        if not card_list:
            return []
        return _parse_card_list(
            card_list, page_no, self.page_size, scope_key, captured_at,
            industry_name=industry_name, category_name=category_name,
        )


# ── 同步入口 ──────────────────────────────────────────────────────────

def collect_sync(scope_key: str = "card_order", **kwargs) -> list[dict]:
    """同步调用入口。"""
    async def _run():
        async with DouyinCompassCollector(**kwargs) as c:
            return await c.collect(scope_key=scope_key)
    return asyncio.run(_run())
