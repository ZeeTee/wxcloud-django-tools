"""调用方身份识别。

云托管会把调用者身份放进内置请求头，官方明确这些头「开发者可以完全信任」。
但有两个坑必须处理：

1. **公网直连容器时没有 ``X-WX-OPENID``**，要用 ``X-WX-SOURCE`` 区分来源；
2. **资源复用场景**下 openid 在 ``X-WX-FROM-OPENID`` 里，要兜底读取。
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


class AuthError(Exception):
    """身份不可信。message 会作为 401 响应体返回。"""


def get_identity(request) -> str:
    """返回调用方 openid；无法识别时按配置决定放行（本地开发）或拒绝。"""
    openid = (request.headers.get("X-WX-OPENID") or "").strip()
    if not openid:
        # 资源复用场景：环境归属另一个账号，openid 在这里
        openid = (request.headers.get("X-WX-FROM-OPENID") or "").strip()
    if openid:
        return openid

    if settings.DEAI_ALLOW_ANONYMOUS:
        # 本地开发：没有云托管注入的头，允许匿名，方便用 curl 调试
        return ANONYMOUS

    logger.warning(
        "拒绝无身份的调用 source=%s path=%s",
        request.headers.get("X-WX-SOURCE"),
        request.path,
    )
    raise AuthError("无法识别调用方身份，请在小程序内使用")
