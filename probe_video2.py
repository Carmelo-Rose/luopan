"""精确抓 video_bring_good 一条 card 的完整字段（含 product_info）。"""
import sys, asyncio, json
sys.path.insert(0, '.')
from playwright.async_api import async_playwright
from config import settings

RANK_PAGE = 'https://compass.jinritemai.com/shop/chance/merchandise-product-rank?rank_type=3'
# 短视频榜 实时 接口（来自 probe_video 捕获，date_type=1）
API = (
    'https://compass.jinritemai.com/compass_api/shop/product/product_rank/video_bring_good'
    '?page_no=1&page_size=10&industry_id=4&category_id=1000003282'
    '&brand_type=-1&price_bin=%E4%B8%8D%E9%99%90&search_info=&rank_data_type=1'
    '&begin_date=2026%2F06%2F22+00%3A00%3A00&end_date=2026%2F06%2F22+00%3A00%3A00'
    '&date_type=1&activity_id='
)
OUT = r'D:\workspace\claude\code\luopan\data\probe_video_card.json'


async def main():
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=settings.BROWSER_USER_DATA_DIR, channel='chrome',
        headless=False, args=['--disable-blink-features=AutomationControlled'],
        ignore_https_errors=True,
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(RANK_PAGE, wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(6)
    res = await page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials:'include', headers:{'Accept':'application/json'}});
            return {ok:r.ok, status:r.status, text: await r.text()};
        }""", API)
    out = {'ok': res.get('ok'), 'status': res.get('status')}
    try:
        data = json.loads(res.get('text', ''))
        d = data.get('data', {})
        out['data_keys'] = list(d.keys())
        lst = d.get('data_result') or d.get('card_list') or d.get('list') or []
        out['list_len'] = len(lst)
        if lst:
            out['card0'] = lst[0]
    except Exception as e:
        out['parse_error'] = str(e)
        out['raw_head'] = res.get('text', '')[:1000]
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('written', OUT, 'list_len=', out.get('list_len'), 'status=', out.get('status'))
    await ctx.close()
    await pw.stop()

asyncio.run(main())
