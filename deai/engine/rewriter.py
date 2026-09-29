"""混合改写编排：LLM 负责重写，规则层负责兜底与质检。

流程
----
1. 规则层先跑一遍（毫秒级），用户立刻能看到体检报告和「规则版」结果；
2. LLM 在后台线程做深度改写；
3. 改写结果过一道**保真校验** —— 这个功能最大的回归风险是模型为了去 AI 味
   顺手把原文的信息点和数字也删了。

两种执行方式
------------
* ``skill="humanizer"``（默认）：用 ``deai/skills/`` 下的 skill 编译出的 system
  prompt。skill 原文是为「有文件工具的 agent」写的，编译时会把它依赖的
  references 注入进 prompt（见 ``deai/skills/registry.py``）。
* ``skill="legacy"``：回退到 ``prompts.py`` 里那两个硬编码 prompt。

保留 legacy 是为了**灰度与故障回退**：skill 出问题时可以一键切回，不用改代码、
不用重新部署。这也是 P0 敢直接上 skill 的底气。
"""

from __future__ import annotations

import logging
import re

from ..skills import SkillError, get_registry, parse
from . import llm
from .prompts import MODE_LABELS, build_user_message, get_system_prompt

logger = logging.getLogger(__name__)

LEGACY_SKILL = "legacy"
DEFAULT_SKILL = "humanizer"

# 模型偶尔不听话，会用代码块或「以下是改写后的版本：」把正文包起来
_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?\s*```\s*$", re.S)
_PREFACE = re.compile(r"^\s*(以下是|下面是|这是)[^\n]{0,30}[:：]\s*\n+")
_QUOTE_PAIRS = (("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"'), ("'", "'"))
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# skill 模式的 user message：指向系统提示里的输出协议
_PROTOCOL_USER_MESSAGE = (
    "请按系统提示中的【输出协议】改写下面这段文字。\n\n"
    "<待改写文本>\n{text}\n</待改写文本>"
)


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


def resolve_system_prompt(skill: str, mode: str) -> tuple[str, str, str, bool]:
    """返回 ``(system_prompt, 实际使用的 skill, skill 版本, 是否发生回退)``。

    skill 编译失败时回退到 legacy prompt，而不是让整个请求 500 ——
    用户拿到一份稍弱的结果，也好过拿到一个错误。
    """
    if skill == LEGACY_SKILL:
        return get_system_prompt(mode), LEGACY_SKILL, "", False
    try:
        compiled = get_registry().compile(skill, scene=mode)
    except SkillError as exc:
        logger.warning("skill %s 不可用（%s），回退到 legacy prompt", skill, exc)
        return get_system_prompt(mode), LEGACY_SKILL, "", True
    return compiled.system_prompt, compiled.slug, compiled.version, False


def rewrite(text: str, mode: str = "general", skill: str = DEFAULT_SKILL) -> dict:
    """调用模型做深度改写。

    :param mode: 场景（``general`` / ``xhs``），决定 skill 的场景覆盖
    :param skill: 用哪个 skill；``legacy`` 回退到硬编码 prompt
    :returns: 含 ``text`` / ``report`` / ``warnings`` / skill 元信息的字典
    :raises llm.LLMError: 模型不可用或返回异常
    """
    cfg = llm.load_config()
    scene = mode if mode in MODE_LABELS else "general"
    system_prompt, used_skill, skill_version, degraded = resolve_system_prompt(skill, scene)
    use_protocol = used_skill != LEGACY_SKILL

    # 中文 1 字约 1-1.5 token；skill 模式还要额外输出报告，所以留更足余量
    max_tokens = max(1536, min(8000, int(len(text) * 2.4) + 768))
    user_message = (
        _PROTOCOL_USER_MESSAGE.format(text=text) if use_protocol else build_user_message(text)
    )

    raw = llm.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        # 比 legacy 略低：skill 鼓励「注入灵魂」，压一点温度能减少自由发挥导致的事实添加
        temperature=0.9,
        max_tokens=max_tokens,
    )

    if use_protocol:
        parsed = parse(raw)
        polished = strip_wrapping(parsed.text)
        report = parsed.report
        protocol_ok = parsed.parsed
        if not protocol_ok:
            # 不阻断：容错解析已经给出可用正文，但要让它可见，便于观察模型稳定性
            logger.info("skill=%s 未按输出协议返回（fallback=%s）", used_skill, parsed.fallback)
    else:
        polished = strip_wrapping(raw)
        report = ""
        protocol_ok = True

    return {
        "text": polished,
        "report": report,
        "warnings": check_fidelity(text, polished),
        "model": cfg.model,
        "mode": scene,
        "skill": used_skill,
        "skillVersion": skill_version,
        "protocolOk": protocol_ok,
        "degraded": degraded,
    }


__all__ = [
    "rewrite",
    "resolve_system_prompt",
    "strip_wrapping",
    "check_fidelity",
    "LEGACY_SKILL",
    "DEFAULT_SKILL",
]
