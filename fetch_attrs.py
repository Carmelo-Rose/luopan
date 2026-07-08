"""
手动入口：对指定商品详情 URL 抓「产品参数」（品牌/材质/产地等），JSON 输出到 stdout。

不进主采集流水线，跟 run.py --multi/--acc 的定时任务无关，按需手动运行。
与主采集共用同一个 Chrome profile（BROWSER_USER_DATA_DIR），主任务运行期间调用
会因 profile 被占用而失败，报错后请等主任务结束再重试。

用法：
    python fetch_attrs.py --url "https://haohuo.jinritemai.com/...&id=123..."
    python fetch_attrs.py --url URL1 --url URL2
    python fetch_attrs.py --file urls.txt      # 文件每行一个详情 URL
"""
import argparse
import json
import sys

from collector.product_attrs import fetch_products_attrs_sync


def _load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.url or [])
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            urls.extend(line.strip() for line in f if line.strip())
    return urls


def main() -> int:
    # Windows 控制台默认按本地代码页(GBK)编码 stdout，重定向到文件时中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="抓取商品详情页产品参数")
    parser.add_argument("--url", action="append", help="商品详情 URL，可重复传多个")
    parser.add_argument("--file", help="文件路径，每行一个商品详情 URL")
    args = parser.parse_args()

    urls = _load_urls(args)
    if not urls:
        parser.error("至少需要 --url 或 --file 提供一个商品详情 URL")

    try:
        results = fetch_products_attrs_sync(urls)
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
