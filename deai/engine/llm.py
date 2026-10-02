"""OpenAI 兼容协议的极简 LLM 客户端，支持多 provider 切换与费用统计。

支持的 provider
----------------
用 ``LLM_PROVIDER`` 选择（默认 ``deepseek``）：

* ``deepseek`` —— DeepSeek 官方。密钥读 ``DEEPSEEK_API_KEY``，回退 ``LLM_API_KEY``。
* ``openrouter`` —— OpenRouter。密钥读 ``OPENROUTER_API_KEY``，回退 ``LLM_API_KEY``。

两者都兼容 OpenAI 的 ``/chat/completions`` 协议，但**返回值有两处实质差异**，
代码里都做了适配：

============  ==============================  ==================================
              DeepSeek                        OpenRouter
============  ==============================  ==================================
缓存命中字段  ``prompt_cache_hit_tokens``      ``prompt_tokens_details.cached_tokens``
费用          **不返回**，要按价格表自己算    **直接返回** ``cost``（美元）
余额接口      ``/user/balance``               ``/credits`` + ``/key``
============  ==============================  ==================================

为什么不用官方 SDK：后端跑在微信云托管上，容器越小冷启动越快。
标准库 ``urllib`` 就够了，不必为此引入依赖树。

密钥只从环境变量读，不写进代码、不进日志。

关于「费用怎么算」——一个实测结论
--------------------------------
最初想用「调用前后查两次余额、算差值」的方式计费，实测**行不通**：
余额只返回 2 位小数，而一次 humanizer 调用约 0.0014 元，远小于最小刻度；
调用 11704 token 后等 30 秒，余额纹丝不动。

所以改用响应里的 ``usage`` 字段。OpenRouter 还会直接给出 ``cost``，
那就连价格表都不需要维护了。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_PROVIDER = "deepseek"
DEFAULT_TIMEOUT = 50  # 必须小于 gunicorn 的 --timeout(55) 与平台上限 60

# 单价（元 / 百万 token），仅 DeepSeek 这类「不返回 cost」的 provider 需要。
# 默认取 DeepSeek 的高峰时段价——宁可高估不要低估。
DEFAULT_PRICE_CACHE_IN = 0.04
DEFAULT_PRICE_IN = 2.0
DEFAULT_PRICE_OUT = 8.0
# OpenRouter 返回美元成本，按这个汇率折成人民币入库/展示。汇率会变，按需覆盖。
DEFAULT_USD_CNY = 7.2

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "key_envs": ("DEEPSEEK_API_KEY", "LLM_API_KEY"),
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "reports_cost": False,
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_envs": ("OPENROUTER_API_KEY", "LLM_API_KEY"),
        "base_url": "https://openrouter.ai/api/v1",
        # OpenRouter 的模型名是 `厂商/模型` 格式
        "model": "deepseek/deepseek-chat",
        "reports_cost": True,
    },
}


class LLMError(RuntimeError):
    """模型调用失败。message 会直接展示给用户，所以要写人话。"""


class LLMConfig:
    __slots__ = ("api_key", "base_url", "model", "timeout", "provider")

    def __init__(
        self, api_key: str, base_url: str, model: str, timeout: int, provider: str
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.provider = provider

    @property
    def label(self) -> str:
        return str(PROVIDERS.get(self.provider, {}).get("label") or self.provider)


class Usage:
    """一次调用的 token 消耗与（可能的）供应商上报成本。"""

    __slots__ = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cost_usd",
    )

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens or (prompt_tokens + completion_tokens)
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_miss_tokens = (
            cache_miss_tokens if cache_miss_tokens else max(0, prompt_tokens - cache_hit_tokens)
        )
        # 供应商直接上报的美元成本；只有 OpenRouter 会给，DeepSeek 恒为 0
        self.cost_usd = cost_usd

    @classmethod
    def from_api(cls, raw: dict | None) -> "Usage":
        """兼容两种 usage 格式。

        DeepSeek 用 ``prompt_cache_hit_tokens``；OpenRouter（OpenAI 格式）把
        缓存命中放在 ``prompt_tokens_details.cached_tokens`` 里。
        """
        raw = raw or {}
        details = raw.get("prompt_tokens_details") or {}
        try:
            cost_usd = float(raw.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost_usd = 0.0
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cache_hit_tokens=int(
                raw.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
            ),
            cache_miss_tokens=int(raw.get("prompt_cache_miss_tokens") or 0),
            cost_usd=cost_usd,
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
            "costUSD": round(self.cost_usd, 8),
        }


class ChatResult:
    __slots__ = ("content", "usage", "model", "provider")

    def __init__(self, content: str, usage: Usage, model: str, provider: str) -> None:
        self.content = content
        self.usage = usage
        self.model = model
        self.provider = provider


def _pick_key(spec: dict) -> str:
    for env_name in spec.get("key_envs", ()):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    return ""


def active_provider() -> str:
    """当前生效的 provider 名。未知值回落默认，不报错。"""
    name = (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    return name if name in PROVIDERS else DEFAULT_PROVIDER


def load_config() -> LLMConfig:
    provider = active_provider()
    spec = PROVIDERS[provider]
    base = (os.environ.get("LLM_BASE_URL") or spec["base_url"]).strip().rstrip("/")
    model = (os.environ.get("LLM_MODEL") or spec["model"]).strip()
    try:
        timeout = int(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return LLMConfig(
        _pick_key(spec), base, model, max(5, min(timeout, 55)), provider
    )


def is_configured() -> bool:
    """没有密钥时，接口应该优雅降级而不是 500。"""
    return bool(load_config().api_key)


def provider_info() -> dict:
    """给 /api/health 用：当前用哪个 provider、模型是什么、密钥配没配。"""
    cfg = load_config()
    return {
        "provider": cfg.provider,
        "providerLabel": cfg.label,
        "model": cfg.model,
        "baseUrl": cfg.base_url,
        "keyConfigured": bool(cfg.api_key),
        "available": list(PROVIDERS.keys()),
    }


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


def usd_cny_rate() -> float:
    try:
        return float(os.environ.get("USD_CNY_RATE") or DEFAULT_USD_CNY)
    except ValueError:
        return DEFAULT_USD_CNY


def estimate_cost(usage: Usage, provider: str | None = None) -> dict:
    """把 token 用量折算成人民币费用。

    优先用供应商上报的美元成本（OpenRouter 会给），没有才按本地价格表估算。
    两者都会标明 ``source``，便于判断这个数字有多可信。
    """
    provider = provider or active_provider()

    if usage.cost_usd > 0:
        rate = usd_cny_rate()
        cny = usage.cost_usd * rate
        return {
            "costCNY": round(cny, 6),
            "rawCostUSD": round(usage.cost_usd, 8),
            "usdCnyRate": rate,
            "source": "provider",  # 供应商直接上报，最可信
            "provider": provider,
        }

    price_cache, price_miss, price_out = load_pricing()
    cache_cost = usage.cache_hit_tokens / 1_000_000 * price_cache
    miss_cost = usage.cache_miss_tokens / 1_000_000 * price_miss
    out_cost = usage.completion_tokens / 1_000_000 * price_out
    return {
        "costCNY": round(cache_cost + miss_cost + out_cost, 6),
        "breakdown": {
            "cacheHit": round(cache_cost, 6),
            "cacheMiss": round(miss_cost, 6),
            "output": round(out_cost, 6),
        },
        "pricing": {"cacheIn": price_cache, "in": price_miss, "out": price_out},
        "source": "local_table",  # 本地价格表估算，仅供参考
        "provider": provider,
    }


def _get_json(url: str, key: str, timeout: int) -> dict:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LLMError("模型密钥无效或没有权限（401/403）") from exc
        raise LLMError(f"接口返回 {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise LLMError("查询超时") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接失败：{getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError("接口返回的不是合法 JSON") from exc


def get_balance(timeout: int = 15) -> dict:
    """查询账户余额。两个 provider 的接口不同，这里分别适配。

    只用于展示。**不要**用它做「调用前后差值」来算费用——实测余额精度和更新
    延迟都不支持单次核算（详见模块 docstring）。
    """
    cfg = load_config()
    if not cfg.api_key:
        raise LLMError("服务端尚未配置模型密钥，无法查询余额")

    if cfg.provider == "openrouter":
        # OpenRouter: /credits 给总额度与已用，/key 给 key 级限额
        root = cfg.base_url
        credits = (_get_json(f"{root}/credits", cfg.api_key, timeout).get("data") or {})
        total = float(credits.get("total_credits") or 0)
        used = float(credits.get("total_usage") or 0)
        payload = {
            "provider": "openrouter",
            "currency": "USD",
            "totalBalance": f"{total - used:.4f}",
            "totalCredits": f"{total:.4f}",
            "totalUsage": f"{used:.4f}",
        }
        try:
            key_data = (_get_json(f"{root}/key", cfg.api_key, timeout).get("data") or {})
            payload["keyLimit"] = key_data.get("limit")
            payload["keyLimitRemaining"] = key_data.get("limit_remaining")
            payload["isFreeTier"] = bool(key_data.get("is_free_tier"))
            payload["expiresAt"] = key_data.get("expires_at")
        except LLMError:
            pass  # key 信息拿不到不影响余额展示
        return payload

    # DeepSeek: 余额接口在 API 根路径下，不带 /v1
    root = cfg.base_url[:-3] if cfg.base_url.endswith("/v1") else cfg.base_url
    data = _get_json(f"{root}/user/balance", cfg.api_key, timeout)
    infos = data.get("balance_infos") or []
    first = infos[0] if infos else {}
    return {
        "provider": "deepseek",
        "isAvailable": bool(data.get("is_available")),
        "currency": first.get("currency") or "CNY",
        "totalBalance": first.get("total_balance") or "0",
        "grantedBalance": first.get("granted_balance") or "0",
        "toppedUpBalance": first.get("topped_up_balance") or "0",
    }


def _extra_headers(cfg: LLMConfig) -> dict:
    """OpenRouter 建议带上来源信息（用于它的应用排名），拿不到就留空。"""
    if cfg.provider != "openrouter":
        return {}
    headers = {}
    referer = (os.environ.get("OPENROUTER_SITE_URL") or "").strip()
    title = (os.environ.get("OPENROUTER_APP_NAME") or "deai-wechat").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


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
        raise LLMError("服务端尚未配置模型密钥，请联系管理员设置 API Key")

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
        for header, value in _extra_headers(cfg).items():
            request.add_header(header, value)
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            if data.get("error"):
                # OpenRouter 会用 200 带 error 体返回部分错误
                message = str((data.get("error") or {}).get("message") or "模型返回了错误")
                raise LLMError(message[:200])
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("模型没有返回任何内容，请稍后重试")
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            if not content:
                raise LLMError("模型返回了空文本，请稍后重试")
            usage = Usage.from_api(data.get("usage"))
            return ChatResult(
                content, usage, str(data.get("model") or cfg.model), cfg.provider
            )

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - 读错误体失败不该掩盖原始错误
                detail = ""
            if exc.code in (401, 403):
                # 密钥问题重试没有意义，直接抛出
                raise LLMError("模型密钥无效或没有权限（401/403），请检查服务端配置") from exc
            if exc.code == 402:
                # OpenRouter 额度不足用它
                raise LLMError("模型账户额度不足（402），请充值后再试") from exc
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
    "PROVIDERS",
    "DEFAULT_PROVIDER",
    "load_config",
    "load_pricing",
    "usd_cny_rate",
    "is_configured",
    "active_provider",
    "provider_info",
    "estimate_cost",
    "get_balance",
    "chat",
]
