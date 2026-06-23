"""Phase 0 探查：抓「短视频榜」真实接口。
打开商品榜单页 → 点「短视频榜」tab → 点「实时」→ 捕获榜单 JSON 接口。
结果写到 data/probe_video.txt（榜单接口保留完整 body）。
"""
import sys, asyncio, json, os
sys.path.insert(0, '.')
from playwright.async_api import async_playwright
from config import settings

# 入口先用通用商品榜单页（默认落在商品卡榜），再用 UI 点到短视频榜
RANK_URL = (
    'https://compass.jinritemai.com/shop/chance/merchandise-product-rank'
    '?rank_type=3'
)
OUT = r'D:\workspace\claude\code\luopan\data\probe_video.txt'

# 疑似榜单数据接口的 URL 关键字（命中则保留完整 body）
RANK_HINTS = ('product_rank', '_hot_v2', 'hot_v2', 'product_card_hot', 'video', 'rank')


async def click_text(page, text):
    """尝试多种方式点击含指定文字的可点击元素。"""
    for sel in (f'text="{text}"', f'text={text}'):
        try:
            el = page.locator(sel).first
            await el.click(timeout=4000)
            print(f'  点击成功: {text} via {sel}')
            return True
        except Exception as e:
            continue
    print(f'  点击失败: {text}')
    return False


async def probe():
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=settings.BROWSER_USER_DATA_DIR,
        channel='chrome',
        headless=False,
        args=['--disable-blink-features=AutomationControlled'],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    api_hits = []  # {url, body, is_rank}

    async def on_resp(resp):
        ct = resp.headers.get('content-type', '')
        if 'json' not in ct:
            return
        try:
            text = (await resp.body()).decode('utf-8', 'ignore')
        except Exception:
            return
        if len(text) < 80:
            return
        url = resp.url
        is_rank = any(h in url for h in RANK_HINTS)
        # 榜单接口留完整 body（截 20k 足够看字段），其它接口只留摘要
        api_hits.append({'url': url, 'body': text[:20000] if is_rank else text[:400], 'is_rank': is_rank})

    page.on('response', on_resp)

    print(f'导航至: {RANK_URL}')
    await page.goto(RANK_URL, wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(8)
    print(f'落地 URL: {page.url}  标题: {await page.title()}')

    # 切到「短视频榜」
    print('点击「短视频榜」...')
    await click_text(page, '短视频榜')
    await asyncio.sleep(5)
    # 切到「实时」
    print('点击「实时」...')
    await click_text(page, '实时')
    await asyncio.sleep(6)
    print(f'最终 URL: {page.url}')

    lines = [
        f'== 最终 URL: {page.url}',
        f'== 标题: {await page.title()}',
        f'== 捕获 {len(api_hits)} 个 JSON 接口；其中疑似榜单接口:',
        '',
    ]
    rank_hits = [h for h in api_hits if h['is_rank']]
    other_hits = [h for h in api_hits if not h['is_rank']]
    for h in rank_hits:
        lines.append(f'[RANK] URL: {h["url"]}')
        lines.append(f'BODY: {h["body"]}')
        lines.append('-' * 60)
    lines.append('\n== 其它 JSON 接口（仅 URL）:')
    for h in other_hits:
        lines.append(f'  {h["url"]}')

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    await page.screenshot(path=r'D:\workspace\claude\code\luopan\data\probe_video.png')
    print(f'结果已写入: {OUT}（榜单接口 {len(rank_hits)} 个）')

    await ctx.close()
    await pw.stop()


asyncio.run(probe())
