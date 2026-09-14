"""浏览器上下文。**默认走 amcu 后端**（见 `core/amcu.py`），Playwright 作为可选回退。

本 fork 与上游的唯一行为差异就在这里：`goofish_page()` 默认把活交给 amcu，
在用户自己那个已经登录的 Chrome 里开后台标签页——不弹窗、不新建 profile。
`GOOFISH_BROWSER_BACKEND=playwright` 可切回上游原路线（需 `pip install
goofish-cli[playwright]`）。下面保留的是上游的 Playwright 实现，原样不动，
以便跟上游合并时冲突最小。

──────────── 以下为上游原始说明 ────────────

Playwright 浏览器上下文 —— 吸纳 OpenCLI 的浏览器自动化路线。

设计要点（用户明确要求）：
1. **用系统 Chrome**（`channel="chrome"`），不用 playwright 自带的 bundled chromium——
   bundled chromium 的 UA / CDP 指纹太"裸"，是风控高危目标；系统 Chrome 是真实用户
   每天在用的可执行，配上真实 cookies 后基本等同正常浏览。
2. **每次调用独立 profile**（`~/.goofish-cli/profiles/chrome-<tmp>/`）：Chrome 一个
   `user_data_dir` 同时只能被一个进程打开（`SingletonLock`），固定路径会让并发调用
   （MCP 同时跑多个 tool / 用户手动并发）直接 ProfileInUse 起不来。所以每次 tmp 一个
   profile，退出清理——代价是首次启动多几百 ms，收益是天然支持并发。登录态不需要靠
   profile 持久化，我们每次用 `add_cookies` 从 `Session.load()` 灌。cookie 来源复用
   `Session.load()` 的三级兜底 —— `cookies.json` → `browser_cookie3` 自动从本机
   Chrome 抓 → `AuthRequiredError`，不在这里重复实现。
3. **默认 headful**：实测 headless chrome 的指纹（即便 channel=chrome）仍会被闲鱼判
   「非法访问」，返回"请使用正常浏览器访问"。要通过就必须以窗口模式启动。CI / 无桌面
   场景可 `GOOFISH_HEADLESS=1` 切回 headless（代价是可能被风控）。

同步命令怎么用：用 `asyncio.run(...)` 驱动本模块的 async 上下文（参考 list_chats
`--watch-secs` 的 `asyncio.run(collect_session_cids(...))` 模式）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from goofish_cli.core.cookie_types import CookieRecord
from goofish_cli.core.session import Session

PROFILES_PARENT = Path.home() / ".goofish-cli" / "profiles"

BACKEND_AMCU = "amcu"
BACKEND_PLAYWRIGHT = "playwright"
DEFAULT_BACKEND = BACKEND_AMCU


def backend() -> str:
    """选后端。默认 amcu；`GOOFISH_BROWSER_BACKEND=playwright` 切回上游路线。"""
    value = (os.environ.get("GOOFISH_BROWSER_BACKEND") or DEFAULT_BACKEND).strip().lower()
    return value if value in (BACKEND_AMCU, BACKEND_PLAYWRIGHT) else DEFAULT_BACKEND

# 淘系签名链路 cookie（历史上跨 .taobao.com），其余默认落 .goofish.com。
# 仅用于 legacy flat dict 格式（无 domain 信息时）的窄映射兜底。
# 有 domain 的 records 直接用其 domain，不再广播。
_TAOBAO_COOKIE_NAMES = {"_m_h5_tk", "_m_h5_tk_enc", "x5sec", "sgcookie", "cookie2", "_tb_token_"}


def _legacy_domain(name: str) -> str:
    """按 cookie 名字反推归属域。仅用于 domain='' 的 legacy 记录。"""
    return ".taobao.com" if name in _TAOBAO_COOKIE_NAMES else ".goofish.com"


def _cookies_to_playwright(cookies) -> list[dict[str, Any]]:
    """把 flat dict 或 records list 转成 playwright add_cookies 需要的列表。

    有 domain 的 records 单域注入（无广播）。domain='' 的 legacy 记录
    走 _legacy_domain 窄映射分发到单域。记录中的 path 会被注入（不再硬编码 '/'）。
    """
    from .session import _coerce_records
    records = _coerce_records(cookies)
    now = int(__import__("time").time())
    expires = now + 7 * 24 * 3600
    out: list[dict[str, Any]] = []
    for r in records:
        if not r.get("value"):
            continue
        domain = r["domain"] or _legacy_domain(r["name"])
        out.append({
            "name": r["name"],
            "value": r["value"],
            "domain": domain,
            "path": r.get("path") or "/",   # ← 用记录的 path
            "expires": expires,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })
    return out


def _load_cookies_from_session() -> list[CookieRecord]:
    """返回 session.cookie_records（已带 domain），fallback 展平 jar（生成 domain='' records）。"""
    session = Session.load()
    if session.cookie_records:
        return session.cookie_records
    # 边缘：手工构造 Session（无 cookie_records）时回退
    return [
        {"name": name, "value": value, "domain": ""}
        for name, value in session.http.cookies.items()
        if value
    ]


@asynccontextmanager
async def goofish_page(
    *,
    headless: bool | None = None,
    viewport: tuple[int, int] = (1440, 900),
    cookies: dict | list | None = None,
) -> AsyncIterator[Any]:
    """启动系统 Chrome（独立 tmp profile）+ 灌 cookie，yield 出一个 `Page`。

    `cookies` 可选：显式传入（flat dict 或带 domain 的 records list）时直接用；
    不传则走 `Session.load()` 三级兜底。自动刷 `_m_h5_tk` 的调用方需要用内存里
    当前 session 的 cookie records 而非磁盘快照（可能已被改）。

    用法：
        async with goofish_page() as page:
            await page.goto("https://www.goofish.com/search?q=foo")
            ...
    """
    if backend() == BACKEND_AMCU:
        from goofish_cli.core.amcu import goofish_page_amcu

        # amcu 跑的是用户真实、已登录的 Chrome：cookies / headless 两个参数
        # 在这条路上没有意义（没有空白 profile 要灌，也没有窗口要藏）。
        if cookies is not None:
            logger.debug("[amcu] 忽略显式 cookies —— 用的是浏览器自身的登录态")
        async with goofish_page_amcu(viewport=viewport) as page:
            yield page
        return

    from playwright.async_api import async_playwright

    if headless is None:
        # 默认 headful。CI 用户显式 GOOFISH_HEADLESS=1 切回（可能触发风控）。
        headless = os.environ.get("GOOFISH_HEADLESS") == "1"

    PROFILES_PARENT.mkdir(parents=True, exist_ok=True)
    # 每次调用独立 profile 目录，避开 Chrome SingletonLock 并发冲突
    profile_dir = Path(tempfile.mkdtemp(prefix="chrome-", dir=str(PROFILES_PARENT)))
    if cookies is None:
        cookies = _load_cookies_from_session()
    pw_cookies = _cookies_to_playwright(cookies)

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=headless,
                viewport={"width": viewport[0], "height": viewport[1]},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            try:
                await context.add_cookies(pw_cookies)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"注入 cookie 失败：{e}")

            page = context.pages[0] if context.pages else await context.new_page()
            # 隐藏 webdriver 特征（CDP 检测 cookie 外的最后一道门）
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            try:
                yield page
            finally:
                await context.close()
    finally:
        # 清理 tmp profile。忽略错误（进程被 kill 时残留目录由用户手动清 ~/.goofish-cli/profiles/）
        shutil.rmtree(profile_dir, ignore_errors=True)


async def auto_scroll(page: Any, times: int = 2, pause_ms: int = 800) -> None:
    """模拟 OpenCLI 的 `page.autoScroll({times})`：滚到底 N 次触发懒加载。"""
    for _ in range(times):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
    await page.evaluate("window.scrollTo(0, 0)")
