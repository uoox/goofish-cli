"""纯函数测 item get 的参数校验 + itemDO/sellerDO 字段抽取。

背景：mtop.taobao.idle.pc.detail 的详情主体在 data.itemDO / data.sellerDO，
老实现只读 data.trackParams（埋点字段），title/price/seller 全空。
"""
from __future__ import annotations

import pytest

from goofish_cli.commands.item.get import __test__ as t
from goofish_cli.core.errors import GoofishError


def test_normalize_item_id_accepts_digits():
    f = t["_normalize_item_id"]
    assert f("123456") == "123456"
    assert f(987654321) == "987654321"
    assert f("  42  ") == "42"


@pytest.mark.parametrize("bad", ["", "abc", "123abc", " ", None, "12 34"])
def test_normalize_item_id_rejects_non_digit(bad):
    with pytest.raises(GoofishError):
        t["_normalize_item_id"](bad)


def _fixture_data() -> dict:
    return {
        "itemDO": {
            "itemId": 1045171414271,
            "title": "测试  标题",
            "desc": "第一行\n\n第二行",
            "soldPrice": "199",
            "originalPrice": "299",
            "wantCnt": 14,
            "collectCnt": 8,
            "browseCnt": 347,
            "itemStatusStr": "卖掉了",
            "itemLabelExtList": [
                {"propertyText": "分类", "text": "品牌台机"},
                {"propertyText": "品牌", "text": "Asus/华硕"},
                {"propertyText": "成色", "text": "轻微使用痕迹"},
            ],
            "imageInfos": [
                {"url": "http://img.alicdn.com/a.jpg"},
                {"url": "http://img.alicdn.com/b.jpg"},
                {"major": True},  # 无 url 的条目应被过滤
            ],
        },
        "sellerDO": {
            "sellerId": 1234567890,
            "nick": "卖家昵称",
            "publishCity": "葫芦岛",
            "xianyuSummary": "卖出过179件宝贝",
            "replyRatio24h": "83%",
            "replyInterval": "27分钟",
        },
        "trackParams": {"itemId": "1045171414271"},
    }


def test_extract_reads_item_do_and_seller_do():
    result = t["_extract"](_fixture_data(), "1045171414271")

    assert result["item_id"] == "1045171414271"
    assert result["title"] == "测试 标题"
    assert result["description"] == "第一行 第二行"
    assert result["price"] == "¥199"
    assert result["original_price"] == "299"
    assert result["want_count"] == "14"
    assert result["collect_count"] == "8"
    assert result["browse_count"] == "347"
    assert result["status"] == "卖掉了"
    assert result["condition"] == "轻微使用痕迹"
    assert result["brand"] == "Asus/华硕"
    assert result["category"] == "品牌台机"
    assert result["location"] == "葫芦岛"
    assert result["seller_name"] == "卖家昵称"
    assert result["seller_id"] == "1234567890"
    assert result["seller_score"] == "卖出过179件宝贝"
    assert result["reply_ratio_24h"] == "83%"
    assert result["reply_interval"] == "27分钟"
    assert result["seller_url"].endswith("userId=1234567890")
    assert result["image_count"] == "2"
    assert result["image_urls"] == [
        "http://img.alicdn.com/a.jpg",
        "http://img.alicdn.com/b.jpg",
    ]


def test_extract_tolerates_missing_sections():
    result = t["_extract"]({}, "42")

    assert result["item_id"] == "42"
    assert result["title"] == ""
    assert result["price"] == ""
    assert result["seller_url"] == ""
    assert result["image_urls"] == []


def test_extract_price_without_yuan_when_absent():
    data = _fixture_data()
    data["itemDO"].pop("soldPrice")
    data["itemDO"]["defaultPrice"] = False  # 接口里 defaultPrice 可能是 bool
    assert t["_extract"](data, "1")["price"] == ""


def test_find_label_returns_empty_for_unknown():
    assert t["_find_label"]([{"propertyText": "品牌", "text": "x"}], "成色") == ""
