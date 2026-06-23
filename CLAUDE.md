# CLAUDE.md

This repository is the V2 Douyin Compass monitor for short-video product ranking.
`README.md` is the user-facing operations doc; this file captures maintainer rules.

## What This Is

抖音罗盘「短视频榜」TOP200 监控系统。Playwright 复用独立 Chrome Profile 采集榜单快照，SQLite 保存轮次，差分引擎识别新进榜和排名急升商品，再同步到企微/飞书表。

V2 has two production lanes:

- Main lane: `python run.py --multi`, scope prefix `video_order`, writes the main table and summary channels.
- Accessories lane: `python run.py --acc`, scope prefix `video_acc`, monitors configured 服配 leaves, sends WeCom Webhook messages, and writes `LARK_ACC_TABLE_ID`.

旧 `--scope card_order` 只用于兼容旧商品卡榜，不是当前默认流程。

## Commands

```bash
# Install
pip install -r requirements.txt
playwright install chromium

# Login once into the isolated BROWSER_USER_DATA_DIR profile
python run.py --login

# V2 main lane
python run.py --multi
python run.py --multi --dry-run

# V2 accessories lane
python run.py --acc
python run.py --acc --dry-run

# Mock checks
python run.py --mock --dry-run
python run.py --acc --mock --dry-run

# History for one scope
python run.py --list-runs --scope video_order

# Tests
python -m pytest tests/test_diff.py -v
python -m pytest tests/test_category_discovery.py tests/test_collector_url.py tests/test_diff.py -v
```

On this machine, the system `python` may not have Playwright. The scheduled runner uses:

```powershell
& "C:\Users\Administrator.DESKTOP-GRHN4PA\AppData\Roaming\Accio\pre-install\e6550f7e00ff\python\python.exe" run.py --multi
```

Always use `python run.py`, not `python main.py`; `run.py` fixes Windows import paths.

## Architecture

Single-direction data flow:

```text
collector -> db.insert_snapshot -> db.get_latest_two_run_ids -> monitor.diff.compute_diff -> db.insert_events -> notify
```

Important invariants:

- `run_id` is an ISO timestamp. Latest/previous comparisons rely on lexicographic time ordering.
- `get_latest_two_run_ids(conn, scope_key)` is scope-isolated. Do not change it back to global latest runs.
- First run for a scope is baseline only: snapshot is written, no diff, no push.
- `monitor/diff.py` is pure logic. Ranking is smaller-is-better; `delta = previous_rank - current_rank`.
- Event taxonomy is V2 only: `NEW_ENTRY`, `RANK_UP_50`, `RANK_UP_100`, `RANK_UP_150`.
- DB dedupe is authoritative: `UNIQUE (run_id, scope_key, event_type, product_id)` plus `INSERT OR IGNORE`.
- Event push is idempotent: `notified=0` until sync succeeds. `video_acc` retry windows must stay prefix-isolated.

## Collector Notes

- `collector/douyin_compass.py` uses `launch_persistent_context` with `BROWSER_USER_DATA_DIR`; do not point it at the user's daily Chrome profile.
- Current API keyword is `video_bring_good`; old商品卡榜 is `product_card_hot_v2`.
- The short-video API's `category_id` accepts the full comma-joined category path `L2,L3,...,leaf` (e.g. `1000003282,1000003289,1000003461` for 服装,服装配饰,帽子). Passing a leaf id alone returns empty; the full path returns that leaf's own TOP200. The accessories lane therefore queries each leaf directly with `rank_category_id` from `resolve_leaf_targets` (one TOP200 per leaf), no L2-collect-then-local-split. A one-time warm-up cold navigation precedes the per-leaf loop because the first leaf on a cold `_reuse_page=False` nav returns 0.
- DOM fallback is only a first-page safety net and is not sufficient for a valid V2 run.
- Data ranges such as payment and price bins are platform-masked ranges unless detail-page price enrichment succeeds.

## Config

All config comes from `config/settings.py` via `.env`; other modules should import `settings`, not read environment variables directly.

Key V2 settings:

- `RANK_API_PATH=video_bring_good`
- `RANK_TAB_TEXT=短视频榜`
- `TARGET_L1_CATEGORIES` for `--multi`
- `ACC_PATH` and `ACC_LEAF_NAMES` for `--acc`
- `LARK_TABLE_ID` for the main table
- `LARK_ACC_TABLE_ID` for the accessories table

## Gotchas

- No git repository is initialized in this directory.
- Do not run multiple collectors concurrently; the persistent Chrome profile is exclusive.
- Cookie expiry shows up as 0 products or login redirects; rerun `python run.py --login`.
- `data/category_raw_dump.json` is required for `--acc`; refresh it with `--discover` or `--multi` if stale.
- `--acc` must not generate Excel or write WeCom smartsheets; it only sends WeCom Webhook messages and syncs the accessories Lark table.
- Root-level `probe_*.py`, `compass_*.txt/png`, and captured request files are diagnostics, not the production path.
