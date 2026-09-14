#!/usr/bin/env python3
"""守门脚本：保证这个 fork 的既定差异——**默认后端是 amcu，不是 Playwright**——
不会被上游合并悄悄冲掉。

每次自动合并上游之后都会跑一遍（见 .github/workflows/upstream-sync.yml）：
上游若改动了 `goofish_page()` 或依赖表，把 Playwright 又变回默认路径，
这里会失败，合并结果就不会被推到 main，而是转成一个 PR 等人处理。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def main() -> int:
    browser = (ROOT / "src/goofish_cli/core/browser.py").read_text(encoding="utf-8")
    amcu = ROOT / "src/goofish_cli/core/amcu.py"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    check(amcu.is_file(), "core/amcu.py 不见了——amcu 后端本体丢失")
    check('DEFAULT_BACKEND = BACKEND_AMCU' in browser,
          "core/browser.py 里 DEFAULT_BACKEND 不再是 amcu")
    check('goofish_page_amcu' in browser,
          "core/browser.py 的 goofish_page() 不再分发到 amcu 后端")
    check('def backend()' in browser,
          "core/browser.py 缺少 backend() 选择器")

    # Playwright 只允许出现在 optional-dependencies 里，不能回到必装依赖
    deps_block = pyproject.split("[project.optional-dependencies]")[0]
    check('"playwright' not in deps_block,
          "playwright 又变回 [project].dependencies 里的必装依赖了")

    if FAILURES:
        print("amcu 后端守门检查未通过：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        print("\n上游大概改动了浏览器层。请人工合并，保持 amcu 为默认后端。")
        return 1

    print("✓ amcu 仍是默认后端，Playwright 仍是可选依赖")
    return 0


if __name__ == "__main__":
    sys.exit(main())
