"""OpenAI 兼容协议的极简 LLM 客户端，外加 token 计量与费用估算。

为什么不用官方 SDK
------------------
后端跑在微信云托管上，容器镜像越小启动越快、冷启动越短。OpenAI 兼容的
``/chat/completions`` 用标准库 ``urllib`` 就够了，不必为此引入依赖树
（余额查询接口同理）。

密钥只从环境变量读，不写进代码、不进日志。

关于「费用怎么算」——这里有个实测结论
------------------------------------
最初想用「调用前后查两次余额、算差值」的方式计费，实测**行不通**：

* 余额接口只返回**2 位小数**（如 ``7.40``）；
* 一次 humanizer 调用实际花费约 ``0.0007`` 元，远小于 ``0.01`` 的最小刻度；
* 实测调用 11704 token 后等 30 秒，余额仍是 ``7.40``，差值为 0。

所以改用 ``/chat/completions`` 响应里的 ``usage`` 字段：它随响应一起返回、
精确到 token，还区分了缓存命中与未命中（两者单价差 50 倍）。
余额接口保留，但只用于**展示账户余额**，不再用来算单次费用。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 50  # 必须小于 gunicorn 的 --timeout(55) 与平台上限 60

# 单价（元 / 百万 token）。默认取 DeepSeek 的高峰时段价——宁可高估不要低估。
# 实测 prompt 缓存命中率约 98%（humanizer 的 system prompt 固定不变），
# 所以「缓存命中价」这一项对总成本影响很大，务必按实际定价配准。
DEFAULT_PRICE_CACHE_IN = 0.04
DEFAULT_PRICE_IN = 2.0
DEFAULT_PRICE_OUT = 8.0


class LLMError(RuntimeError):
    """模型调用失败。message 会直接展示给用户，所以要写人话。"""


class LLMConfig:
    __slots__ = ("api_key", "base_url", "model", "timeout")

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout


class Usage:
    """一次调用的 token 消耗。字段名对齐 OpenAI 兼容格式。"""

    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens", "cache_hit_tokens", "cache_miss_tokens")

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens or (prompt_tokens + completion_tokens)
        self.cache_hit_tokens = cache_hit_tokens
        # 有的返回里没有 miss 字段，用总量减出来
        self.cache_miss_tokens = (
            cache_miss_tokens if cache_miss_tokens else max(0, prompt_tokens - cache_hit_tokens)
        )

    @classmethod
    def from_api(cls, raw: dict | None) -> "Usage":
        raw = raw or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cache_hit_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
            cache_miss_tokens=int(raw.get("prompt_cache_miss_tokens") or 0),
        )

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hit_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    def to_dict(self) -> dict:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "cacheHitTokens": self.cache_hit_tokens,
            "cacheMissTokens": self.cache_miss_tokens,
            "cacheHitRate": round(self.cache_hit_rate, 4),
        }


class ChatResult:
    __slots__ = ("content", "usage", "model")

    def __init__(self, content: str, usage: Usage, model: str) -> None:
        self.content = content
        self.usage = usage
        self.model = model


def load_config() -> LLMConfig:
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    base = (os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    model = (os.environ.get("LLM_MODEL") or DEFAULT_MODEL).strip()
    try:
        timeout = int(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return LLMConfig(key, base, model, max(5, min(timeout, 55)))


def is_configured() -> bool:
    """没有密钥时，接口应该优雅降级而不是 500。"""
    return bool(load_config().api_key)


def load_pricing() -> tuple[float, float, float]:
    """返回 ``(缓存命中单价, 缓存未命中单价, 输出单价)``，单位 元/百万 token。"""

    def pick(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name) or default)
        except ValueError:
            return default

    return (
        pick("LLM_PRICE_CACHE_IN", DEFAULT_PRICE_CACHE_IN),
        pick("LLM_PRICE_IN", DEFAULT_PRICE_IN),
        pick("LLM_PRICE_OUT", DEFAULT_PRICE_OUT),
    )


def estimate_cost(usage: Usage) -> dict:
    """按 token 用量估算本次费用（元）。

    这是**估算**：不同模型的单价不同，且 DeepSeek 有高峰/空闲两档价
    （空闲是高峰的一半），请用 ``LLM_PRICE_*`` 环境变量配成你的实际单价。
    """
    price_cache, price_miss, price_out = load_pricing()
    cache_cost = usage.cache_hit_tokens / 1_000_000 * price_cache
    miss_cost = usage.cache_miss_tokens / 1_000_000 * price_miss
    out_cost = usage.completion_tokens / 1_000_000 * price_out
    total = cache_cost + miss_cost + out_cost
    return {
        "costCNY": round(total, 6),
        "breakdown": {
            "cacheHit": round(cache_cost, 6),
            "cacheMiss": round(miss_cost, 6),
            "output": round(out_cost, 6),
        },
        "pricing": {
            "cacheIn": price_cache,
            "in": price_miss,
            "out": price_out,
        },
    }


def get_balance(timeout: int = 15) -> dict:
    """查询账户余额。

    只用于展示。**不要**用它做「调用前后差值」来算费用——实测余额只保留
    2 位小数且更新滞后，单次调用的花费根本体现不出来（详见模块 docstring）。
    """
    cfg = load_config()
    if not cfg.api_key:
        raise LLMError("服务端尚未配置模型密钥，无法查询余额")

    # 余额接口在 API 根路径下，不带 /v1
    root = cfg.base_url[:-3] if cfg.base_url.endswith("/v1") else cfg.base_url
    request = urllib.request.Request(f"{root}/user/balance", method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {cfg.api_key}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LLMError("模型密钥无效或没有权限（401/403）") from exc
        raise LLMError(f"余额接口返回 {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise LLMError("查询余额超时") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接余额接口失败：{getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError("余额接口返回的不是合法 JSON") from exc

    infos = data.get("balance_infos") or []
    first = infos[0] if infos else {}
    return {
        "isAvailable": bool(data.get("is_available")),
        "currency": first.get("currency") or "CNY",
        "totalBalance": first.get("total_balance") or "0",
        "grantedBalance": first.get("granted_balance") or "0",
        "toppedUpBalance": first.get("topped_up_balance") or "0",
    }


def chat(
    messages: list[dict],
    *,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    retries: int = 2,
) -> ChatResult:
    """调用 ``/chat/completions``，返回 ``ChatResult``（正文 + token 用量）。

    失败时抛 ``LLMError``，其 message 可直接展示给用户。
    """
    cfg = load_config()
    if not cfg.api_key:
        raise LLMError("服务端尚未配置模型密钥，请联系管理员设置 LLM_API_KEY")

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{cfg.base_url}/chat/completions"

    last_err: LLMError | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {cfg.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("模型没有返回任何内容，请稍后重试")
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            if not content:
                raise LLMError("模型返回了空文本，请稍后重试")
            usage = Usage.from_api(data.get("usage"))
            return ChatResult(content, usage, str(data.get("model") or cfg.model))

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - 读错误体失败不该掩盖原始错误
                detail = ""
            if exc.code in (401, 403):
                # 密钥问题重试没有意义，直接抛出
                raise LLMError("模型密钥无效或没有权限（401/403），请检查服务端配置") from exc
            if exc.code == 429:
                last_err = LLMError("模型服务繁忙（429），稍后再试")
            elif exc.code >= 500:
                last_err = LLMError("模型服务暂时不可用，稍后再试")
            else:
                last_err = LLMError(f"模型接口返回 {exc.code}：{detail or exc.reason}")

        except (socket.timeout, TimeoutError):
            last_err = LLMError("模型响应超时，可能是文本太长，试试分段改写")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                last_err = LLMError("模型响应超时，可能是文本太长，试试分段改写")
            else:
                last_err = LLMError(f"连接模型服务失败：{reason}")
        except json.JSONDecodeError:
            last_err = LLMError("模型返回的内容不是合法 JSON")

        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    raise last_err or LLMError("模型调用失败，请稍后重试")


__all__ = [
    "LLMError",
    "LLMConfig",
    "Usage",
    "ChatResult",
    "load_config",
    "load_pricing",
    "is_configured",
    "estimate_cost",
    "get_balance",
    "chat",
]
