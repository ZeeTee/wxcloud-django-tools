"""OpenAI 兼容协议的极简 LLM 客户端。

为什么不用官方 SDK
------------------
后端跑在微信云托管上，容器镜像越小启动越快、冷启动越短。OpenAI 兼容的
``/chat/completions`` 用标准库 ``urllib`` 就够了，不必为此引入依赖树。

密钥只从环境变量读，不写进代码、不进日志。
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


class LLMError(RuntimeError):
    """模型调用失败。message 会直接展示给用户，所以要写人话。"""


class LLMConfig:
    __slots__ = ("api_key", "base_url", "model", "timeout")

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout


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


def chat(
    messages: list[dict],
    *,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    retries: int = 2,
) -> str:
    """调用 ``/chat/completions``，返回正文文本。

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
            return content

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


__all__ = ["LLMError", "LLMConfig", "load_config", "is_configured", "chat"]
