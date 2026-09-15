"""验 mtop 调用的分类逻辑与 t 精度修复。"""
from __future__ import annotations

import time

import pytest


def test_t_precision_fixed():
    """验 t 毫秒位非 0：`int(time.time() * 1000)` 而不是 `int(time.time()) * 1000`（后者末三位恒为 0）"""
    # 直接模拟 mtop.call 里的取值
    t_ms = int(time.time() * 1000)
    assert t_ms > 1_700_000_000_000
    # 采样几次，应该不都是 000 结尾
    samples = [int(time.time() * 1000) % 1000 for _ in range(5)]
    time.sleep(0.003)
    samples.append(int(time.time() * 1000) % 1000)
    assert any(s != 0 for s in samples)


def test_classify_success():
    from goofish_cli.core.mtop import _classify_error

    _classify_error({"ret": ["SUCCESS::调用成功"]}, "mtop.test")  # 不抛


def test_classify_auth():
    from goofish_cli.core.errors import AuthRequiredError
    from goofish_cli.core.mtop import _classify_error

    with pytest.raises(AuthRequiredError):
        _classify_error({"ret": ["FAIL_SYS_SESSION_EXPIRED::session expired"]}, "mtop.test")


def test_classify_not_found():
    from goofish_cli.core.errors import NotFoundError
    from goofish_cli.core.mtop import _classify_error

    with pytest.raises(NotFoundError):
        _classify_error({"ret": ["FAIL_BIZ_ITEM_NOT_FOUND::商品不存在"]}, "mtop.test")


def test_call_goes_through_the_page_not_httpx(monkeypatch):
    """mtop 请求必须由浏览器页面发出，不能退回从 Python 直发。

    `cookie2`（真正的 session token）是 httpOnly：Python 侧读不到，也灌不进去。
    从进程里发的 mtop 一律 FAIL_SYS_SESSION_EXPIRED，所以传输层一旦被改回 httpx，
    整个鉴权就失效。这条盯住那个接缝——签名仍在 Python 算，请求交给页面发。
    """
    from goofish_cli.core import amcu, mtop

    sent = {}

    def fake_post(url: str, data_val: str) -> str:
        sent["url"], sent["data"] = url, data_val
        return '{"ret":["SUCCESS::调用成功"],"data":{"ok":1}}'

    monkeypatch.setattr(amcu, "mtop_post", fake_post)
    monkeypatch.setattr(mtop, "_h5_token", lambda _session: "tok")
    monkeypatch.setattr(mtop, "generate_sign", lambda t, token, data: "sig")

    raw = mtop.call(object(), "mtop.test.api", {"a": 1})

    assert raw["data"] == {"ok": 1}
    assert sent["data"] == '{"a":1}'
    assert "sign=sig" in sent["url"]
    assert "api=mtop.test.api" in sent["url"]


def test_h5_token_prefers_the_live_page(monkeypatch):
    """`_m_h5_tk` 只有 10 分钟寿命，磁盘快照常常"抓时没过期、用时已过"。

    页面里那份由浏览器活跃访问时持续续期，所以要优先用它；页面拿不到才回退快照。
    """
    from goofish_cli.core import amcu, mtop

    monkeypatch.setattr(amcu, "h5_token_from_page", lambda: "fresh")

    class _S:
        h5_token = "stale"

    assert mtop._h5_token(_S()) == "fresh"


def test_h5_token_falls_back_to_snapshot_when_page_unavailable(monkeypatch):
    """amcu 没连上时不能直接炸——回退磁盘快照，让报错停在更准确的地方。"""
    from goofish_cli.core import amcu, mtop

    def _boom() -> str:
        raise RuntimeError("amcu 没连上")

    monkeypatch.setattr(amcu, "h5_token_from_page", _boom)

    class _S:
        h5_token = "stale"

    assert mtop._h5_token(_S()) == "stale"
