"""浏览器上下文。**只走 amcu 后端**（见 `core/amcu.py`）。

本 fork 与上游的核心差异就在这里：上游每次调用都新起一个系统 Chrome、开一个可见
窗口、灌一份 cookie 进空白 profile；本 fork 直接用用户自己那个已经登录的 Chrome，
标签页开在 amcu 的后台窗口里——不弹窗、不抢焦点、不新建 profile。

为什么不再保留上游那条路线作为可选回退：它的登录态要靠把 cookie 灌进空白 profile，
而 `cookie2`（真正的 session token）是 httpOnly，Python 侧根本读不到，灌不进去，
于是那条路上的 mtop 调用必然 `FAIL_SYS_SESSION_EXPIRED`。既然它拿不到有效登录态，
留着只会是个看起来能用、实际必然失败的陷阱。mtop 现在统一走
`core/amcu.py::mtop_post`（在页面里 fetch，cookie2 由浏览器自动附带）。
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger

BACKEND_AMCU = "amcu"
DEFAULT_BACKEND = BACKEND_AMCU


def backend() -> str:
    """选后端。本 fork 只有 amcu 一条路。

    `GOOFISH_BROWSER_BACKEND` 曾用于切回上游路线，现在只要设成别的值就告警并忽略
    ——静默忽略会让人以为切过去了，然后困惑于为什么行为没变。
    """
    value = (os.environ.get("GOOFISH_BROWSER_BACKEND") or "").strip().lower()
    if value and value != BACKEND_AMCU:
        logger.warning(
            f"GOOFISH_BROWSER_BACKEND={value} 已忽略：本 fork 只支持 amcu 后端"
        )
    return BACKEND_AMCU


@asynccontextmanager
async def goofish_page(
    *,
    headless: bool | None = None,
    viewport: tuple[int, int] = (1440, 900),
    cookies: dict | list | None = None,
) -> AsyncIterator[Any]:
    """在用户自己的 Chrome 里开一个后台标签页，yield 出一个 `Page`。

    `headless` / `cookies` 两个参数保留只为不破坏上游调用方的签名，在这条路上没有
    意义：跑的就是用户真实的、已登录的浏览器，没有空白 profile 要灌，也没有窗口要藏。

    用法：
        async with goofish_page() as page:
            await page.goto("https://www.goofish.com/search?q=foo")
            ...
    """
    from goofish_cli.core.amcu import goofish_page_amcu

    if cookies is not None:
        logger.debug("[amcu] 忽略显式 cookies —— 用的是浏览器自身的登录态")
    if headless:
        logger.debug("[amcu] 忽略 headless —— 标签页本来就开在后台窗口里")
    async with goofish_page_amcu(viewport=viewport) as page:
        yield page


async def auto_scroll(page: Any, times: int = 2, pause_ms: int = 800) -> None:
    """模拟 OpenCLI 的 `page.autoScroll({times})`：滚到底 N 次触发懒加载。"""
    for _ in range(times):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
    await page.evaluate("window.scrollTo(0, 0)")
