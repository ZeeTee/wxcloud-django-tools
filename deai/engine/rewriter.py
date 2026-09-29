"""混合改写编排：LLM 负责重写，规则层负责兜底与质检。

流程
----
1. 规则层先跑一遍（毫秒级），用户立刻能看到体检报告和「规则版」结果；
2. LLM 在后台线程做深度改写；
3. 改写结果过一道**保真校验** —— 这个功能最大的回归风险是模型为了去 AI 味
   顺手把原文的信息点和数字也删了。
"""

from __future__ import annotations

import re

from . import llm
from .prompts import MODE_LABELS, build_user_message, get_system_prompt

# 模型偶尔不听话，会用代码块或「以下是改写后的版本：」把正文包起来
_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?\s*```\s*$", re.S)
_PREFACE = re.compile(r"^\s*(以下是|下面是|这是)[^\n]{0,30}[:：]\s*\n+")
_QUOTE_PAIRS = (("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"'), ("'", "'"))
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def strip_wrapping(text: str) -> str:
    """清掉模型自作主张加上的包装，只留正文。"""
    t = (text or "").strip()
    m = _CODE_FENCE.match(t)
    if m:
        t = m.group(1).strip()
    t = _PREFACE.sub("", t)
    for left, right in _QUOTE_PAIRS:
        if len(t) > 2 and t.startswith(left) and t.endswith(right):
            t = t[1:-1].strip()
            break
    return t.strip()


def check_fidelity(original: str, rewritten: str) -> list[str]:
    """轻量保真校验，返回给用户看的人话提示（可能为空）。"""
    warnings: list[str] = []
    src = original.strip()
    dst = rewritten.strip()
    if not src or not dst:
        return warnings

    lost = sorted(set(_NUMBER.findall(src)) - set(_NUMBER.findall(dst)))
    if lost:
        warnings.append("原文里的数字 " + "、".join(lost[:6]) + " 在改写后没出现，请确认没被漏掉")

    ratio = len(dst) / len(src)
    if ratio < 0.55:
        warnings.append("改写后字数只有原文的 %d%%，可能删掉了信息" % round(ratio * 100))
    elif ratio > 1.9:
        warnings.append("改写后字数涨到原文的 %d%%，可能加了原文没有的内容" % round(ratio * 100))
    return warnings


def rewrite(text: str, mode: str = "general") -> dict:
    """调用模型做深度改写。

    :returns: ``{"text": 改写结果, "warnings": [...], "model": 模型名, "mode": 模式}``
    :raises llm.LLMError: 模型不可用或返回异常
    """
    cfg = llm.load_config()
    # 中文 1 字约 1-1.5 token，改写后长度通常与原文相当，留足余量但不超模型上限
    max_tokens = max(1024, min(8000, int(len(text) * 2.2) + 512))

    raw = llm.chat(
        [
            {"role": "system", "content": get_system_prompt(mode)},
            {"role": "user", "content": build_user_message(text)},
        ],
        temperature=1.0,
        max_tokens=max_tokens,
    )

    polished = strip_wrapping(raw)
    return {
        "text": polished,
        "warnings": check_fidelity(text, polished),
        "model": cfg.model,
        "mode": mode if mode in MODE_LABELS else "general",
    }


__all__ = ["rewrite", "strip_wrapping", "check_fidelity"]
