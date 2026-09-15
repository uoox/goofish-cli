"""统一 mtop 调用模板。抽出 headers/params/sign 流程。

要点：
- `t` 取真实毫秒：`int(time.time() * 1000)`，而不是 `int(time.time()) * 1000`（后者末三位恒为 0）
- 自动识别风控关键字抛 RiskControlError
- 自动识别令牌过期抛 AuthRequiredError
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from goofish_cli.core.errors import (
    AuthRequiredError,
    GoofishError,
    NotFoundError,
    RiskControlError,
    SignError,
)
from goofish_cli.core.session import USER_AGENT, Session
from goofish_cli.core.sign import generate_sign

APP_KEY = "34839810"
MTOP_HOST = "https://h5api.m.goofish.com"

_RISK_KEYWORDS = (
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
    "哎哟喂",
    "/punish",
)
_AUTH_KEYWORDS = (
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EMPTY",
    "令牌过期",
    "FAIL_SYS_ILLEGAL_ACCESS",
)


def default_headers() -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://www.goofish.com/",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
    }


def call(
    session: Session,
    api: str,
    data: dict[str, Any] | list[Any] | str,
    *,
    version: str = "1.0",
    spm_cnt: str = "a21ybx.home.0.0",
    extra_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """调用 mtop 接口。返回原始 JSON。失败抛 GoofishError 子类。

    请求**在浏览器页面里发出**（见 `core/amcu.py::mtop_post`），不是从 Python 发：
    `cookie2` 是 httpOnly，Python 侧永远拿不到，只有让浏览器自己带上它才能通过鉴权。
    因此这里也不再需要上游那套 `_auto_refresh`（Playwright 点"快速进入"刷 cookie）——
    页面本来就是活的登录态，`_m_h5_tk` 每次现取，没有"快照过期"这回事。
    """
    url = f"{MTOP_HOST}/h5/{api}/{version}/"
    t_ms = str(int(time.time() * 1000))
    data_val = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))

    token = _h5_token(session)
    if not token:
        raise AuthRequiredError(
            "_m_h5_tk 拿不到：请确认浏览器已登录 https://www.goofish.com，"
            "且 `amcu browser doctor` 显示扩展已连接"
        )
    sign = generate_sign(t_ms, token, data_val)

    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": t_ms,
        "sign": sign,
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": spm_cnt,
    }
    if extra_params:
        params.update(extra_params)

    # 惰性 import：amcu 里反过来要用 core.browser，顶层互引会绕成循环。
    from goofish_cli.core.amcu import mtop_post

    raw = json.loads(mtop_post(f"{url}?{urlencode(params)}", data_val))
    _classify_error(raw, api)
    return raw


def _h5_token(session: Session) -> str:
    """取 `_m_h5_tk` 的签名段，优先问浏览器页面。

    磁盘快照里的 `_m_h5_tk` 只有 10 分钟寿命，常常"抓的时候没过期、用的时候已过"。
    页面里的那份是浏览器活跃访问时被 Set-Cookie 持续续期的，永远新鲜。
    页面取不到（amcu 没连上等）才退回 session 里的快照，让错误信息更准。
    """
    try:
        from goofish_cli.core.amcu import h5_token_from_page
        token = h5_token_from_page()
        if token:
            return token
    except Exception as e:  # noqa: BLE001 — 退回磁盘快照，由调用方报错
        logger.debug(f"从页面取 _m_h5_tk 失败（回退磁盘快照）：{e}")
    return session.h5_token


def _classify_error(raw: dict[str, Any], api: str) -> None:
    """根据 ret 字段分类抛异常。成功则不抛。"""
    ret = raw.get("ret") or []
    ret_str = " | ".join(ret) if isinstance(ret, list) else str(ret)
    if not ret_str or "SUCCESS" in ret_str:
        return

    for kw in _RISK_KEYWORDS:
        if kw in ret_str:
            raise RiskControlError(
                f"[{api}] 触发风控：{ret_str}",
                raw=raw,
            )
    for kw in _AUTH_KEYWORDS:
        if kw in ret_str:
            raise AuthRequiredError(f"[{api}] 登录态失效：{ret_str}", raw=raw)
    if "ILLEGAL_REQUEST" in ret_str or "sign" in ret_str.lower():
        raise SignError(f"[{api}] 签名错误：{ret_str}", raw=raw)
    if "NOT_FOUND" in ret_str or "不存在" in ret_str:
        raise NotFoundError(f"[{api}] 未找到：{ret_str}", raw=raw)
    raise GoofishError(f"[{api}] 调用失败：{ret_str}", raw=raw)
