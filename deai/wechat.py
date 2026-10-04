"""微信小程序身份验证：手机号授权（``getPhoneNumber``）。

为什么用手机号
--------------
``X-WX-OPENID`` 是云托管自动注入的，**未登录也能拿到**，所以它天然能唯一区分
用户（匿名额度靠它计数，清一次小程序缓存也重置不了）。但也正因为如此，
「未登录」和「已登录」在身份上**没有任何区别**——光靠 ``wx.login()`` 换 openid
并不能证明用户做了什么，那只是一个谁都能触发的动作。

要区分两档额度，就必须有一个「用户主动做过、且微信背书」的动作。
手机号授权就是这样的动作：用户得真的点一下授权按钮，微信才发 code，
而且这个 code **5 分钟内有效、只能消费一次**。

前提条件（两个都是硬门槛，不满足就别做这个功能）
------------------------------------------------
1. **资质**：该能力「针对**非个人主体，且完成了认证**的小程序开放」。
   个人主体小程序调不通，服务端会返回 ``48001``。
2. **计费**：自 2023-08-28 起**每次成功调用收费 0.03 元**，每个小程序账号有
   1000 次体验额度（正式版/体验版/开发版共用）。额度不足时前端按钮回调里会拿到
   ``e.detail.errno === 1400001``，此时根本不会产生 code，服务端也就看不到请求。

**``getPhoneNumber`` 的 code 与 ``wx.login`` 的 code 不能混用**（官方原文）。
所以接口字段刻意分成 ``phoneCode`` 和 ``code`` 两个，不要串。

两种调用方式
------------
1. **云调用（默认，``WX_OPENAPI_ENABLED=True``）**

   容器内用 **HTTP** 直接请求 ``http://api.weixin.qq.com``，**不带 access_token**。
   旁加载的「开放接口服务」会中转请求并自动注入 ``cloudbase_access_token``。
   好处是**不用维护 token**，也就没有过期刷新、多副本互相顶掉的问题。

   前置条件（缺一不可，否则会报白名单错误）：
     - 控制台-云调用 打开「开放接口服务」开关
     - 在「微信令牌权限配置」里加上路径白名单：``/wxa/business/getuserphonenumber``
       （只填路径，不要带 ``?`` 和参数）
     - **打开开关之后重新构建一次服务版本**——开关是按「版本创建时刻」生效的

   注意：开启「开放接口服务」后，``sns/jscode2session`` 会失效（它本身不带
   access_token，会被当成云调用去查白名单）。所以本项目在云调用模式下
   不再依赖 code2Session 判断身份，openid 直接用 header 里的。

2. **自管 access_token（兜底，``WX_OPENAPI_ENABLED=False``）**

   用 AppID + AppSecret 走 ``/cgi-bin/token`` 换 token，缓存在数据库表
   ``deai_wx_access_token`` 里（**不是进程内存**）。存数据库是因为云托管会
   多副本运行：各副本各存一份内存缓存的话，每次刷新都会把别的副本的 token
   顶失效（微信的 access_token 是全局唯一的，新的一发旧的立即作废）。
   即便如此仍可能撞车，所以调用时遇到 token 失效错误会强制刷新重试一次。

用标准库 ``urllib`` 实现，理由和 ``llm.py`` 一样：云托管镜像越小、冷启动越快。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 直连（HTTPS）域名。自管 token 和 code2Session 走这里。
_CGI_BASE = "https://api.weixin.qq.com"
_PHONE_PATH = "/wxa/business/getuserphonenumber"
_TOKEN_PATH = "/cgi-bin/token"
_CODE2SESSION_PATH = "/sns/jscode2session"

# 提前多久认为 token 过期（秒）。微信给的是 7200 秒。
_TOKEN_REFRESH_MARGIN = 300

# 微信常见错误码 → 人话。都会展示给用户，所以不要写技术术语。
_ERROR_HINTS = {
    -1: "微信服务繁忙，请稍后再试",
    40001: "微信鉴权失败，请稍后再试",
    40013: "小程序 AppID 配置有误",
    40029: "授权凭证已失效，请重新点击授权",
    40125: "小程序 AppSecret 配置有误",
    42001: "微信鉴权过期，请稍后再试",
    45011: "操作太频繁了，请稍后再试",
    47001: "请求参数有误，请重新授权",
    40226: "操作被风控拦截，请稍后再试",
    48001: "该小程序没有手机号快速验证的权限（需非个人主体且已认证）",
}
# 这些错误码代表 token 失效，自管 token 模式下刷新一次再试
_TOKEN_INVALID = {40001, 42001}


class WeChatError(RuntimeError):
    """微信接口调用失败。message 会直接展示给用户。"""


class WeChatNotConfigured(WeChatError):
    """服务端还没配置好，属于部署问题而不是用户问题（接口返回 503）。"""


# --- 配置检查 ---------------------------------------------------------------


def appid_configured() -> bool:
    return bool(settings.WX_APPID)


def is_configured() -> bool:
    """AppID + AppSecret 都配了（自管 token / code2Session 需要）。"""
    return bool(settings.WX_APPID and settings.WX_SECRET)


def phone_ready() -> bool:
    """手机号授权能不能用。

    云调用模式下只要开了开关就行，**连 AppSecret 都不需要**；
    自管 token 模式才要求 AppID + AppSecret 齐全。
    """
    if settings.WX_OPENAPI_ENABLED:
        return True
    return is_configured()


# --- HTTP 底层 --------------------------------------------------------------


def _request(
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    base: str,
    timeout: int | None = None,
) -> dict:
    """向微信接口发一次请求，返回解析后的 JSON。

    统一的错误处理：微信接口出错时 HTTP 状态**仍然是 200**，错误藏在 body 的
    ``errcode`` 里，所以不能只看状态码——这里只负责传输层异常，
    errcode 交给调用方处理（因为不同接口的错误码含义不同）。
    """
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")

    try:
        with urllib.request.urlopen(
            request, timeout=timeout or settings.WX_LOGIN_TIMEOUT
        ) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (socket.timeout, TimeoutError) as exc:
        raise WeChatError("连接微信服务器超时，请稍后再试") from exc
    except urllib.error.HTTPError as exc:
        raise WeChatError(f"微信服务器返回异常（HTTP {exc.code}），请稍后再试") from exc
    except urllib.error.URLError as exc:
        # 云调用模式下最常见的失败原因就是这里：容器里没有开放接口服务，
        # 请求发不出去。把排查方向直接写进错误信息里。
        hint = ""
        if settings.WX_OPENAPI_ENABLED:
            hint = "（如果用云调用，请确认控制台已开启「开放接口服务」并重新构建版本）"
        raise WeChatError(f"连接微信服务器失败{hint}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WeChatError("微信服务器返回的内容无法解析") from exc


def _raise_for_errcode(payload: dict) -> None:
    errcode = payload.get("errcode")
    if errcode:
        hint = _ERROR_HINTS.get(int(errcode), "微信接口调用失败，请稍后再试")
        raise WeChatError(f"{hint}（errcode={errcode}）")


# --- 自管 access_token（仅 WX_OPENAPI_ENABLED=False 时用）-------------------


def _fetch_access_token() -> tuple[str, int]:
    payload = _request(
        _TOKEN_PATH,
        params={
            "grant_type": "client_credential",
            "appid": settings.WX_APPID,
            "secret": settings.WX_SECRET,
        },
        base=_CGI_BASE,
    )
    _raise_for_errcode(payload)
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise WeChatError("微信没有返回 access_token，请检查 AppID / AppSecret")
    return token, int(payload.get("expires_in") or 7200)


def get_access_token(force: bool = False) -> str:
    """取 access_token，带数据库缓存。多副本共享，避免互相顶掉。"""
    from .models import WxAccessToken

    now = timezone.now()
    row = WxAccessToken.objects.filter(pk=1).first()
    if row and not force and row.expires_at > now + timedelta(seconds=60):
        return row.token

    token, expires_in = _fetch_access_token()
    expires_at = now + timedelta(seconds=max(60, expires_in - _TOKEN_REFRESH_MARGIN))
    WxAccessToken.objects.update_or_create(
        pk=1, defaults={"token": token, "expires_at": expires_at}
    )
    logger.info("已刷新微信 access_token，有效期至 %s", expires_at)
    return token


# --- 手机号 -----------------------------------------------------------------


def _normalize(phone: str) -> str:
    """取纯数字，并去掉中国大陆的 ``86`` 区号。

    微信回包里 ``phoneNumber`` 带区号（``+86 13800138000``）、``purePhoneNumber``
    不带。同一个人的号码不应该因为写法不同就算出两个指纹，所以统一归一化。
    """
    digits = re.sub(r"\D", "", phone or "")
    # 13 位且以 86 开头 = 带区号的大陆手机号，去掉区号
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits


def mask_phone(phone: str) -> str:
    """打码：``13800138000`` → ``138****8000``。"""
    digits = _normalize(phone)
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "****" if digits else ""


def phone_fingerprint(phone: str) -> str:
    """手机号的 HMAC 指纹。

    为什么要它：库里的手机号是**打码存储**的（看不到完整号码），
    但要支持「同一个手机号绑定多个 openid」这类排查，需要一个可比较的值。
    HMAC 用 SECRET_KEY 当盐，拿到数据库也反查不出号码。
    """
    digits = _normalize(phone)
    if not digits:
        return ""
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        digits.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _request_phone(code: str) -> dict:
    """按当前模式调一次 ``getuserphonenumber``。"""
    if settings.WX_OPENAPI_ENABLED:
        # 云调用：HTTP + 不带 access_token
        return _request(
            _PHONE_PATH,
            body={"code": code},
            base=settings.WX_OPENAPI_BASE,
        )
    if not is_configured():
        raise WeChatNotConfigured(
            "服务端还没配置微信 AppID / AppSecret，暂时无法授权手机号"
        )
    return _request(
        _PHONE_PATH,
        params={"access_token": get_access_token()},
        body={"code": code},
        base=_CGI_BASE,
    )


def get_phone_number(code: str) -> dict:
    """用 ``getPhoneNumber`` 回调里的 code 换取手机号。

    :returns: ``{"phone": 完整号码, "pure_phone": 不含区号, "country_code": "86",
                 "masked": "138****8000", "fingerprint": "..."}``
    :raises WeChatError: 未配置、code 无效/过期、或微信接口异常
    """
    code = (code or "").strip()
    if not code:
        raise WeChatError("缺少手机号授权凭证 code")

    payload = _request_phone(code)

    # 自管 token 模式下，多副本可能刚好把 token 顶失效，刷新一次再试。
    # 云调用模式不需要这个兜底（根本没有 token）。
    if not settings.WX_OPENAPI_ENABLED and int(payload.get("errcode") or 0) in _TOKEN_INVALID:
        logger.warning("access_token 失效，强制刷新后重试：errcode=%s", payload.get("errcode"))
        fresh = get_access_token(force=True)
        payload = _request(
            _PHONE_PATH,
            params={"access_token": fresh},
            body={"code": code},
            base=_CGI_BASE,
        )

    _raise_for_errcode(payload)

    info = payload.get("phone_info") or {}
    phone = str(info.get("phoneNumber") or "").strip()
    pure = str(info.get("purePhoneNumber") or "").strip() or phone
    if not pure:
        raise WeChatError("微信没有返回手机号，请重新授权")

    # 安全校验：回包里的 watermark.appid 必须是本小程序。
    # 没配 WX_APPID 就没法校验（云调用模式下可以不配），打条 warning 提醒补齐。
    expected = settings.WX_APPID
    got = str((info.get("watermark") or {}).get("appid") or "").strip()
    if expected:
        if got != expected:
            logger.warning("手机号 watermark 不匹配：期望 %s，实际 %s", expected, got)
            raise WeChatError("这个手机号不属于当前小程序，已拒绝")
    else:
        logger.warning("未配置 WX_APPID，跳过手机号 watermark 校验（建议补上）")

    return {
        "phone": phone,
        "pure_phone": pure,
        "country_code": str(info.get("countryCode") or "").strip(),
        "masked": mask_phone(pure),
        "fingerprint": phone_fingerprint(pure),
    }


# --- 旧登录方式（保留兼容，云调用模式下不要用）------------------------------


def code2session(code: str) -> dict:
    """用 ``wx.login()`` 返回的 code 换取 ``openid`` / ``session_key``。

    .. deprecated::
       这个方法**只能证明「请求来自本小程序」，证明不了用户做过什么**，
       所以它已经不作为额度分档的依据了（谁都能触发 wx.login）。
       而且开启「开放接口服务」后它会因白名单问题失败。
       保留它只为不破坏老前端的 ``/api/auth/login`` 调用。
    """
    if not is_configured():
        raise WeChatNotConfigured("服务端还没配置微信 AppID / AppSecret，暂时无法登录")

    code = (code or "").strip()
    if not code:
        raise WeChatError("缺少登录凭证 code")

    payload = _request(
        _CODE2SESSION_PATH,
        params={
            "appid": settings.WX_APPID,
            "secret": settings.WX_SECRET,
            "js_code": code,
            "grant_type": "authorization_code",
        },
        base=_CGI_BASE,
    )
    _raise_for_errcode(payload)

    openid = str(payload.get("openid") or "").strip()
    if not openid:
        raise WeChatError("微信没有返回 openid，请重新登录")

    return {
        "openid": openid,
        "session_key": str(payload.get("session_key") or ""),
        "unionid": str(payload.get("unionid") or ""),
    }


__all__ = [
    "WeChatError",
    "WeChatNotConfigured",
    "appid_configured",
    "is_configured",
    "phone_ready",
    "get_access_token",
    "get_phone_number",
    "mask_phone",
    "phone_fingerprint",
    "code2session",
]
