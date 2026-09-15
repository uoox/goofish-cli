"""search — 搜索闲鱼商品。对标 OpenCLI `xianyu/search.js`。

思路：打开 `https://www.goofish.com/search?q=xxx` 让页面自己渲染，autoScroll 触发
懒加载，再在 page context 里跑 DOM 选择器提卡片。**不走 mtop 直签**：
- search 没对外 API，只有 HTML 卡片 + 动态加载
- 浏览器真实渲染天然抗风控

分页（2026-08 实测）：搜索页**不是无限滚动**——窄查询滚动到底卡片数不再增长；
翻页靠 DOM 里的 `search-pagination-container`（宽查询下 1..50 页），点击右箭头后
SPA 内部重渲染、URL 不变，`?page=N` URL 参数被服务端忽略。所以跨页抓取 = 点击
翻页箭头 + 按 item_id 去重累积，终止条件：达到 --pages 上限 / 右箭头 disabled /
连续一页无新增（防御）。

字段参考 OpenCLI：`item_id / rank / title / price / original_price / condition /
brand / location / badge / url / extra`。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from goofish_cli.core import Strategy, command
from goofish_cli.core.browser import auto_scroll, goofish_page
from goofish_cli.core.errors import AuthRequiredError, GoofishError

# limit 是**跨页总上限**（去重后条数）。站点每页 30 卡，50 页满配远超此值，
# 200 是给 MCP 调用方的运行时护栏（每页 ~4s，200 条 ≈ 7 页 ≈ 40s）。
MAX_LIMIT = 200
DEFAULT_LIMIT = 20
MAX_PAGES = 50  # 与站点分页控件一致（实测 1..50）
# 0 卡片 + 非 empty/blocked 时判定为疑似瞬时登录墙（同一 cookie 注入实测时好时坏），
# 总尝试次数（含首次）。重试前短暂退避，避免连续两枪都打在风控瞬间。
AUTH_WALL_ATTEMPTS = 2
# 点击翻页后的兜底等待；主等待靠"首卡片 id 变化"事件，这里只兜渲染卡住的底
PAGE_STABLE_MS = 2000


def _should_retry(payload: dict[str, Any]) -> bool:
    """零卡片且明确 requiresAuth 时才重试（瞬时登录墙兜底）。

    #28 合并后的契约：其他零卡片形态（未知页面结构等）不重试，
    直接交给 `_raise_for_failed_page` 按语义抛错。
    """
    return not payload.get("items") and bool(payload.get("requiresAuth"))


def _normalize_limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return min(MAX_LIMIT, max(1, n))


def _normalize_pages(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return min(MAX_PAGES, max(1, n))


def _item_id_from_url(url: str) -> str:
    m = re.search(r"[?&]id=(\d+)", url or "")
    return m.group(1) if m else ""


def _build_search_url(query: str) -> str:
    from urllib.parse import quote
    return f"https://www.goofish.com/search?q={quote(query)}"


# 页面上下文里跑的 JS。抽出来做常量方便测试（`__test__` 导出）。
_EXTRACT_JS = r"""
(limit) => (async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (predicate, timeoutMs = 8000) => {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (predicate()) return true;
      await wait(150);
    }
    return false;
  };

  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const sel = {
    card: 'a[href*="/item?id="]',
    title: '[class*="row1-wrap-title"], [class*="main-title"]',
    attrs: '[class*="row2-wrap-cpv"] span[class*="cpv--"]',
    priceWrap: '[class*="price-wrap"]',
    priceNum: '[class*="number"]',
    priceDec: '[class*="decimal"]',
    priceDesc: '[class*="price-desc"] [title], [class*="price-desc"] [style*="line-through"]',
    sellerWrap: '[class*="row4-wrap-seller"]',
    sellerText: '[class*="seller-text"]',
    badge: '[class*="credit-container"] [title], [class*="credit-container"] span',
  };

  await waitFor(() => {
    const bodyText = document.body?.innerText || '';
    return Boolean(
      document.querySelector(sel.card)
      || /请先登录|登录后|验证码|安全验证|异常访问/.test(bodyText)
      || /暂无相关宝贝|未找到相关宝贝|没有找到/.test(bodyText)
    );
  });

  const bodyText = document.body?.innerText || '';
  const requiresAuth = /请先登录|登录后/.test(bodyText);
  const blocked = /验证码|安全验证|异常访问/.test(bodyText);
  const empty = /暂无相关宝贝|未找到相关宝贝|没有找到/.test(bodyText);

  const items = Array.from(document.querySelectorAll(sel.card))
    .slice(0, limit)
    .map((card) => {
      const href = card.href || card.getAttribute('href') || '';
      const title = clean(card.querySelector(sel.title)?.textContent || '');
      const attrs = Array.from(card.querySelectorAll(sel.attrs))
        .map((n) => clean(n.textContent || ''))
        .filter(Boolean);
      const priceWrap = card.querySelector(sel.priceWrap);
      const priceNumber = clean(priceWrap?.querySelector(sel.priceNum)?.textContent || '');
      const priceDecimal = clean(priceWrap?.querySelector(sel.priceDec)?.textContent || '');
      const location = clean(card.querySelector(sel.sellerWrap)?.querySelector(sel.sellerText)?.textContent || '');
      const originalPriceNode = card.querySelector(sel.priceDesc);
      const badgeNode = card.querySelector(sel.badge);

      return {
        title,
        url: href,
        price: clean('¥' + priceNumber + priceDecimal).replace(/^¥\s*$/, ''),
        original_price: clean(originalPriceNode?.getAttribute('title') || originalPriceNode?.textContent || ''),
        condition: attrs[0] || '',
        brand: attrs[1] || '',
        extra: attrs.slice(2).join(' | '),
        location,
        badge: clean(badgeNode?.getAttribute('title') || badgeNode?.textContent || ''),
      };
    })
    .filter((it) => it.title && it.url);

  return { requiresAuth, blocked, empty, items, bodyPreview: bodyText.slice(0, 500) };
})()
"""

# 翻页控件状态：右箭头是否可用 + 分页盒里的最大页码（实测 1..50，"..." 是非数字盒）
_PAGINATION_JS = r"""
() => {
  const boxes = [...document.querySelectorAll('[class*="page-box"]')]
    .map((e) => (e.textContent || '').trim())
    .filter((t) => /^\d+$/.test(t))
    .map(Number);
  const arrows = [...document.querySelectorAll('[class*="pagination-arrow-container"]')];
  const right = arrows.length ? arrows[arrows.length - 1] : null;
  return {
    hasNext: Boolean(right) && !right.hasAttribute('disabled'),
    totalPages: boxes.length ? Math.max(...boxes) : null,
  };
}
"""

# 翻页是 SPA 重渲染（URL 不变）。点完右箭头等"首卡片 id 变化"，最多 8s；
# 超时返回 false（大概率是最后一页重渲染失败或内容未变，交给上层去重兜底）。
_WAIT_PAGE_CHANGE_JS = r"""
(prevFirstId) => new Promise((resolve) => {
  const start = Date.now();
  const tick = () => {
    const first = document.querySelector('a[href*="/item?id="]');
    const id = first ? ((first.href || '').match(/id=(\d+)/) || [])[1] : null;
    if (id && id !== prevFirstId) return resolve(true);
    if (Date.now() - start > 8000) return resolve(false);
    setTimeout(tick, 200);
  };
  tick();
})
"""


def _first_card_id(items: list[dict[str, Any]]) -> str:
    """当前页首卡 id——翻页等待的"内容已变化"基准。"""
    for it in items:
        item_id = _item_id_from_url(it.get("url", ""))
        if item_id:
            return item_id
    return ""


def _raise_for_failed_page(payload: dict[str, Any]) -> None:
    """首页级失败（0 卡片）按原语义抛错；翻页途中的失败由调用方优雅终止。"""
    items = payload.get("items") or []
    # "登录后" 在页脚也会出现——只有在"没拿到卡片 && 命中关键词"时才判定 auth 失败
    if not items and payload.get("requiresAuth"):
        raise AuthRequiredError("www.goofish.com 搜索结果页要求登录，cookies 可能失效")
    if not items and payload.get("blocked"):
        raise GoofishError("搜索页返回验证码/安全验证（触发风控），稍后重试或换账号")
    if not items and not payload.get("empty"):
        preview = (payload.get("bodyPreview") or "")[:200]
        raise GoofishError(
            f"未在搜索页上解析到任何卡片，可能 DOM 结构已变。"
            f"页面文案预览：{preview!r}"
        )


async def _walk_pages(
    page: Any,
    items: list[dict[str, Any]],
    seen: set[str],
    fetched_pages: int,
    pages: int,
    limit: int,
) -> tuple[int, int | None, str]:
    """翻页状态机：点右箭头 → 等重渲染 → 去重累积。

    返回 (fetched_pages, total_pages, stopped_reason)。任何翻页途中的
    浏览器异常（SPA 重渲染销毁执行上下文等）都被吞掉并优雅终止——
    调用方拿到已累积的部分结果，stopped_reason 说明终止原因。

    锚点（cur_first_id）始终取**未过滤**的下一页 payload 首卡：
    fresh 是去重后的列表，若下一页首卡与上一页重复，它会被过滤掉，
    用 fresh 的首卡做锚点会与 DOM 实况脱节，导致 page-change 等待
    假阳性、提前终止（review P1-2）。
    """
    anchor_id = _first_card_id(items) or ""
    total_pages: int | None = None
    stopped_reason = "pages_reached"

    while fetched_pages < pages and len(items) < limit:
        try:
            pag = await page.evaluate(_PAGINATION_JS)
        except Exception:  # noqa: BLE001 — 执行上下文销毁等，保留已抓结果
            return fetched_pages, total_pages, "error"
        # 结构突变（None / 非dict）同样按优雅终止处理，不能 AttributeError 穿透
        if not isinstance(pag, dict):
            return fetched_pages, total_pages, "error"

        if total_pages is None and pag.get("totalPages") is not None:
            total_pages = pag["totalPages"]
        if not pag.get("hasNext"):
            return fetched_pages, total_pages, "last_page"

        try:
            arrow = page.locator('[class*="pagination-arrow-container"]').nth(1)
            await arrow.click(timeout=5000)
            # 等待"首卡 id 不再等于上一页 DOM 首卡"。changed=False 是等待超时
            # （重渲染大概率失败）；先看提取结果再定性。
            changed = await page.evaluate(_WAIT_PAGE_CHANGE_JS, anchor_id)
            await page.wait_for_timeout(PAGE_STABLE_MS)
            nxt = await page.evaluate(_EXTRACT_JS, MAX_LIMIT)
        except Exception:  # noqa: BLE001 — 同上，部分结果优先
            return fetched_pages, total_pages, "error"
        if not isinstance(nxt, dict):
            return fetched_pages, total_pages, "error"

        # 锚点更新为**未过滤** payload 的首卡（= 本页 DOM 实际首卡）。
        # fresh 是去重后的列表：若本页首卡与上一页重复，会被过滤掉，
        # 用 fresh 的首卡会让下一轮等待基准与 DOM �脱节（review P1-2）。
        nxt_items = nxt.get("items") or []
        anchor_id = _first_card_id(nxt_items) or anchor_id
        # item_id 是输出的稳定主键（调用方按它去重/取详情）。解析不出数字 id
        # 的卡片（?id=abc 等）跨页会重复追加（seen 只记非空 id），直接跳过。
        nxt_items = [it for it in nxt_items if _item_id_from_url(it.get("url", ""))]
        fresh = [
            it for it in nxt_items if _item_id_from_url(it.get("url", "")) not in seen
        ]
        if not fresh:
            if not nxt_items and nxt.get("blocked"):
                return fetched_pages, total_pages, "blocked"
            return fetched_pages, total_pages, "no_new" if changed else "stale"

        for it in fresh:
            item_id = _item_id_from_url(it.get("url", ""))
            if item_id:
                seen.add(item_id)
            items.append(it)
        fetched_pages += 1

    if len(items) >= limit:
        return fetched_pages, total_pages, "limit"
    return fetched_pages, total_pages, stopped_reason


async def _run(query: str, limit: int, pages: int) -> dict[str, Any]:
    url = _build_search_url(query)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    fetched_pages = 0
    total_pages: int | None = None

    async with goofish_page() as page:
        # ---- 第 1 页（保留瞬时登录墙重试：整页重新导航）----
        payload: dict[str, Any] | None = None
        for attempt in range(1, AUTH_WALL_ATTEMPTS + 1):
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            await auto_scroll(page, times=2)
            raw = await page.evaluate(_EXTRACT_JS, MAX_LIMIT)
            if not isinstance(raw, dict):
                raise GoofishError("搜索页返回结构非预期")
            payload = raw
            # 拿到卡片 / 明确的空结果 / 明确的风控页都不需要重试
            if not _should_retry(raw):
                break
            if attempt < AUTH_WALL_ATTEMPTS:
                await asyncio.sleep(1.5)

        assert payload is not None
        _raise_for_failed_page(payload)

        # item_id 是输出的稳定主键：解析不出数字 id 的卡片直接跳过，
        # 否则同一张坏卡跨页会重复追加（seen 只记非空 id）
        for it in payload.get("items") or []:
            item_id = _item_id_from_url(it.get("url", ""))
            if not item_id:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append(it)
        fetched_pages = 1

        # ---- 翻页：点右箭头 → 等重渲染 → 去重累积（状态机，见 _walk_pages）----
        fetched_pages, total_pages, stopped_reason = await _walk_pages(
            page, items, seen, fetched_pages, pages, limit
        )

        # 循环提前退出（到 limit / 末页）时补一次终态读取
        if total_pages is None:
            try:
                pag = await page.evaluate(_PAGINATION_JS)
                total_pages = pag.get("totalPages")
            except Exception:  # noqa: BLE001 — 终态读取失败不影响部分结果
                total_pages = None

    items = items[:limit]
    return {
        "items": [
            {"rank": i + 1, "item_id": _item_id_from_url(it.get("url", "")), **it}
            for i, it in enumerate(items)
        ],
        "total": len(items),
        "pages_fetched": fetched_pages,
        "pages_total": total_pages,
        "stopped_reason": stopped_reason,
        "query": "",
    }


@command(
    namespace="search",
    name="items",
    description="搜索闲鱼商品（浏览器路径，抗风控；--pages 跨页抓取）",
    strategy=Strategy.COOKIE,
    columns=["rank", "item_id", "title", "price", "condition", "brand", "location", "badge", "url"],
)
def search(query: str, limit: int = DEFAULT_LIMIT, pages: int = 1) -> dict[str, Any]:
    q = str(query).strip()
    result = asyncio.run(_run(q, _normalize_limit(limit), _normalize_pages(pages)))
    result["query"] = q
    return result


__test__ = {
    "MAX_LIMIT": MAX_LIMIT,
    "MAX_PAGES": MAX_PAGES,
    "AUTH_WALL_ATTEMPTS": AUTH_WALL_ATTEMPTS,
    "_normalize_limit": _normalize_limit,
    "_normalize_pages": _normalize_pages,
    "_build_search_url": _build_search_url,
    "_item_id_from_url": _item_id_from_url,
    "_should_retry": _should_retry,
    "_first_card_id": _first_card_id,
    "_walk_pages": _walk_pages,
}
