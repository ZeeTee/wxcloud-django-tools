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

# 启发式：改写后新增的「具体化」痕迹。
# 实测模型会补出原文没有的举例（「填表、整理、来回搬运数据」），这类内容最危险
# ——读起来很具体，但全是编的。下面两条规则专门抓这种「凭空具体化」。
# 三项以上的顿号列举
_ENUM_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,12}(?:、[\u4e00-\u9fa5A-Za-z0-9]{2,12}){2,}")
# 中文引号/书名号里包着的短语
_QUOTED_RE = re.compile(r"[\u300c\u300e\u201c\"]([^\u300d\u300f\u201d\"]{2,30})[\u300d\u300f\u201d\"]")

# 模型自报「没新增」的各种写法（与 skills/output.py 的判定保持一致）
_NONE_FACT_CLAIMS = {"无", "无。", "没有", "没有。", "none", "n/a", "na", "-"}

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


def check_fidelity(original: str, rewritten: str, added_facts: str = "") -> list[str]:
    """保真校验，返回给用户看的人话提示（可能为空）。

    三层检查，因为「去 AI 味」和「不新增事实」本质上是冲突的——skill 要求
    「具体化、注入灵魂」，模型很容易编出听起来很具体的细节：

    1. **数字守恒**：原文的数字不能在改写后消失；
    2. **长度比**：暴涨（加了内容）或暴缩（删了信息）都要提醒；
    3. **新增内容**：模型自报（``added_facts``）+ 启发式（新增的列举 / 引号短语）。
       启发式会误报（改写本来就可能引入列举），所以措辞是「请核对」而不是断言。
    """
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

    warnings.extend(check_added_content(src, dst, added_facts))
    return warnings


def _missing_enum_items(original: str, rewritten: str) -> list[str]:
    """找出改写里「原文找不到出处」的列举项。

    不能整组字符串比对：原文写「批处理、快捷键和离线模式」，改写常常换成
    「批处理、快捷键、离线模式」，连接词一变，整组就不相等了，会大面积误报。
    所以拆成单项，用**前两个字**在原文里找子串——改写常给列举项加尾部修饰
    （「离线模式」→「离线模式它都支持」），前缀匹配能容忍这个。
    """
    missing: list[str] = []
    for enum in _ENUM_RE.findall(rewritten):
        if enum in original:  # 整组原样出现，肯定不是新增
            continue
        for item in enum.split("、"):
            item = item.strip()
            if len(item) >= 2 and item[:2] not in original:
                missing.append(item)
    return missing


def check_added_content(original: str, rewritten: str, added_facts: str = "") -> list[str]:
    """检测「模型自行补充了原文没有的具体内容」。

    两路信号：模型自己在报告里的自报，以及改写正文里新出现的具体化痕迹。
    模型自报更可信（它知道自己在补什么），启发式用来兜住它不承认的情况。
    启发式一定会误报，所以措辞是「确认一下」而不是断言。
    """
    warnings: list[str] = []

    claim = (added_facts or "").strip()
    if claim and claim.lower() not in _NONE_FACT_CLAIMS:
        short = re.sub(r"\s+", " ", claim)[:120]
        warnings.append("模型自报补充了原文没有的内容，请逐条核对：" + short)

    missing = _missing_enum_items(original, rewritten)
    if missing:
        samples = "、".join(sorted(set(missing))[:4])
        warnings.append("改写后出现了原文找不到出处的具体内容（" + samples + "），确认一下不是编的")

    new_quotes = set(_QUOTED_RE.findall(rewritten)) - set(_QUOTED_RE.findall(original))
    if new_quotes:
        samples = "；".join(sorted(new_quotes)[:3])
        warnings.append("改写后出现了原文没有的引述（" + samples + "），确认一下出处")

    return warnings


