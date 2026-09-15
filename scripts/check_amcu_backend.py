#!/usr/bin/env python3
"""守门脚本：保证这个 fork 的既定差异——**浏览器层只走 amcu**——不被上游合并冲掉。

每次自动合并上游之后都会跑一遍（见 .github/workflows/upstream-sync.yml）：
上游若把 Playwright 改回默认路径、或把它重新塞进依赖表、或把 mtop 改回从 Python
发请求，这里就会失败，合并结果不会被推到 main，而是转成一个 PR 等人处理。

为什么 mtop 必须走页内 fetch：`cookie2`（真正的 session token）是 httpOnly，
Python 侧读不到也灌不进去，从进程里发的 mtop 请求一律 FAIL_SYS_SESSION_EXPIRED。
只有让请求在浏览器页面内发出、由浏览器自动附带 cookie2，鉴权才过得去。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def main() -> int:
    src = ROOT / "src/goofish_cli"
    browser = (src / "core/browser.py").read_text(encoding="utf-8")
    mtop = (src / "core/mtop.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    check((src / "core/amcu.py").is_file(), "core/amcu.py 不见了——amcu 后端本体丢失")
    check("goofish_page_amcu" in browser, "core/browser.py 的 goofish_page() 不再分发到 amcu")
    check("mtop_post" in mtop, "core/mtop.py 不再用 amcu 页内 fetch 发请求（cookie2 会带不上）")

    # Playwright 必须彻底消失：依赖表里不能有，源码里也不能 import 回来。
    check("playwright" not in pyproject.lower(), "pyproject.toml 里又出现 playwright 依赖")
    # 只拦**真的用起来**（import / async_playwright 调用），不拦注释里提到它：
    # amcu shim 之所以长成现在这样，依据就是"与上游接口同签名"，那些对比说明是
    # 代码为什么这么写的唯一线索，按字面禁词清掉只会让后来人看不懂。
    usage = re.compile(r"^[ \t]*(?:import|from)[ \t]+playwright\b|async_playwright[ \t]*\(", re.M)
    offenders = sorted(
        p.relative_to(ROOT).as_posix()
        for p in src.rglob("*.py")
        if usage.search(p.read_text(encoding="utf-8"))
    )
    check(not offenders, f"源码里又 import 了 playwright：{', '.join(offenders)}")

    if FAILURES:
        print("amcu 后端守门检查未通过：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        print("\n上游大概改动了浏览器层。请人工合并，保持 amcu 为唯一后端。")
        return 1

    print("✓ 浏览器层只走 amcu，mtop 走页内 fetch，Playwright 已彻底移除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
