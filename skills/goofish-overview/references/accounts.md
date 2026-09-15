# 账号模型

## 登录态存储

- 磁盘：`~/.goofish-cli/cookies.json`（可通过 `GOOFISH_COOKIES_PATH` 覆盖）
- 内存：`Session.http.cookies`（requests 的 CookieJar）
- `auth_status` 返回 `{unb, tracknick, nick, valid, h5_token_exp}`

## 关键 cookie 字段

| cookie | 用途 | 失效表现 |
|---|---|---|
| `_m_h5_tk` | h5 mtop 签名（**10 分钟** TTL） | `FAIL_SYS_TOKEN_EXOIRED`（拼写来自服务端非笔误） |
| `unb` | 用户 id | — |
| `cookie2` | 真正的 session token | `FAIL_SYS_SESSION_EXPIRED` |
| `sgcookie / _tb_token_` | 完整 session 三件套 | 同上 |
| `x5sec` | 风控通行（可选） | RGV587 风险时需要重导 |
| `tracknick` | 昵称跟踪 | — |

## 登录态怎么来的（本 fork）

mtop 请求在 goofish.com **页面内**发出，`cookie2` 由浏览器自动附带——既然不需要
读它，上游那套"自动续命"（`refresh_cookies_via_browser` 点"快速进入"）和扫码兜底
（`auth login --qr`）就都没有存在理由，已连同 Playwright 整个删除。

```
mtop 调用 → core/mtop.py 算签名（_m_h5_tk 从页面实时读，不用磁盘快照）
   ↓
core/amcu.py::mtop_post → 在闲鱼页面内 fetch(credentials:'include')
   ↓
浏览器自动带上 httpOnly 的 cookie2 → 返回 JSON
```

**登录方式就是你在自己的 Chrome 里登录闲鱼。** 掉登录的表现是 `auth status` 回
`valid: false`、mtop 一律 `FAIL_SYS_SESSION_EXPIRED`；注意**搜索仍然正常**
（闲鱼匿名也能搜），所以"搜索能用"不能拿来判断登录还在——这点很容易误判。
**Agent 不主动调 auth_login**。

## 环境变量

| 变量 | 作用 |
|---|---|
| `GOOFISH_COOKIES_PATH` | 自定义 cookies.json 路径 |
| `GOOFISH_HEADLESS=1` | 浏览器 headless（会触发风控，慎用） |
| `GOOFISH_NO_CHROME_BOOTSTRAP=1` | 禁止从本机 Chrome 自动抓 cookie |
