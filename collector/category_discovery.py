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
    let current = fiber;
    for (let i = 0; i < 50 && current; i++) {
        const props = current.memoizedProps || current.pendingProps || {};
        if (props.options && Array.isArray(props.options) && props.options.length > 0
            && props.options[0].label) {
            options = props.options;
            break;
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


async def discover_categories(page: Page, target_l1_names: list[str]) -> dict:
    """
    从 React fiber 中提取类目树，按目标一级类目过滤。

    Returns
    -------
    dict
        {
          "智能家居": [
            {"name": "五金/工具", "category_id": "1000003462", "industry_id": "6"},
            ...
          ],
          ...
        }
    """
    logger.info("开始类目树自动发现（React fiber），目标 L1: %s", target_l1_names)

    # 确保级联选择器已展开（触发组件渲染）
    await _ensure_cascader_rendered(page)
    await page.wait_for_timeout(1000)

    # 从 React fiber 提取完整数据
    result = await page.evaluate(_REACT_EXTRACT_JS)

    if result.get("error"):
        logger.error("React fiber 提取失败: %s", result["error"])
        return {}

    raw_options = result.get("options", [])
    logger.info("从 React fiber 提取到 %d 个一级类目", len(raw_options))

    target_set = set(target_l1_names)
    tree: dict[str, list[dict]] = {}

    for l1 in raw_options:
        l1_name = l1.get("label") or l1.get("cate_name", "")
        l1_id = l1.get("value") or l1.get("cate_id", "")

        if l1_name not in target_set:
            logger.debug("跳过 L1: %s (不在目标中)", l1_name)
            continue

        l2_children = l1.get("children", [])
        l2_list = []
        for l2 in l2_children:
            l2_name = l2.get("label") or l2.get("cate_name", "")
            l2_id = l2.get("value") or l2.get("cate_id", "")
            if not l2_name or not l2_id:
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
            logger.warning("  [%s]: 无二级类目", l1_name)

    logger.info("类目树发现完成: %d 个 L1, 共 %d 个 L2",
                len(tree), sum(len(v) for v in tree.values()))
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


def load_category_tree(cache_path: str) -> Optional[dict]:
    """从缓存文件加载类目树。"""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            tree = json.load(f)
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
