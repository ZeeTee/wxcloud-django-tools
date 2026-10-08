"""请求日志中间件：把每个请求的参数、身份头、来源 IP 打进日志。

为什么需要它
------------
``X-WX-OPENID`` 这类头是**云托管在服务端注入**的，客户端根本不发送——
在微信开发者工具的 Network 面板里永远看不到。想确认「到底有没有值」，
唯一的地方就是**容器日志**。这个中间件就是干这个的。

用中间件而不是给每个 view 加装饰器：一次覆盖**所有**接口（含模板原有的
``/api/count`` 和主页），而且以后新增接口自动被覆盖，不会漏。

⚠️ 隐私提示
-----------
它会把 ``openid`` 和**请求体原文**（也就是用户要改写的文稿）写进日志。
排查阶段开着的收益远大于成本，但**对公开放之前建议关掉**：

    DEAI_LOG_REQUESTS=false

日志格式（每个请求一行，方便 grep ``[req]``）::

    [req] POST /api/rewrite -> 200 1523ms | has_openid=True has_identity=True openid=oXXXX appid=wxXXXX env=prod-1 source=1 | ip=1.2.3.4 | params=body={"text":"首先…"} | headers={"host":"…"}

两个布尔值的区别（搜「到底有没有值」时搜它们最省事）：

- ``has_openid``：``X-WX-OPENID`` 在不在——直属环境的正常形态
- ``has_identity``：``X-WX-OPENID`` 或 ``X-WX-FROM-OPENID`` 任一个有——资源复用

> 注意 ``headers={...}`` 里的名字是 Django 迭代 `request.headers` 时的写法
> （``X-WX-OPENID`` 会显示成 ``X-Wx-Openid``）。header 本身大小写不敏感，
> 取值不受影响；上面 ``openid=``/``source=`` 那一段用的是官方原始写法。
"""

from __future__ import annotations

import json
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# 身份相关：排查的重点，逐个列出来，没有值的显示 "-"
_IDENTITY_HEADERS = {
    "X-WX-OPENID": "openid",
    "X-WX-FROM-OPENID": "from_openid",
    "X-WX-APPID": "appid",
    "X-WX-FROM-APPID": "from_appid",
    "X-WX-UNIONID": "unionid",
    "X-WX-FROM-UNIONID": "from_unionid",
    "X-WX-ENV": "env",
    "X-WX-SOURCE": "source",
}

# 客户端 IP。官方两篇文档写的名字不一致，两个都读；只作参考，不做安全判据。
_IP_HEADERS = ("X-Original-Forwarded-For", "X-Forwarded-For")

# 这几个头不打：cookie / 凭证是敏感信息，content-length 是噪声
_SKIP_HEADERS = {"cookie", "authorization", "content-length"}


def _brief(value: str, limit: int) -> str:
    """超长就截断。截断量写出来，免得看日志的人以为内容就这么点。"""
    text = " ".join(str(value).split())  # 压成一行，保证一条日志只占一行
    if limit > 0 and len(text) > limit:
        return f"{text[:limit]}…(截断，共 {len(text)} 字)"
    return text


def _dump(obj: object, limit: int) -> str:
    try:
        return _brief(json.dumps(obj, ensure_ascii=False), limit)
    except (TypeError, ValueError):
        return _brief(str(obj), limit)


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEAI_LOG_REQUESTS:
            return self.get_response(request)

        started = time.monotonic()
        # 必须在 view 之前读 body：Django 首次访问 request.body 会把原始字节
        # 缓存进 request._body，之后 view 再读拿到的还是同一份，不会被消费掉。
        try:
            params = self._params(request)
        except Exception as exc:  # noqa: BLE001 - 日志绝不能把请求搞挂
            params = f"<读取失败：{exc}>"

        response = self.get_response(request)

        try:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info(
                "[req] %s %s -> %s %.0fms | %s | ip=%s | params=%s | headers=%s",
                request.method,
                request.get_full_path(),
                getattr(response, "status_code", "?"),
                elapsed_ms,
                self._identity(request),
                self._ip(request),
                params,
                self._headers(request),
            )
        except Exception:  # noqa: BLE001
            logger.warning("[req] 组装日志失败", exc_info=True)

        return response

    # ---------------- 各段 ----------------

    def _identity(self, request) -> str:
        limit = settings.DEAI_LOG_MAX_CHARS
        parts = []
        for header, label in _IDENTITY_HEADERS.items():
            value = request.headers.get(header) or ""
            parts.append(f"{label}={_brief(value, 64) if value else '-'}")

        # 两个布尔值，想在日志里搜「到底有没有」时搜它们最省事：
        #   has_openid   = X-WX-OPENID 在不在（直属环境的正常形态）
        #   has_identity = 两个头任一个有（资源复用只有 X-WX-FROM-OPENID）
        # 分开写是有原因的：只按「任一个有」算的话，资源复用场景下会出现
        # 「has_openid=True 但 openid=-」这种自相矛盾的日志。
        has_openid = bool(request.headers.get("X-WX-OPENID"))
        has_identity = has_openid or bool(request.headers.get("X-WX-FROM-OPENID"))
        head = f"has_openid={has_openid} has_identity={has_identity}"
        return _brief(f"{head} {' '.join(parts)}", max(limit, 400))

    def _ip(self, request) -> str:
        for name in _IP_HEADERS:
            value = request.headers.get(name)
            if value:
                return value
        return request.META.get("REMOTE_ADDR") or "-"

    def _params(self, request) -> str:
        limit = settings.DEAI_LOG_MAX_CHARS
        parts = []

        query = request.META.get("QUERY_STRING")
        if query:
            parts.append(f"query={_brief(query, limit)}")

        content_type = request.content_type or ""
        if content_type.startswith("application/json"):
            try:
                raw = request.body.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                parts.append(f"body=<读取失败：{exc}>")
                raw = ""
            if raw:
                try:
                    # 解析后再 dump，为的是让 ensure_ascii=False 生效：
                    # 客户端发 "\u9996\u5148" 这种转义时，日志里也是能读的中文
                    parts.append(f"body={_dump(json.loads(raw), limit)}")
                except (json.JSONDecodeError, ValueError):
                    # 不是合法 JSON 就原样打出来——排查坏请求时更要看原始内容
                    parts.append(f"body={_brief(raw, limit)}")
        elif request.method == "POST":
            try:
                parts.append(f"post={_dump(request.POST.dict(), limit)}")
            except Exception:  # noqa: BLE001
                parts.append("post=<读取失败>")

        return " ".join(parts) if parts else "-"

    def _headers(self, request) -> str:
        limit = settings.DEAI_LOG_MAX_CHARS
        data = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _SKIP_HEADERS
        }
        return _dump(data, limit)
