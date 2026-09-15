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

| | Playwright 路线（上游） | amcu 路线（本 fork，唯一路线） |
|---|---|---|
| 浏览器 | 每次新起一个系统 Chrome 进程 | 复用你当前开着的 Chrome |
| profile | 每次一个空白临时 profile | 你的真实 profile |
| 登录态 | 把 cookie 灌进空白 profile | 本来就登录着，不用灌 |
| 可见性 | **默认弹窗**（无头会被风控） | 标签开在 amcu 后台窗口，不抢焦点 |
| 风控画像 | 每次都像一台新设备首次登录 | 与日常浏览完全同源 |
| 反检测 | 需要藏 `navigator.webdriver` | 不需要，本来就没有 |

## 为什么上游那条路线被整个删掉，而不是留作可选回退

因为它**拿不到有效登录态**，留着只会是个"看起来能切、切过去必然失败"的陷阱。

闲鱼的 mtop 接口认的是 `cookie2`（真正的 session token）。这个 cookie 是
**httpOnly**：

- `document.cookie` 读不到它；
- `browser_cookie3` 从 Chrome 磁盘库直读，在本机被 Chrome 的 app-bound 加密挡住；
- `amcu browser network` 只暴露 URL + status，不给请求头。

也就是说，**Python 进程无论如何都凑不齐 cookie2**，那么"把 cookie 灌进空白
profile"这个前提就不成立，那条路上的每一次 mtop 调用都会回
`FAIL_SYS_SESSION_EXPIRED`。上游为了绕开这点写了两套机制——`core/refresh.py`
（点 passport 的"快速进入"免密刷新）和 `core/qr_login.py`（扫码重新登录），
本质都是"想方设法把 cookie2 弄到手"。

本 fork 把前提取消了：**不去读 cookie2，让浏览器自己带上它。**

mtop 请求改成在 goofish.com 页面内用 `fetch(credentials:'include')` 发出
（`core/amcu.py::mtop_post`），cookie2 作为同站 cookie 由浏览器自动附加，
我们全程不需要看见它。这也正是真实闲鱼网页的做法，风控画像与日常浏览同源。

于是 `refresh.py`、`qr_login.py`、`auth login --qr`、`GOOFISH_BROWSER_BACKEND`
开关、以及 `playwright` 这个可选依赖，全部失去存在理由，一并删除。

### 签名仍在 Python 算

只有**传输**搬进了页面，签名没有：`t`/`sign`/`appKey` 这些参数照旧由
`core/mtop.py` 算好拼进 URL（`core/sign.py` 走 execjs）。页面只负责把请求发出去。

`_m_h5_tk`（h5 签名用的 token）改为**从页面实时读**（`h5_token_from_page()`）。
它只有 10 分钟寿命、靠浏览器活跃访问时 `Set-Cookie` 续期，从磁盘快照里读经常是
"抓的时候没过期、用的时候已过"——上游那套 refresh 有一半就是在跟这个赛跑。
直接问页面就永远是新鲜的。

## 怎么实现的

`core/amcu.py` 把 `amcu browser <verb> --json` 包装成一组与上游 `Page`
**同签名**的对象，所以 `commands/search`、`commands/item/view` 这些调用方
**一行都没改**：

```
page.goto / evaluate / wait_for_timeout / wait_for_selector / query_selector
page.locator(css).nth(n).click() / page.frames / page.add_init_script
page.context.cookies()
frame.url / evaluate / wait_for_selector / wait_for_load_state / get_by_text
```

几个值得说明的点：

- **`evaluate` 的语义对齐**。上游的规则是"求值结果是函数就调用它、把参数传进去，
  否则当表达式取值"。`wrap_js()` 用页面里的 `typeof` 判定复刻了这条规则，而不是
  用正则去猜，因此 `(limit) => …` 箭头函数和 `window.scrollTo(...)` 语句都能原样跑。
  amcu 的 eval 会 await Promise，所以异步提取脚本（以及页内 `fetch`）不用改写。
- **mtop 传输标签页是进程内复用的**。每条 mtop 都新开标签页要多花 3~6s 导航，还平白
  多出一堆站内请求招风控。`_mtop_tab()` 开一次、整个进程复用，标签页被关掉会自动重开。
- **`add_init_script` 是空实现**。它在上游只用来藏 `navigator.webdriver`；
  真实浏览器没有这个标记，不需要藏。

## 前置条件

- macOS，装好 [amcu](https://github.com/uoox/amcu)，且 `amcu browser doctor` 显示
  bridge 扩展连着浏览器（一次性设置：`amcu browser install`，然后在
  `chrome://extensions` 里 Load unpacked）。
- **在那个浏览器里正常登录 https://www.goofish.com 。这就是登录方式**——
  没有 `auth login --qr`，你自己登着就行。登录态掉了的表现是 `goofish auth status`
  回 `valid: false`、mtop 一律 `FAIL_SYS_SESSION_EXPIRED`，而搜索仍然正常
  （闲鱼匿名也能搜，所以**搜索能用不代表登录还在**，这点很容易误判）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GOOFISH_AMCU_BIN` | `amcu` | amcu 可执行路径 |
| `GOOFISH_AMCU_SESSION` | `goofish-cli` | amcu 会话名，与你手动用的 default 会话隔离 |
| `GOOFISH_AMCU_TIMEOUT` | `60` | 单条 amcu 命令的秒级上限 |
| `GOOFISH_AMCU_BROWSER` | 自动 | 指定 `chrome` / `edge` / `brave` 等 |

`GOOFISH_BROWSER_BACKEND` 已废弃：设成 amcu 以外的值只会告警并被忽略
（静默忽略会让人以为切过去了，然后困惑于行为为什么没变）。

## 与上游同步

`.github/workflows/upstream-sync.yml` 每天拉一次上游：干净合并且
`scripts/check_amcu_backend.py` 守门通过、测试通过，才直接推 main；
一旦有冲突、或上游把 Playwright 重新引入、或测试挂了，就不动 main，
转成一个 PR 等人处理。守门脚本检查三件事：amcu 仍是唯一后端、mtop 仍走页内
fetch、源码里没有 `import playwright`（只拦真实使用，注释里提到它不算——
那些对比说明正是代码为什么这么写的依据）。
