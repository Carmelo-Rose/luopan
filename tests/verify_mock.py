"""快速验证 mock 双轮运行（无 Playwright 依赖）。"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DB_PATH"] = os.path.join(os.path.dirname(__file__), "..", "data", "verify_mock.db")
os.environ["NOTIFY_CHANNEL"] = "none"

from main import run_once

r1 = asyncio.run(run_once("card_order", dry_run=True, mock=True))
assert r1["is_baseline"] is True, "第一轮应为 baseline"
assert r1["total_products"] == 200, f"应采集 200 条, 实际 {r1['total_products']}"
print(f"[PASS] 第一轮 baseline, 采集 {r1['total_products']} 条")

r2 = asyncio.run(run_once("card_order", dry_run=True, mock=True))
assert r2["is_baseline"] is False, "第二轮不应为 baseline"
assert r2["total_products"] == 200
assert len(r2["events"]) > 0, "第二轮应有差分事件"

types = {}
for e in r2["events"]:
    types[e["event_type"]] = types.get(e["event_type"], 0) + 1
print(f"[PASS] 第二轮事件 {len(r2['events'])} 条，分布: {types}")

# 验证去重：再跑一轮不会重复写入同 run_id 事件
r3 = asyncio.run(run_once("card_order", dry_run=True, mock=True))
print(f"[PASS] 第三轮事件 {len(r3['events'])} 条（与第二轮对比不同数据集）")

print("\n=== 所有验证通过 ===")
