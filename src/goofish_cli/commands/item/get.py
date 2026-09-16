"""item get — 查询商品详情。接口 mtop.taobao.idle.pc.detail v1.0（只读）

字段提取与 `item view`（浏览器路径）对齐：详情主体在 `data.itemDO` /
`data.sellerDO` / `itemDO.itemLabelExtList`，而不是 `data.trackParams`
（trackParams 只剩埋点字段，title/price/seller 早已搬走，只读它会拿到全空）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from goofish_cli.core import NotFoundError, Session, Strategy, command
from goofish_cli.core.errors import GoofishError
from goofish_cli.core.mtop import call


def _normalize_item_id(value: Any) -> str:
    s = str(value or "").strip()
    if not re.fullmatch(r"\d+", s):
        raise GoofishError(f"item_id 必须是数字，收到：{value!r}")
    return s


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _find_label(labels: list[dict[str, Any]], name: str) -> str:
    """itemLabelExtList 里按 propertyText（分类/品牌/成色/功能状态…）找文案。"""
    for label in labels:
        if isinstance(label, dict) and _clean(label.get("propertyText")) == name:
            return _clean(label.get("text"))
    return ""


def _extract(data: dict[str, Any], item_id: str) -> dict[str, Any]:
    """从 mtop.taobao.idle.pc.detail 的 data 段抽详情字段（与 item view 同构）。"""
    item = data.get("itemDO") or {}
    seller = data.get("sellerDO") or {}
    labels = item.get("itemLabelExtList") or []
    images = [
        e.get("url")
        for e in (item.get("imageInfos") or [])
        if isinstance(e, dict) and e.get("url")
    ]
    seller_id = str(seller.get("sellerId") or "")

    price = _clean("¥" + str(item.get("soldPrice") or item.get("defaultPrice") or ""))
    if price == "¥":
        price = ""

    return {
        "item_id": _clean(item.get("itemId") or item_id),
        "title": _clean(item.get("title")),
        "description": _clean(item.get("desc")),
        "price": price,
        "original_price": _clean(item.get("originalPrice")),
        "want_count": str(item.get("wantCnt") or ""),
        "collect_count": str(item.get("collectCnt") or ""),
        "browse_count": str(item.get("browseCnt") or ""),
        "status": _clean(item.get("itemStatusStr")),
        "condition": _find_label(labels, "成色"),
        "brand": _find_label(labels, "品牌"),
        "category": _find_label(labels, "分类"),
        "location": _clean(seller.get("publishCity") or seller.get("city")),
        "seller_name": _clean(seller.get("nick") or seller.get("uniqueName")),
        "seller_id": seller_id,
        "seller_score": _clean(seller.get("xianyuSummary")),
        "reply_ratio_24h": _clean(seller.get("replyRatio24h")),
        "reply_interval": _clean(seller.get("replyInterval")),
        "seller_url": f"https://www.goofish.com/personal?userId={seller_id}" if seller_id else "",
        "image_count": str(len(images)),
        "image_urls": images,
    }


@command(
    namespace="item",
    name="get",
    description="查询闲鱼商品详情（只读，API 直签；字段与 item view 对齐）",
    strategy=Strategy.COOKIE,
    columns=[
        "item_id", "title", "price", "condition", "brand", "location",
        "seller_name", "want_count", "browse_count",
    ],
)
def get(item_id: str, raw: bool = False) -> dict[str, Any]:
    normalized = _normalize_item_id(item_id)
    session = Session.load()
    resp = call(
        session,
        api="mtop.taobao.idle.pc.detail",
        data={"itemId": normalized},
        version="1.0",
        spm_cnt="a21ybx.item.0.0",
    )
    data = resp.get("data", {}) or {}
    result = _extract(data, normalized)
    if not result.get("title"):
        raise NotFoundError(
            f"商品 {normalized} 无标题：可能已删除/下架/不存在",
            raw=resp,
        )
    result["_image_urls_json"] = json.dumps(result["image_urls"], ensure_ascii=False)
    if raw:
        result["raw"] = resp
    return result


__test__ = {
    "_normalize_item_id": _normalize_item_id,
    "_clean": _clean,
    "_find_label": _find_label,
    "_extract": _extract,
}
