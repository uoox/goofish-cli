# amcu 后端（本 fork 与上游的唯一差异）

## 为什么要换掉 Playwright

上游用 Playwright 驱动**系统 Chrome**，并且在 `core/browser.py` 的注释里写明了
三条刻意的设计取舍：

1. 用系统 Chrome（`channel="chrome"`）而不是 bundled chromium，因为后者指纹太裸；
2. 每次调用开一个**全新的临时 profile**，避开 Chrome 的 `SingletonLock` 并发冲突，
   登录态靠 `add_cookies` 现灌；
3. **默认 headful**——实测无头 Chrome 即便 `channel=chrome` 也会被闲鱼判"非法访问"，
   必须以真实窗口启动才能过。

这三条串起来的后果是：每搜一个关键词，就等于新开一台"陌生电脑"、装上你的
cookie、弹一个可见窗口去访问闲鱼、然后销毁。一轮 20 多个关键词的监控任务，
就是 20 多次弹窗，而且每次在风控眼里都是一台新设备在登录。

amcu 后端换了个思路：**不新建浏览器，直接用你已经开着、已经登录的那个 Chrome。**

| | Playwright 后端（上游） | amcu 后端（本 fork 默认） |
|---|---|---|
| 浏览器 | 每次新起一个系统 Chrome 进程 | 复用你当前开着的 Chrome |
| profile | 每次一个空白临时 profile | 你的真实 profile |
| 登录态 | 把 cookie 灌进空白 profile | 本来就登录着，不用灌 |
| 可见性 | **默认弹窗**（无头会被风控） | 标签开在 amcu 后台窗口，不抢焦点 |
| 风控画像 | 每次都像一台新设备首次登录 | 与日常浏览完全同源 |
| 反检测 | 需要藏 `navigator.webdriver` | 不需要，本来就没有 |

## 怎么实现的

`core/amcu.py` 把 `amcu browser <verb> --json` 包装成一组与 Playwright `Page`
**同签名**的对象，所以 `commands/search`、`commands/item/view`、`core/refresh`、
`core/qr_login` 这些调用方**一行都没改**：

```
page.goto / evaluate / wait_for_timeout / wait_for_selector / query_selector
page.locator(css).nth(n).click() / page.frames / page.add_init_script
page.context.cookies()
frame.url / evaluate / wait_for_selector / wait_for_load_state / get_by_text
```

几个值得说明的点：

- **`evaluate` 的语义对齐**。Playwright 的规则是"求值结果是函数就调用它、把参数传进去，
  否则当表达式取值"。`wrap_js()` 用页面里的 `typeof` 判定复刻了这条规则，而不是
  用正则去猜，因此 `(limit) => …` 箭头函数和 `window.scrollTo(...)` 语句都能原样跑。
  amcu 的 eval 会 await Promise，所以上游的异步提取脚本不用改。
- **iframe 能进去**。`amcu browser eval --frame <frameId>` 支持按真实 frameId 求值，
  所以 `page.frames` / `frame.evaluate` / `content_frame()` 都是真的，
  `core/refresh.py` 点 passport 登录框里"快速进入"的路径得以保留。
- **`context.cookies()` 在这里语义更实在**。既然跑的就是你真实的 Chrome，
  "上下文的 cookie"就是那个 profile 的 cookie。实现上两路合并：页面里的
  `document.cookie`（能拿到刚被 `Set-Cookie` 刷新的 `_m_h5_tk`，但读不到 httpOnly）
  ＋ `browser_cookie3` 从 Chrome 磁盘库直读（能拿到 `cookie2` 等 httpOnly 字段），
  同名以页面里的为准。这样 `core/refresh.py` 一行不用改也能在 amcu 下工作。
- **`add_init_script` 是空实现**。它在上游只用来藏 `navigator.webdriver`；
  真实浏览器没有这个标记，不需要藏。

## 前置条件

- macOS，装好 [amcu](https://github.com/uoox/amcu)，且 `amcu browser doctor` 显示
  bridge 扩展连着浏览器（一次性设置：`amcu browser install`，然后在
  `chrome://extensions` 里 Load unpacked）。
- 在那个浏览器里正常登录 https://www.goofish.com。**这就是登录方式**——
  amcu 后端下不需要 `auth login --qr` 扫码，你自己登着就行。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GOOFISH_BROWSER_BACKEND` | `amcu` | 设成 `playwright` 切回上游路线（需 `pip install goofish-cli[playwright]`） |
| `GOOFISH_AMCU_BIN` | `amcu` | amcu 可执行路径 |
| `GOOFISH_AMCU_SESSION` | `goofish-cli` | amcu 会话名，与你手动用的 default 会话隔离 |
| `GOOFISH_AMCU_TIMEOUT` | `60` | 单条 amcu 命令的秒级上限 |
| `GOOFISH_AMCU_BROWSER` | 自动 | 指定 `chrome` / `edge` / `brave` 等 |

## 与上游同步

`.github/workflows/upstream-sync.yml` 每天拉一次上游：干净合并且
`scripts/check_amcu_backend.py` 守门通过、测试通过，才直接推 main；
一旦有冲突、或上游把 Playwright 改回默认后端、或测试挂了，就不动 main，
转成一个 PR 等人处理。守门脚本检查的就是本文档开头那条既定差异。
