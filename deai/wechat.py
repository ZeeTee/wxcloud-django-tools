"""微信小程序登录：用 ``wx.login()`` 的 code 换 openid。

走微信官方接口 ``sns/jscode2session``。用标准库 ``urllib`` 实现，理由和 ``llm.py``
一样：云托管镜像越小、冷启动越快，不值得为一次 HTTP 调用引入依赖树。

**为什么不用 access_token**
--------------------------
``code2Session`` 只需要 ``appid`` + ``secret``，**不需要 access_token**。
所以这里没有 token 缓存、没有过期刷新、没有多副本抢刷的问题。
（以后若要调 ``getPhoneNumber`` 之类的开放接口，才需要 access_token，
那时再单独做。）

**为什么需要它**
----------------
``X-WX-OPENID`` 是云托管自动注入的，未登录也能拿到——匿名额度靠它计数就够了。
但「未登录」和「已登录」在身份上没有区别，要区分两档额度，就得有一个
**用户主动做过的、可验证的动作**。这个模块提供的正是那个验证。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

_API = "https://api.weixin.qq.com/sns/jscode2session"

# 微信常见错误码 → 人话。展示给用户，所以不要写技术术语。
_ERROR_HINTS = {
    -1: "微信服务繁忙，请稍后再试",
    40029: "登录凭证已失效，请重新点击登录",
    45011: "操作太频繁了，请稍后再试",
    40226: "登录被风控拦截，请稍后再试",
}


class WeChatError(RuntimeError):
    """登录失败。message 会直接展示给用户。"""


def is_configured() -> bool:
    return bool(settings.WX_APPID and settings.WX_SECRET)


def code2session(code: str) -> dict:
    """用 ``wx.login()`` 返回的 code 换取 ``openid`` / ``session_key``。

    :returns: ``{"openid": ..., "session_key": ..., "unionid": ...}``
    :raises WeChatError: 未配置、code 无效、或微信接口异常
    """
    if not is_configured():
        raise WeChatError("服务端还没配置微信 AppID / AppSecret，暂时无法登录")

    code = (code or "").strip()
    if not code:
        raise WeChatError("缺少登录凭证 code")

    query = urllib.parse.urlencode(
        {
            "appid": settings.WX_APPID,
            "secret": settings.WX_SECRET,
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    request = urllib.request.Request(f"{_API}?{query}", method="GET")

    try:
        with urllib.request.urlopen(request, timeout=settings.WX_LOGIN_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (socket.timeout, TimeoutError) as exc:
        raise WeChatError("连接微信服务器超时，请稍后再试") from exc
    except urllib.error.URLError as exc:
        raise WeChatError(f"连接微信服务器失败：{getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise WeChatError("微信服务器返回的内容无法解析") from exc

    # 注意：微信这个接口出错时 HTTP 状态仍是 200，错误藏在 body 的 errcode 里，
    # 所以不能只看状态码。
    errcode = payload.get("errcode")
    if errcode:
        hint = _ERROR_HINTS.get(int(errcode), "微信登录失败，请稍后再试")
        raise WeChatError(f"{hint}（errcode={errcode}）")

    openid = str(payload.get("openid") or "").strip()
    if not openid:
        raise WeChatError("微信没有返回 openid，请重新登录")

    return {
        "openid": openid,
        "session_key": str(payload.get("session_key") or ""),
        "unionid": str(payload.get("unionid") or ""),
    }


__all__ = ["WeChatError", "is_configured", "code2session"]
