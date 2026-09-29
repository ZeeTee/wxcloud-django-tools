"""体检报告：把规则命中翻译成「AI 味分数 + 分类明细 + 一针见血的建议」。"""

from __future__ import annotations

import math

from .rules import Hit
from .textutil import rhythm_stats

# 不同严重度的权重（结构问题的权重额外放大，因为它是 AI 味的主要来源）
_SEVERITY_WEIGHT = {"high": 3.0, "medium": 1.4, "low": 0.6}
_PATTERN_BOOST = 1.5
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# 分类 → 一条可执行的改写建议（体检报告里给用户看）
_CATEGORY_ADVICE: dict[str, str] = {
    "connector": "把「首先/其次/综上所述」这类路标词删掉，句子之间靠意思衔接，不靠连接词。",
    "opening": "开头别铺垫时代背景，直接说事。第一句就给场景或结论。",
    "empty_adj": "「极大地/显著地」删掉后句意不变，说明它们本来就没信息量。",
    "hollow_result": "「发挥了重要作用」要换成具体做了什么、结果怎样。",
    "closing": "结尾不要升华和口号，讲完就停。",
    "meta": "别预告自己要说什么（「接下来我们来看」），直接进入内容。",
    "translation": "「对…进行优化」改成「优化了」——还原动词和主动语态。",
    "buzzword": "行业黑话换成大白话：「赋能」→「帮」，「抓手」→「切入点」。",
    "hedge": "「进一步/不断/更好地」多数可以直接删。",
    "idiom": "四字词和成语堆多了会显得空，一句话最多留一个。",
    "written_verb": "书面动词换成口语词：「获取」→「拿到」，「具备」→「有」。",
    "other": "这些词是 AI 的高频用语，建议逐一替换成日常说法。",
}

# 结构模式（pattern_XX）的建议。这些是最有洞察力的命中，
# 但它们的 category key 是 pattern_P01 这种，查不到 _CATEGORY_ADVICE，
# 不单独映射的话 advice 会莫名其妙变空。
_PATTERN_ADVICE: dict[str, str] = {
    "P01": "删掉「首先/其次/最后」这类路标词，句子之间靠意思衔接，不靠序数词。",
    "P02": "「更X、更Y、更Z」的排比留一个就够，三个以上就成广告词了。",
    "P03": "「不仅…而且…」连着堆会显得空，改成一句一句说。",
    "P04": "「不是…而是…」用两次以上就成了模板，换成直接陈述。",
    "P05": "「是X，是Y，更是Z」是最典型的升华排比，砍掉。",
    "P06": "破折号插入语全篇最多留一处。",
    "P07": "破折号用得太密，改成句号或括号。",
    "P08": "「问题很直接：…」是在替读者下判断，把冒号后面的话直接写出来。",
    "P09": "「包括以下几点：」这种冒号清单，改成正常句子叙述。",
    "P10": "正文里别用 emoji、【】段头和 🔥💡👉 这类引导符。",
    "P11": "加粗太多了，真正的重点不需要加粗三次。",
    "P12": "整篇项目符号会像说明书，最多留三条，其余写成段落。",
    "P13": "小标题太密，合并一些，让段落自己说话。",
    "P14": "「一、二、三」式伪权威标题是模板痕迹。",
    "P15": "「对…进行优化」改成「优化了…」，还原动词。",
    "P16": "「发挥了重要作用」要换成具体做了什么。",
    "P17": "「使…得到了提升」改成主动语态：「…变快了」。",
    "P18": "「对于…而言」是译制腔，换成「对…来说」或直接删。",
    "P19": "「作为一个…」开头很生硬，直接说事。",
    "P20": "「…之一」是典型的模糊收尾，给个明确说法。",
    "P21": "「研究表明」没有出处就是空挂权威，要么补来源，要么删掉。",
    "P22": "「既要…也要…」是伪平衡，换成明确表态或分情况说。",
    "P23": "四字成语连着堆三个以上，读起来像标语。",
    "P24": "「具有里程碑式的意义」这类拔高句，说具体影响。",
    "P25": "结尾的鸡汤和口号整段删掉，讲完就停。",
    "P26": "删掉「希望本文对您有所帮助」这类客服收尾。",
    "P27": "别预告自己要说什么，直接进入内容。",
    "P28": "「好的/当然可以/问得好」是助手腔，正文里不该出现。",
    "P29": "正文里残留了引用角标或模型痕迹，务必删干净。",
    "P30": "正文里的分割线和引用块是排版模板痕迹。",
    "P31": "连接词密度过高，每段都在「此外/因此/从而」，删掉一半。",
    "P32": "同一个东西反复换称呼（该系统/这款工具）反而像机器写的。",
    "P33": "「被」字句太多，换成主动语态。",
    "P34": "「非常/十分/极其」这类程度副词删掉不影响意思。",
    "P35": "中英文夹杂，把英文词换成中文说法。",
    "P36": "开头别铺垫时代背景，第一句直接说事。",
}


