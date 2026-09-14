"""amcu 后端的纯单测：不碰真实浏览器，全部打桩 `amcu.run`。

覆盖三件容易出错的事：
1. `wrap_js` 是否与 Playwright `page.evaluate(js, arg)` 同语义（函数就调用、否则取值）
2. amcu 的结构化错误（ok:false）是否被翻成 AmcuError
3. `goofish_page()` 的后端分发是否默认走 amcu、且能被环境变量切回
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from goofish_cli.core import amcu, browser
from goofish_cli.core.amcu import AmcuError, AmcuPage, wrap_js

# ── wrap_js：与 Playwright 同语义 ──────────────────────────────────────────

def test_wrap_js_calls_function_with_arg():
    out = wrap_js("(limit) => limit * 2", 21)
    assert "typeof __gf === 'function'" in out
    assert "__gf(21)" in out


def test_wrap_js_passes_undefined_when_no_arg():
    assert "__gf(undefined)" in wrap_js("() => 1")


def test_wrap_js_serializes_non_scalar_arg():
    out = wrap_js("(spec) => spec", {"selector": "a[href]", "index": 1})
    assert json.dumps({"selector": "a[href]", "index": 1}, ensure_ascii=False) in out


def test_wrap_js_keeps_unicode_readable():
    assert "快速进入" in wrap_js("(t) => t", "快速进入")


def test_wrap_js_handles_plain_expression():
    """`window.scrollTo(...)` 这类语句不是函数，必须原样求值而不是被调用。"""
    out = wrap_js("window.scrollTo(0, document.body.scrollHeight)")
    assert "window.scrollTo(0, document.body.scrollHeight)" in out
    assert "typeof __gf === 'function'" in out


# ── run()：错误翻译 ────────────────────────────────────────────────────────

def _completed(stdout: str = "", stderr: str = "", code: int = 0):
    class P:
        pass

    p = P()
    p.stdout, p.stderr, p.returncode = stdout, stderr, code
    return p


def test_run_returns_result_payload():
    payload = json.dumps({"ok": True, "result": {"value": 42}})
    with patch.object(amcu.subprocess, "run", return_value=_completed(payload)):
        assert amcu.run("eval", "--js", "42") == {"value": 42}


def test_run_raises_on_structured_error_from_stderr():
    """amcu 的错误走 stderr、退出码仍是 0——不能当成成功。"""
    err = json.dumps({"ok": False, "code": "no_current_tab", "message": "no tab"})
    with (
        patch.object(amcu.subprocess, "run", return_value=_completed("", err)),
        pytest.raises(AmcuError) as exc,
    ):
        amcu.run("eval", "--js", "1")
    assert exc.value.code == "no_current_tab"


def test_run_raises_when_amcu_missing():
    with (
        patch.object(amcu.subprocess, "run", side_effect=FileNotFoundError),
        pytest.raises(AmcuError) as exc,
    ):
        amcu.run("status")
    assert exc.value.code == "amcu_not_found"


def test_run_raises_on_non_json_output():
    with (
        patch.object(amcu.subprocess, "run", return_value=_completed("not json")),
        pytest.raises(AmcuError) as exc,
    ):
        amcu.run("status")
    assert exc.value.code == "bad_json"


# ── Page：把 amcu 的返回摊成 Playwright 形状 ───────────────────────────────

async def test_page_evaluate_unwraps_value():
    with patch.object(amcu, "run", return_value={"value": {"items": [1, 2]}}) as m:
        page = AmcuPage("123")
        assert await page.evaluate("(n) => n", 7) == {"items": [1, 2]}
    args = m.call_args[0]
    assert args[0] == "eval"
    assert "--tab" in args and "123" in args


async def test_page_frames_lists_iframes():
    frames = {"frames": [
        {"frameId": 0, "url": "https://www.goofish.com/login"},
        {"frameId": 7024, "url": "https://passport.goofish.com/mini_login.htm?x=1"},
    ]}
    with patch.object(amcu, "run", return_value=frames):
        page = AmcuPage("1")
        got = page.frames
    assert [f.frame_id for f in got] == [0, 7024]
    assert "passport" in got[1].url


async def test_query_selector_returns_none_when_absent():
    with patch.object(amcu, "run", return_value={"value": None}):
        assert await AmcuPage("1").query_selector("#nope") is None


async def test_content_frame_matches_iframe_by_src():
    page = AmcuPage("1")
    handle = amcu.AmcuElementHandle(page, "#box", "https://passport.goofish.com/mini_login.htm?a=1")
    frames = {"frames": [
        {"frameId": 0, "url": "https://www.goofish.com/login"},
        {"frameId": 7024, "url": "https://passport.goofish.com/mini_login.htm?b=2"},
    ]}
    with patch.object(amcu, "run", return_value=frames):
        frame = await handle.content_frame()
    assert frame is not None and frame.frame_id == 7024


async def test_locator_click_raises_when_element_missing():
    with (
        patch.object(amcu, "run", return_value={"value": {"ok": False, "reason": "not-found"}}),
        pytest.raises(AmcuError),
    ):
        await AmcuPage("1").locator("[class*=arrow]").nth(1).click()


async def test_add_init_script_is_noop():
    """真实浏览器不需要反检测注入；空实现但必须保留签名。"""
    assert await AmcuPage("1").add_init_script("whatever") is None


# ── 后端分发 ───────────────────────────────────────────────────────────────

def test_default_backend_is_amcu(monkeypatch):
    monkeypatch.delenv("GOOFISH_BROWSER_BACKEND", raising=False)
    assert browser.backend() == browser.BACKEND_AMCU


def test_backend_can_be_switched_to_playwright(monkeypatch):
    monkeypatch.setenv("GOOFISH_BROWSER_BACKEND", "playwright")
    assert browser.backend() == browser.BACKEND_PLAYWRIGHT


def test_unknown_backend_falls_back_to_amcu(monkeypatch):
    monkeypatch.setenv("GOOFISH_BROWSER_BACKEND", "selenium")
    assert browser.backend() == browser.BACKEND_AMCU


async def test_goofish_page_uses_amcu_without_playwright_installed(monkeypatch):
    """核心回归：默认路径下不能 import playwright（本 fork 不装它也要能跑）。"""
    monkeypatch.delenv("GOOFISH_BROWSER_BACKEND", raising=False)
    seen = {}

    with patch.object(amcu, "run") as m:
        def fake(verb, *args, timeout=None):
            seen.setdefault("verbs", []).append(verb)
            if verb == "tab" and "--new" in args:
                return {"tab": {"id": 999}}
            return {}
        m.side_effect = fake
        async with browser.goofish_page() as page:
            assert isinstance(page, AmcuPage)
            assert page.tab_id == "999"

    assert seen["verbs"][0] == "tab"      # 开后台标签页
    assert "tab" in seen["verbs"]          # 且退出时关掉
