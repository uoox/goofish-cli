"""amcu 后端 —— 在**用户自己的、已经登录的 Chrome** 里跑闲鱼页面。

为什么要有这个后端（与 `browser.py` 的 Playwright 路线对比）：

| | Playwright 后端 | amcu 后端 |
|---|---|---|
| 浏览器 | 每次调用新起一个系统 Chrome 进程 | 复用用户当前开着的 Chrome |
| profile | 每次一个空白 tmp profile | 用户的真实 profile |
| 登录态 | 把 cookie 灌进空白 profile | 本来就是登录着的，不用灌 |
| 可见性 | **默认 headful，窗口会弹出来** | 标签开在 amcu 的后台窗口，不抢焦点 |
| 风控画像 | 每次都像一台"新设备"首次登录 | 与用户日常浏览完全同源 |

也就是说：Playwright 路线要弹窗，恰恰是因为它开的是一个"陌生"的浏览器，
必须用真实窗口去骗过闲鱼的无头指纹检测；而 amcu 走的是用户本来就在用的那个
浏览器，不存在这个问题——既不弹窗，风控画像也更干净。

实现方式：把 `amcu browser <verb> --json` 子命令包装成一组与 Playwright
`Page` **同签名**的对象，因此 `commands/search`、`commands/item/view`、
`core/refresh`、`core/qr_login` 这些调用方**一行都不用改**。

覆盖到的 Playwright 接口（调用方实际用到的全集）：
    page.goto / evaluate / wait_for_timeout / wait_for_selector / query_selector
    page.locator(css).nth(n).click() / page.frames / page.add_init_script
    page.context.cookies()
    frame.url / evaluate / wait_for_selector / wait_for_load_state / get_by_text

环境变量：
    GOOFISH_AMCU_BIN       amcu 可执行路径（默认 `amcu`，走 PATH）
    GOOFISH_AMCU_SESSION   amcu 会话名（默认 `goofish-cli`，与用户的 default 会话隔离）
    GOOFISH_AMCU_TIMEOUT   单条 amcu 命令的秒级上限（默认 60）
    GOOFISH_AMCU_BROWSER   指定浏览器（chrome / edge / brave…；默认让 amcu 自己挑）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger

from goofish_cli.core.errors import GoofishError

_UNSET = object()


def amcu_bin() -> str:
    return os.environ.get("GOOFISH_AMCU_BIN") or "amcu"


def _session() -> str:
    return os.environ.get("GOOFISH_AMCU_SESSION") or "goofish-cli"


def _default_timeout() -> int:
    try:
        return int(os.environ.get("GOOFISH_AMCU_TIMEOUT", "60"))
    except ValueError:
        return 60


class AmcuError(GoofishError):
    """amcu 子命令返回 ok:false，或进程本身起不来。"""

    def __init__(self, code: str, message: str, next_steps: list[str] | None = None):
        self.code = code
        self.next_steps = next_steps or []
        super().__init__(f"[amcu:{code}] {message}")


def is_available() -> bool:
    """amcu 在 PATH 上，且 bridge 扩展连着一个浏览器。"""
    if not shutil.which(amcu_bin()) and not os.path.exists(amcu_bin()):
        return False
    try:
        run("status")
    except Exception:  # noqa: BLE001
        return False
    return True


def run(verb: str, *args: str, timeout: int | None = None) -> dict[str, Any]:
    """跑一条 `amcu browser <verb> … --json`，返回 result 字典。

    amcu 的结构化错误走 stderr 且退出码为 0，所以两个流都要看。
    """
    cmd = [amcu_bin(), "browser", verb, *args, "--session", _session(), "--json"]
    browser = os.environ.get("GOOFISH_AMCU_BROWSER")
    if browser:
        cmd += ["--browser", browser]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout or _default_timeout()
        )
    except FileNotFoundError as e:
        raise AmcuError(
            "amcu_not_found",
            f"找不到 amcu 可执行文件（{amcu_bin()}）。装好 amcu，或设 GOOFISH_AMCU_BIN。",
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AmcuError("timeout", f"amcu browser {verb} 超时") from e

    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not raw:
        raise AmcuError("empty_output", f"amcu browser {verb} 没有输出（退出码 {proc.returncode}）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AmcuError("bad_json", f"amcu browser {verb} 输出不是 JSON：{raw[:200]}") from e

    if not data.get("ok"):
        raise AmcuError(
            str(data.get("code") or "unknown"),
            str(data.get("message") or raw[:200]),
            data.get("nextSteps"),
        )
    return data.get("result") or {}


async def arun(verb: str, *args: str, timeout: int | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(run, verb, *args, timeout=timeout)


# ── JS 求值 ────────────────────────────────────────────────────────────────

def wrap_js(js: str, arg: Any = _UNSET) -> str:
    """把 Playwright `page.evaluate(js, arg)` 的语义翻译成一段自洽的表达式。

    Playwright 的规则是：js 求值出来**是函数就调用它**（把 arg 传进去），
    不是函数就当表达式取值。调用方两种写法都在用——
    `_EXTRACT_JS` 是 `(limit) => …` 箭头函数，`window.scrollTo(...)` 是语句。
    所以这里不做正则猜测，直接在页面里用 `typeof` 判定，与 Playwright 同构。

    arg 用 json.dumps 内联。amcu 的 eval 只收一段表达式、不收额外参数，
    但它会 await Promise，所以异步提取脚本可以原样跑。
    """
    payload = "undefined" if arg is _UNSET else json.dumps(arg, ensure_ascii=False)
    return (
        "(() => { const __gf = (" + js + ");"
        " return (typeof __gf === 'function') ? __gf(" + payload + ") : __gf; })()"
    )


async def _eval(js: str, arg: Any = _UNSET, *, tab: str, frame_id: int | None = None,
                timeout: int | None = None) -> Any:
    args = ["--js", wrap_js(js, arg), "--tab", tab]
    if frame_id is not None:
        args += ["--frame", str(frame_id)]
    result = await arun("eval", *args, timeout=timeout)
    return result.get("value")


# 在元素上派发一串**真实形状**的鼠标事件。闲鱼翻页箭头是挂了框架 handler 的 div，
# 光调 el.click() 对某些实现不生效，补齐 pointer/mouse 序列更稳。
_CLICK_JS = r"""
(spec) => {
  const el = document.querySelectorAll(spec.selector)[spec.index];
  if (!el) return { ok: false, reason: 'not-found' };
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const opts = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, button: 0 };
  try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    const Ctor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Ctor(type, opts));
  }
  return { ok: true };
}
"""

_QUERY_JS = r"""
(spec) => {
  const el = document.querySelector(spec.selector);
  if (!el) return null;
  const style = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    src: el.getAttribute('src') || '',
    visible: style.visibility !== 'hidden' && style.display !== 'none' && (r.width > 0 || r.height > 0),
  };
}
"""

_TEXT_CLICK_JS = r"""
(spec) => {
  const wanted = spec.text;
  const nodes = Array.from(document.querySelectorAll('button, a, div, span, li, input'));
  const hit = nodes.find((n) => {
    const t = (n.innerText || n.value || '').replace(/\s+/g, ' ').trim();
    return spec.exact ? t === wanted : t.includes(wanted);
  });
  if (!hit) return { ok: false, reason: 'not-found' };
  const r = hit.getBoundingClientRect();
  const opts = { bubbles: true, cancelable: true, composed: true,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    const Ctor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
    hit.dispatchEvent(new Ctor(type, opts));
  }
  return { ok: true };
}
"""


# ── Playwright 同签名对象 ──────────────────────────────────────────────────

class AmcuLocator:
    """`page.locator(css)` 的最小实现：只支持调用方用到的 `.nth(n).click()`。"""

    def __init__(self, page: AmcuPage, selector: str, index: int = 0):
        self._page = page
        self._selector = selector
        self._index = index

    def nth(self, index: int) -> AmcuLocator:
        return AmcuLocator(self._page, self._selector, index)

    @property
    def first(self) -> AmcuLocator:
        return self.nth(0)

    async def click(self, timeout: int | None = None) -> None:
        spec = {"selector": self._selector, "index": self._index}
        out = await _eval(_CLICK_JS, spec, tab=self._page.tab_id,
                          timeout=_ms_to_s(timeout))
        if not (isinstance(out, dict) and out.get("ok")):
            raise AmcuError("element_not_found",
                            f"没找到可点的元素：{self._selector}[{self._index}]")


class AmcuTextLocator:
    """`frame.get_by_text(t, exact=True)` 的最小实现。"""

    def __init__(self, target: AmcuPage | AmcuFrame, text: str, exact: bool):
        self._target = target
        self._text = text
        self._exact = exact

    @property
    def first(self) -> AmcuTextLocator:
        return self

    async def click(self, timeout: int | None = None) -> None:
        out = await self._target.evaluate(
            _TEXT_CLICK_JS, {"text": self._text, "exact": self._exact}
        )
        if not (isinstance(out, dict) and out.get("ok")):
            raise AmcuError("element_not_found", f"没找到文案为 {self._text!r} 的元素")


class AmcuElementHandle:
    """`page.query_selector()` 的返回值。只实现 `content_frame()`。"""

    def __init__(self, page: AmcuPage, selector: str, src: str):
        self._page = page
        self._selector = selector
        self._src = src

    async def content_frame(self) -> AmcuFrame | None:
        """按 iframe 的 src 在 amcu 的 frame 列表里找对应 frame。"""
        if not self._src:
            return None
        key = self._src.split("?")[0]
        for frame in self._page.frames:
            if frame.frame_id == 0:
                continue
            if key and key in frame.url:
                return frame
        return None


class AmcuFrame:
    """一个 frame。frame_id=0 是主文档，其余是 iframe（amcu 的真实 frameId）。"""

    def __init__(self, page: AmcuPage, frame_id: int, url: str):
        self._page = page
        self.frame_id = frame_id
        self.url = url

    async def evaluate(self, js: str, arg: Any = _UNSET) -> Any:
        return await _eval(js, arg, tab=self._page.tab_id,
                           frame_id=None if self.frame_id == 0 else self.frame_id)

    async def wait_for_load_state(self, state: str = "load", timeout: int | None = None) -> None:
        await _poll(lambda: self.evaluate("document.readyState !== 'loading'"),
                    timeout_ms=timeout or 5000)

    async def wait_for_selector(self, selector: str, timeout: int | None = None,
                                state: str = "visible") -> Any:
        ok = await _poll(
            lambda: self.evaluate("(s) => !!document.querySelector(s)", selector),
            timeout_ms=timeout or 15000,
        )
        if not ok:
            raise AmcuError("timeout", f"等 {selector} 超时（frame {self.frame_id}）")
        return True

    def get_by_text(self, text: str, exact: bool = False) -> AmcuTextLocator:
        return AmcuTextLocator(self, text, exact)


class AmcuContext:
    """`page.context`。

    `cookies()` 的语义在 amcu 下更实在：既然跑的就是用户真实 Chrome，
    "上下文的 cookie"就是那个 profile 的 cookie。所以两路合并——
    页面里的 `document.cookie`（拿得到刚被 Set-Cookie 刷新的 `_m_h5_tk`，
    但读不到 httpOnly）＋ browser_cookie3 从 Chrome 磁盘库直读
    （拿得到 `cookie2` 等 httpOnly 字段）。同名以页面里的为准（更新）。

    这一层做对了，`core/refresh.py` 就能一行不改地在 amcu 下工作：
    它导航到强鉴权页触发服务端下发 cookie，然后从这里读回来。
    """

    def __init__(self, page: AmcuPage):
        self._page = page

    async def cookies(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for rec in await asyncio.to_thread(_disk_cookies):
            merged[rec["name"]] = rec
        for rec in await self._document_cookies():
            merged[rec["name"]] = rec
        return list(merged.values())

    async def _document_cookies(self) -> list[dict[str, Any]]:
        raw = await self._page.evaluate("document.cookie") or ""
        out: list[dict[str, Any]] = []
        for part in str(raw).split(";"):
            if "=" not in part:
                continue
            name, _, value = part.strip().partition("=")
            if name and value:
                out.append({"name": name, "value": value, "domain": ".goofish.com", "path": "/"})
        return out


def _disk_cookies() -> list[dict[str, Any]]:
    """从本机浏览器 cookie 库直读（含 httpOnly）。失败就返回空，不打断流程。"""
    try:
        from goofish_cli.core.browser_cookie import extract_goofish_cookies
        _, records = extract_goofish_cookies(browser=os.environ.get("GOOFISH_AMCU_BROWSER", "auto"))
        return [dict(r) for r in records]
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[amcu] 从浏览器磁盘读 cookie 失败（忽略）：{e}")
        return []


class AmcuPage:
    """Playwright `Page` 的等价物，底下是 amcu 的一个后台标签页。"""

    def __init__(self, tab_id: str):
        self.tab_id = str(tab_id)
        self.context = AmcuContext(self)

    # -- 导航 --
    async def goto(self, url: str, wait_until: str | None = None,
                   timeout: int | None = None) -> None:
        await arun("navigate", "--url", url, "--tab", self.tab_id,
                   timeout=_ms_to_s(timeout))

    async def reload(self) -> None:
        await arun("reload", "--tab", self.tab_id)

    # -- 求值 --
    async def evaluate(self, js: str, arg: Any = _UNSET) -> Any:
        return await _eval(js, arg, tab=self.tab_id)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)

    async def wait_for_selector(self, selector: str, timeout: int | None = None,
                                state: str = "visible") -> Any:
        want_present = state != "hidden"
        ok = await _poll(
            lambda: self._selector_present(selector, want_present),
            timeout_ms=timeout or 15000,
        )
        if not ok:
            raise AmcuError("timeout", f"等 {selector}（state={state}）超时")
        return True

    async def _selector_present(self, selector: str, want_present: bool) -> bool:
        present = bool(await self.evaluate("(s) => !!document.querySelector(s)", selector))
        return present is want_present

    async def query_selector(self, selector: str) -> AmcuElementHandle | None:
        info = await self.evaluate(_QUERY_JS, {"selector": selector})
        if not info:
            return None
        return AmcuElementHandle(self, selector, str(info.get("src") or ""))

    def locator(self, selector: str) -> AmcuLocator:
        return AmcuLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = False) -> AmcuTextLocator:
        return AmcuTextLocator(self, text, exact)

    # -- frames（Playwright 里是同步属性，这里保持同步）--
    @property
    def frames(self) -> list[AmcuFrame]:
        try:
            result = run("frames", "--tab", self.tab_id)
        except AmcuError as e:
            logger.debug(f"[amcu] 取 frame 列表失败：{e}")
            return [AmcuFrame(self, 0, "")]
        return [
            AmcuFrame(self, int(f.get("frameId", 0)), str(f.get("url") or ""))
            for f in result.get("frames") or []
        ]

    @property
    def main_frame(self) -> AmcuFrame:
        return AmcuFrame(self, 0, "")

    async def add_init_script(self, script: str) -> None:
        """Playwright 用它在文档加载前藏 `navigator.webdriver`。

        amcu 后端不需要：跑的是用户自己的 Chrome，本来就没有 webdriver 标记，
        也没有 tmp profile 的裸指纹。空实现即可，保留签名让调用方不用改。
        """
        logger.trace("[amcu] add_init_script 空实现（真实浏览器无需反检测）")


def _ms_to_s(ms: int | None) -> int | None:
    return None if ms is None else max(1, int(ms / 1000))


async def _poll(check, *, timeout_ms: int, interval_ms: int = 300) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        try:
            if await check():
                return True
        except AmcuError:
            pass
        await asyncio.sleep(interval_ms / 1000)
    return False


@asynccontextmanager
async def goofish_page_amcu(
    *,
    viewport: tuple[int, int] = (1440, 900),
    url: str = "about:blank",
) -> AsyncIterator[AmcuPage]:
    """在 amcu 的后台窗口开一个标签页，yield 出 Page，退出时关掉它。

    标签页开在 amcu 自己的后台窗口里——不进用户的标签栏，不抢焦点，
    用户不会看到窗口弹出来，也不会误关掉它。
    """
    result = await arun("tab", "--new", "--url", url)
    tab = result.get("tab") or {}
    tab_id = tab.get("id")
    if tab_id is None:
        raise AmcuError("no_tab", "amcu 没有返回新标签页的 id")
    page = AmcuPage(str(tab_id))
    logger.debug(f"[amcu] 后台标签页 {tab_id} 已开（用户的 Chrome，不弹窗）")
    try:
        yield page
    finally:
        try:
            await arun("tab", "--close", "--tab", page.tab_id, timeout=20)
        except Exception as e:  # noqa: BLE001 — 清理失败不该盖掉主流程的异常
            logger.debug(f"[amcu] 关闭标签页 {page.tab_id} 失败（忽略）：{e}")


__test__ = {
    "wrap_js": wrap_js,
    "_ms_to_s": _ms_to_s,
}