def _weighted_score(text: str, hits: list[Hit]) -> int:
    n = max(len(text.strip()), 1)
    weighted = 0.0
    high_patterns = 0
    high_words = 0

    for h in hits:
        w = _SEVERITY_WEIGHT.get(h.severity, 1.0)
        if h.rule_id.startswith("pattern:"):
            w *= _PATTERN_BOOST
            if h.severity == "high":
                high_patterns += 1
        elif h.severity == "high":
            high_words += 1
        weighted += w

    density = weighted / n * 100
    score = 100 * (1 - math.exp(-density / 6))

    # 保底：结构性问题比零散用词更能说明问题，不该被长文本稀释
    if high_patterns >= 2:
        score = max(score, 70)
    elif high_patterns == 1:
        score = max(score, 45)
    if high_words >= 8:
        score = max(score, 65)
    elif high_words >= 4:
        score = max(score, 40)

    return min(100, round(score))


def _level_of(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _aggregate(hits: list[Hit]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for h in hits:
        b = buckets.setdefault(
            h.category,
            {
                "key": h.category,
                "name": h.category_name,
                "count": 0,
                "severity": h.severity,
                "samples": [],
            },
        )
        b["count"] += 1
        # 类别严重度取该类里最严重的
        if _SEVERITY_RANK.get(h.severity, 9) < _SEVERITY_RANK.get(b["severity"], 9):
            b["severity"] = h.severity
        if h.text not in b["samples"] and len(b["samples"]) < 4:
            b["samples"].append(h.text)

    out = list(buckets.values())
    out.sort(key=lambda b: (_SEVERITY_RANK.get(b["severity"], 9), -b["count"]))
    return out


def _advice(categories: list[dict]) -> list[str]:
    tips: list[str] = []
    for c in categories[:4]:
        key = c["key"]
        tip = _CATEGORY_ADVICE.get(key)
        if not tip and key.startswith("pattern_"):
            tip = _PATTERN_ADVICE.get(key.split("_", 1)[-1])
        if not tip:
            # 兜底：宁可用通用措辞，也不要让「建议」卡片空着
            tip = f"「{c['name']}」出现 {c['count']} 处，按上面的原因逐条改掉。"
        if tip not in tips:
            tips.append(tip)
    return tips


def _verdict(score: int, level: str, categories: list[dict]) -> str:
    head = {
        "high": "AI 味偏重",
        "medium": "有点 AI 味",
        "low": "读起来挺像人写的",
    }[level]
    if not categories:
        return f"{head}（{score} 分）：没抓到明显的机器腔，节奏也自然。"
    detail = "、".join(f"{c['name']} {c['count']} 处" for c in categories[:3])
    return f"{head}（{score} 分）：主要是{detail}。"


def build_report(text: str, hits: list[Hit]) -> dict:
    """生成完整的体检报告（直接作为 API 的 ``report`` 字段返回）。"""
    score = _weighted_score(text, hits)
    level = _level_of(score)
    categories = _aggregate(hits)
    stats = rhythm_stats(text)

    # 句子过于整齐划一时补一条提示（这是很典型的机器特征）
    extra_tips = list(_advice(categories))
    if stats["sentenceCount"] >= 5 and stats["rhythmVariance"] < 0.35:
        extra_tips.append("句子长度太均匀了，读起来像模板。故意插几个短句打破节奏。")
    if stats["longSentenceRatio"] > 0.5:
        extra_tips.append("超过一半的句子在 40 字以上，长句拆短会立刻自然很多。")
    # 词库是中文的，英文/代码为主的文本判不准，直接说清楚，别让用户以为"没问题"
    stripped = text.strip()
    if len(stripped) > 20:
        cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
        if cjk / len(stripped) < 0.3:
            extra_tips.append("这段文本里中文很少，当前词库主要针对中文，对英文部分的判断不一定准。")

    return {
        "score": score,
        "level": level,
        "verdict": _verdict(score, level, categories),
        "totalHits": len(hits),
        "charCount": len(text.strip()),
        "categories": categories,
        "advice": extra_tips[:4],
        "stats": stats,
        "hits": [h.to_dict() for h in hits],
    }


__all__ = ["build_report"]