def resolve_system_prompt(
    skill: str, mode: str, intensity: str = "medium"
) -> tuple[str, str, str, bool, str]:
    """返回 ``(system_prompt, skill, 版本, 是否回退, prompt 指纹)``。

    skill 编译失败时回退到 legacy prompt，而不是让整个请求 500 ——
    用户拿到一份稍弱的结果，也好过拿到一个错误。

    指纹是编译后 prompt 的哈希：``version`` 是手写的、可能忘记 bump，
    指纹则能精确追踪「这次任务用的是哪份 prompt」。
    """
    if skill == LEGACY_SKILL:
        return get_system_prompt(mode), LEGACY_SKILL, "", False, ""
    try:
        compiled = get_registry().compile(skill, scene=mode, intensity=intensity)
    except SkillError as exc:
        logger.warning("skill %s 不可用（%s），回退到 legacy prompt", skill, exc)
        return get_system_prompt(mode), LEGACY_SKILL, "", True, ""
    return (
        compiled.system_prompt,
        compiled.slug,
        compiled.version,
        False,
        compiled.fingerprint,
    )


def rewrite(
    text: str,
    mode: str = "general",
    skill: str = DEFAULT_SKILL,
    intensity: str = "medium",
) -> dict:
    """调用模型做深度改写。

    :param mode: 场景（``general`` / ``xhs`` / ``academic`` / ``official``）
    :param skill: 用哪个 skill；``legacy`` 回退到硬编码 prompt
    :param intensity: 改写强度（``light`` / ``medium`` / ``heavy``）
    :returns: 含 ``text`` / ``report`` / ``warnings`` / skill 元信息的字典
    :raises llm.LLMError: 模型不可用或返回异常
    """
    cfg = llm.load_config()
    scene = mode if mode in MODE_LABELS or mode in ("academic", "official") else "general"
    system_prompt, used_skill, skill_version, degraded, fingerprint = resolve_system_prompt(
        skill, scene, intensity
    )
    use_protocol = used_skill != LEGACY_SKILL

    # 中文 1 字约 1-1.5 token；skill 模式还要额外输出报告，所以留更足余量
    max_tokens = max(1536, min(8000, int(len(text) * 2.4) + 768))
    user_message = (
        _PROTOCOL_USER_MESSAGE.format(text=text) if use_protocol else build_user_message(text)
    )

    result = llm.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        # 比 legacy 略低：skill 鼓励「注入灵魂」，压一点温度能减少自由发挥导致的事实添加
        temperature=0.9,
        max_tokens=max_tokens,
    )
    raw = result.content

    added_facts = ""
    if use_protocol:
        parsed = parse(raw)
        polished = strip_wrapping(parsed.text)
        report = parsed.report
        protocol_ok = parsed.parsed
        added_facts = parsed.added_facts
        if not protocol_ok:
            # 不阻断：容错解析已经给出可用正文，但要让它可见，便于观察模型稳定性
            logger.info("skill=%s 未按输出协议返回（fallback=%s）", used_skill, parsed.fallback)
    else:
        polished = strip_wrapping(raw)
        report = ""
        protocol_ok = True

    usage = result.usage
    cost = llm.estimate_cost(usage)

    return {
        "text": polished,
        "report": report,
        # 模型自报「新增了哪些原文没有的内容」，前端要原样展示给用户核对
        "addedFacts": added_facts,
        "warnings": check_fidelity(text, polished, added_facts),
        "model": result.model,
        "mode": scene,
        "skill": used_skill,
        "skillVersion": skill_version,
        # 编译后 prompt 的指纹：version 手写可能忘记 bump，指纹不会说谎
        "promptFingerprint": fingerprint,
        "intensity": intensity,
        "protocolOk": protocol_ok,
        "degraded": degraded,
        # 用量与费用取自响应里的 usage，不用「余额两次查询算差值」——
        # 实测余额只有 2 位小数且更新滞后，单次调用根本体现不出来（详见 llm.py）
        "usage": usage,
        "cost": cost,
    }


__all__ = [
    "rewrite",
    "resolve_system_prompt",
    "strip_wrapping",
    "check_fidelity",
    "check_added_content",
    "LEGACY_SKILL",
    "DEFAULT_SKILL",
]
