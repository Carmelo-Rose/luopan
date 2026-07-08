"""
类目树自动发现与缓存。

通过 React fiber 直接从级联选择器组件中提取完整 L1→L2→L3 数据，
包含 value（即 industry_id / category_id），无需逐个点击 DOM。

发现流程：
  1. 导航到罗盘页面
  2. 展开级联选择器（触发组件渲染）
  3. 从 React fiber 链中提取 options 数组
  4. 映射为 L1→L2 结构，缓存到 data/category_tree.json

数据结构：
  L1.value = industry_id (如 "6" = 智能家居)
  L2.value = category_id (如 "1000003462" = 五金/工具)
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional
from playwright.async_api import Page

from config import settings

logger = logging.getLogger(__name__)

_REACT_EXTRACT_JS = """
() => {
    const picker = document.querySelector('.ecom-cascader-picker');
    if (!picker) return { error: 'picker not found' };

    let fiber = null;
    for (const key of Object.keys(picker)) {
        if (key.startsWith('__reactFiber') || key.startsWith('__reactInternalInstance')) {
            fiber = picker[key];
            break;
        }
    }
    if (!fiber) return { error: 'no React fiber found' };

    let options = null;
    let optionCount = 0;
    let current = fiber;
    for (let i = 0; i < 100 && current; i++) {
        const props = current.memoizedProps || current.pendingProps || {};
        for (const key of ['options', 'treeData', 'items', 'data']) {
            const candidate = props[key];
            if (Array.isArray(candidate) && candidate.length > optionCount
                && candidate[0] && (candidate[0].label || candidate[0].cate_name)
                && candidate[0].children) {
                options = candidate;
                optionCount = candidate.length;
            }
        }
        current = current.return;
    }
    if (!options) return { error: 'options not found in fiber chain' };

    function extract(node, depth) {
        if (!node || depth > 4) return null;
        const result = {
            value: String(node.value || ''),
            label: String(node.label || ''),
            cate_id: String(node.cate_id || node.value || ''),
            cate_name: String(node.cate_name || node.label || ''),
            isLeaf: !!node.isLeaf,
        };
        if (node.children && Array.isArray(node.children) && node.children.length > 0) {
            result.children = node.children
                .map(c => extract(c, depth + 1))
                .filter(Boolean);
        }
        return result;
    }

    return {
        count: options.length,
        options: options.map(o => extract(o, 0)),
    };
}
"""


# 级联里代表「该层全部」的特殊子节点文案。选中它即等于采集其父类目下所有商品。
_ALL_NODE_LABEL = "全部"


def _clean_label(node: dict) -> str:
    return (node.get("label") or node.get("cate_name") or "").strip()


def _node_id(node: dict) -> str:
    return str(node.get("value") or node.get("cate_id") or "")


def _raw_dump_path() -> str:
    """原始类目树落盘路径（与类目树缓存同目录）。"""
    return os.path.join(
        os.path.dirname(settings.CATEGORY_TREE_CACHE), "category_raw_dump.json"
    )


def _dump_raw_tree(raw_options: list) -> None:
    """把提取到的完整原始类目树（含各级 children/isLeaf）落盘，便于离线核对结构。

    防退化：补充发现（只查个别缺失 L1）有时只提取到部分 L1，若直接覆盖会把完整
    dump 砍小。故当本次 L1 数 < 现有 dump 的 L1 数时跳过覆盖，保留更完整的版本。
    """
    try:
        dump_path = _raw_dump_path()
        if os.path.exists(dump_path):
            try:
                with open(dump_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, list) and len(raw_options) < len(existing):
                    logger.warning(
                        "本次原始类目树仅 %d 个 L1 < 现有 dump %d 个，疑似部分发现，跳过覆盖",
                        len(raw_options), len(existing),
                    )
                    return
            except Exception:
                pass
        os.makedirs(os.path.dirname(dump_path), exist_ok=True)
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(raw_options, f, ensure_ascii=False, indent=2)
        logger.info("原始类目树已 dump 到: %s（供核对结构/「全部」层级）", dump_path)
    except Exception as e:
        logger.warning("dump 原始类目树失败: %s", e)


def flatten_category_lookup(raw_options: list) -> dict:
    """
    把完整类目树（L1→L5）拍平为 {cate_id: {l1,l2,l3,leaf,path}} 索引。

    用于把商品自带的 leaf_category_id 翻译成各层级名字：
      - l3   = 叶子往上数第 3 层（三级类目，所有商品层级统一）
      - leaf = 该节点自身名字（最细类目）
    「全部」占位节点不建索引（但仍向下遍历其 children）。
    """
    lookup: dict[str, dict] = {}

    def walk(node: dict, path: list[tuple[str, str]]) -> None:
        name = _clean_label(node)
        cid = _node_id(node)
        if not name or name == _ALL_NODE_LABEL or not cid or cid == "0":
            new_path = path  # 占位节点不进 path、不建索引
        else:
            new_path = path + [(cid, name)]
            names = [n for _, n in new_path]
            lookup[cid] = {
                "l1": names[0] if len(names) >= 1 else "",
                "l2": names[1] if len(names) >= 2 else "",
                "l3": names[2] if len(names) >= 3 else "",
                "leaf": names[-1],
                "path": names,
            }
        for child in (node.get("children") or []):
            walk(child, new_path)

    for root in raw_options:
        walk(root, [])
    return lookup


def save_category_lookup(cache_path: str, lookup: dict) -> None:
    """保存类目拍平索引到缓存文件。"""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(lookup, f, ensure_ascii=False, indent=2)
        logger.info("类目拍平索引已缓存到: %s（%d 个 cate_id）", cache_path, len(lookup))
    except Exception as e:
        logger.warning("保存类目拍平索引失败: %s", e)


def load_category_lookup(cache_path: str) -> Optional[dict]:
    """加载类目拍平索引；不存在或损坏返回 None。无 TTL（随类目发现一起刷新）。"""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            lookup = json.load(f)
        logger.info("加载类目拍平索引: %s（%d 个 cate_id）", cache_path, len(lookup))
        return lookup
    except Exception as e:
        logger.warning("加载类目拍平索引失败: %s", e)
        return None


def ensure_category_lookup(cache_path: str) -> dict:
    """
    返回类目拍平索引；缓存缺失时尝试用已落盘的 category_raw_dump.json 现场构建并补缓存。

    用于「类目树缓存命中、跳过了在线发现」的场景——此时 lookup 缓存可能尚未生成，
    但原始树 dump 通常已存在，足以离线重建索引。最终拿不到返回空 dict（采集不报错，列留空）。
    """
    lookup = load_category_lookup(cache_path)
    if lookup:
        return lookup
    dump_path = _raw_dump_path()
    if os.path.exists(dump_path):
        try:
            with open(dump_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            lookup = flatten_category_lookup(raw)
            if lookup:
                save_category_lookup(cache_path, lookup)
                logger.info("类目拍平索引缺失，已从原始树 dump 重建")
                return lookup
        except Exception as e:
            logger.warning("从原始类目树 dump 重建拍平索引失败: %s", e)
    return {}


async def discover_categories(page: Page, target_l1_names: list[str]) -> dict:
    """
    从 React fiber 中提取类目树，按目标一级类目过滤，采集粒度为二级类目（L2）。

    选中某个 L2 后其下都有一个「全部」(value=0) 占位项，选它即代表采集该 L2
    全部商品，故 category_id 用 L2 自身 id（不是「全部」节点的 0）。

    Returns
    -------
    dict
        {
          "智能家居": [
            {"name": "五金/工具", "category_id": "1000001142", "industry_id": "7"},
            ...
          ],
          ...
        }
    """
    logger.info("开始类目树自动发现（React fiber），目标 L1: %s", target_l1_names or "（账号可见的全部）")

    # 确保级联选择器已展开（触发组件渲染）
    await _ensure_cascader_rendered(page)
    # 尽力 hover 展开各目标 L1/L2，触发子级懒加载，避免只拿到首列数据
    await _expand_target_columns(page, target_l1_names)
    await page.wait_for_timeout(1000)

    # 从 React fiber 提取完整数据
    result = await page.evaluate(_REACT_EXTRACT_JS)

    if result.get("error"):
        logger.error("React fiber 提取失败: %s", result["error"])
        return {}

    raw_options = result.get("options", [])
    logger.info("从 React fiber 提取到 %d 个一级类目", len(raw_options))
    _dump_raw_tree(raw_options)

    # 同步刷新「id→三级/叶子类目名」拍平索引（采集器据此翻译商品 leaf_category_id）。
    # 注意：采集粒度虽是 L2，但索引覆盖全树各层，供商品叶子类目反查。
    # 防退化：补充发现可能只提取到部分树，若拍平条数比现有少则跳过覆盖，避免砍小索引。
    try:
        new_lookup = flatten_category_lookup(raw_options)
        existing = load_category_lookup(settings.CATEGORY_LOOKUP_CACHE) or {}
        if len(new_lookup) >= len(existing):
            save_category_lookup(settings.CATEGORY_LOOKUP_CACHE, new_lookup)
        else:
            logger.warning(
                "本次拍平索引 %d 条 < 现有 %d 条，疑似部分发现，保留现有索引不覆盖",
                len(new_lookup), len(existing),
            )
    except Exception as e:
        logger.warning("构建类目拍平索引失败（非致命）: %s", e)

    target_set = set(target_l1_names)
    discover_all = not target_set
    tree: dict[str, list[dict]] = {}

    for l1 in raw_options:
        l1_name = _clean_label(l1)
        l1_id = _node_id(l1)

        if not discover_all and l1_name not in target_set:
            logger.debug("跳过 L1: %s (不在目标中)", l1_name)
            continue

        # 采集粒度 = 二级类目（L2）。选中 L2 后其下都有一个「全部」(value=0) 占位项，
        # 选「全部」即代表该 L2 全部商品，因此 category_id 用 L2 自身 id（不是「全部」的 0）。
        l2_list: list[dict] = []
        for l2 in (l1.get("children") or []):
            l2_name = _clean_label(l2)
            l2_id = _node_id(l2)
            if not l2_name or l2_name == _ALL_NODE_LABEL or not l2_id or l2_id == "0":
                continue
            l2_list.append({
                "name": l2_name,
                "category_id": l2_id,
                "industry_id": l1_id,
            })

        if l2_list:
            tree[l1_name] = l2_list
            logger.info("  [%s] (industry_id=%s): %d 个二级类目: %s",
                        l1_name, l1_id, len(l2_list),
                        [c["name"] for c in l2_list])
        else:
            logger.warning("  [%s]: 无二级类目（可能子级未加载或账号无权限）", l1_name)

    logger.info("类目树发现完成: %d 个 L1, 共 %d 个二级类目",
                len(tree), sum(len(v) for v in tree.values()))
    if target_set:
        missing = sorted(target_set - set(tree))
        if missing:
            logger.warning(
                "目标一级类目未出现在当前账号的商品卡榜类目树中: %s；"
                "可能是该账号无此类目权限、或类目名与平台不一致，已跳过（非致命）",
                missing,
            )
    return tree


async def _ensure_cascader_rendered(page: Page) -> None:
    """展开级联选择器，确保 React 组件已渲染。"""
    await page.evaluate("""
        () => {
            const label = document.querySelector('.ecom-cascader-picker-label');
            if (label) label.click();
        }
    """)
    for _ in range(20):
        visible = await page.evaluate("""
            () => {
                const menus = document.querySelector('.ecom-cascader-menus');
                return menus && menus.offsetHeight > 0;
            }
        """)
        if visible:
            logger.info("级联选择器已展开")
            return
        await asyncio.sleep(0.3)
    logger.warning("级联选择器展开超时，继续尝试提取...")


async def _expand_target_columns(page: Page, target_l1_names: list[str]) -> None:
    """
    尽力 hover 展开每个目标 L1 及其 L2，触发级联子级懒加载，
    让 React props 带上完整 children（否则只拿到当前选中 L1 的子级）。
    任意步骤失败都不影响主流程——拿不到就按已加载的数据提取。

    注：若实跑发现 L2/L3 仍不全，多半是 expandTrigger 为 click 而非 hover，
    把下面的 .hover() 换成 .click() 即可（点击非叶子节点只是展开、不会选中）。
    """
    names = list(target_l1_names or [])
    if not names:
        return
    try:
        menus = page.locator(".ecom-cascader-menus")
        for l1 in names:
            l1_item = menus.locator(".ecom-cascader-menu-item", has_text=l1).first
            try:
                if await l1_item.count() == 0:
                    continue
                await l1_item.hover(timeout=3000)
                await page.wait_for_timeout(400)
            except Exception:
                continue
            # 展开后最后一列是该 L1 的 L2，逐个 hover 触发 L3 懒加载
            columns = menus.locator(".ecom-cascader-menu")
            col_count = await columns.count()
            if col_count < 2:
                continue
            l2_items = columns.nth(col_count - 1).locator(".ecom-cascader-menu-item")
            n = await l2_items.count()
            for i in range(n):
                try:
                    await l2_items.nth(i).hover(timeout=2000)
                    await page.wait_for_timeout(250)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("级联展开（懒加载触发）未完全成功，按已加载数据提取: %s", e)


# 类目缓存 TTL（秒），默认 7 天
_CATEGORY_CACHE_TTL_SECONDS = 7 * 24 * 3600


def load_category_tree(
    cache_path: str,
    ttl: int = _CATEGORY_CACHE_TTL_SECONDS,
    allow_expired: bool = False,
) -> Optional[dict]:
    """从缓存文件加载类目树，超过 TTL 则视为过期。"""
    if not os.path.exists(cache_path):
        return None
    try:
        age = time.time() - os.path.getmtime(cache_path)
        if age > ttl and not allow_expired:
            logger.info("类目树缓存已过期（%.1f 天），将重新发现", age / 86400)
            return None
        with open(cache_path, "r", encoding="utf-8") as f:
            tree = json.load(f)
        if age > ttl:
            logger.warning("类目树缓存已过期（%.1f 天），但将继续使用旧缓存", age / 86400)
        logger.info("从缓存加载类目树: %s (%d 个 L1, 共 %d 个 L2)",
                     cache_path, len(tree),
                     sum(len(v) for v in tree.values()))
        return tree
    except Exception as e:
        logger.warning("加载类目树缓存失败: %s", e)
        return None


def save_category_tree(cache_path: str, tree: dict) -> None:
    """保存类目树到缓存文件。"""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)
        logger.info("类目树已缓存到: %s", cache_path)
    except Exception as e:
        logger.warning("保存类目树缓存失败: %s", e)


def resolve_leaf_targets(
    raw_options: list,
    path: list[str],
    leaf_names: list[str],
) -> list[dict]:
    """
    从原始类目树中解析叶子类目目标（服配支线用）。

    Parameters
    ----------
    raw_options : list
        完整原始类目树（category_raw_dump.json 的内容）。
    path : list[str]
        从 L1 到目标父节点的名称路径，如 ["服饰内衣", "服装", "服装配饰"]。
    leaf_names : list[str]
        要匹配的叶子类目名称列表，如 ["帽子", "丝巾/披肩/头巾", ...]。

    Returns
    -------
    list[dict]
        [{"industry_name", "category_name", "leaf_name", "industry_id",
          "category_id", "leaf_category_id", "rank_category_id"}, ...]
        industry_id = L1 的 id，category_id = L2 的 id（写快照 category_name 用），
        leaf_category_id = 叶子自身 id，
        rank_category_id = 榜单 API 用的完整类目路径「L2,L3,...,叶子」逗号拼接，
            直接传给短视频榜接口即可拉到该叶子专属 TOP200。
        category_name = path[1]（真实二级类目，对齐飞书「二级类目」列语义），
        leaf_name = 叶子自身名（用于 scope_key 区分各叶子）。
    """
    if not path or not leaf_names:
        return []

    # 逐层按名匹配，定位到目标父节点，并沿途收集各层 id。
    # cat_path_ids = path[1:] 各层 id（L2..父节点），短视频榜 API 的 category_id
    # 需要「L2,L3,...,叶子」整条路径逗号拼接（实测：只传叶子 id 返回空，传完整路径
    # 即可直接出该叶子的 TOP200）。
    current_level = raw_options
    parent_node = None
    l1_id = ""
    cat_path_ids: list[str] = []
    for depth, name in enumerate(path):
        found = None
        for node in current_level:
            if _clean_label(node) == name:
                found = node
                break
        if not found:
            logger.warning("resolve_leaf_targets: 第 %d 层未找到 %r，已遍历: %s",
                           depth + 1, name, [_clean_label(n) for n in current_level[:10]])
            return []
        if depth == 0:
            l1_id = _node_id(found)            # path[0] = L1 → industry_id
        else:
            cat_path_ids.append(_node_id(found))  # path[1:] = L2..父节点
        parent_node = found
        current_level = found.get("children") or []

    if not parent_node:
        return []

    cat_path = ",".join(cat_path_ids)          # 如 "1000003282,1000003289"（服装,服装配饰）
    l2_id = cat_path_ids[0] if cat_path_ids else ""
    # 真实二级类目名（path[1]），写入快照的 category_name 列，与大盘语义一致。
    l2_name = path[1] if len(path) >= 2 else path[0]

    # 匹配叶子
    leaf_set = set(leaf_names)
    results: list[dict] = []
    for child in (parent_node.get("children") or []):
        child_name = _clean_label(child)
        if child_name in leaf_set:
            leaf_id = _node_id(child)
            results.append({
                "industry_name": path[0],
                "category_name": l2_name,
                "leaf_name": child_name,
                "industry_id": l1_id,
                "category_id": l2_id,
                "leaf_category_id": leaf_id,
                # 榜单 API 用的完整类目路径 L2,L3,...,叶子（直采该叶子 TOP200）
                "rank_category_id": f"{cat_path},{leaf_id}" if cat_path else leaf_id,
            })
            leaf_set.discard(child_name)

    if leaf_set:
        logger.warning("resolve_leaf_targets: 未匹配到的叶子: %s（可能已更名或无权限）",
                       sorted(leaf_set))

    logger.info("resolve_leaf_targets: 路径 %s → 匹配 %d/%d 个叶子: %s",
                " > ".join(path), len(results), len(leaf_names),
                [r["leaf_name"] for r in results])
    return results
