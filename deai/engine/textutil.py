"""中文文本的基础处理：分句、分段、删除替换后的标点清理、节奏统计。

这些函数不依赖任何第三方库，可单独测试。
"""

from __future__ import annotations

import re
import statistics

# 句末标点（中英文都算）
SENTENCE_END = "。！？!?；;…"
# 分句时保留的标点
_SPLIT_RE = re.compile(r"[^。！？!?；;…\n]*[。！？!?；;…]+|[^。！？!?；;…\n]+")


def split_sentences(text: str) -> list[str]:
    """把文本切成句子，保留句末标点；换行也视为句边界。

    >>> split_sentences("今天下雨。我没带伞！")
    ['今天下雨。', '我没带伞！']
    """
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        out.extend(m.group(0).strip() for m in _SPLIT_RE.finditer(line) if m.group(0).strip())
    return out


def split_paragraphs(text: str) -> list[str]:
    """按空行/换行切段，去掉空段。"""
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def cleanup_punct(text: str) -> str:
    """清理「删除型替换」留下的标点垃圾。

    删掉「综上所述，」这类词之后，很容易留下孤立的逗号、连续句号、
    行首标点。这里做一轮保守的收尾清理，只动标点，不动文字。
    """
    if not text:
        return text
    t = text
    # 逗号/顿号紧跟逗号顿号：去掉前一个
    t = re.sub(r"[，、]\s*(?=[，、])", "", t)
    # 句末标点紧跟逗号顿号：去掉逗号顿号
    t = re.sub(r"([。！？；])\s*[，、]+", r"\1", t)
    # 逗号顿号紧跟句末标点：去掉逗号顿号
    t = re.sub(r"[，、]+\s*(?=[。！？；])", "", t)
    # 重复的句末标点
    t = re.sub(r"([。！？])\s*(?=[。！？])", "", t)
    t = re.sub(r"；\s*(?=；)", "", t)
    # 行首的孤立标点（可能是删词后剩下的）
    t = re.sub(r"(?m)^[\s，、。！？；：]+", "", t)
    # 行尾多余的逗号顿号
    t = re.sub(r"(?m)[，、]+\s*$", "", t)
    # 冒号后紧跟句末标点
    t = re.sub(r"：\s*(?=[。！？；])", "", t)
    # 空格与空行收敛
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"[ \t]+(?=\n)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def rhythm_stats(text: str) -> dict:
    """统计句子节奏。AI 文本的典型特征是句长高度均匀、长句偏多。"""
    sentences = split_sentences(text)
    paragraphs = split_paragraphs(text)
    lengths = [len(s) for s in sentences]

    if not lengths:
        return {
            "sentenceCount": 0,
            "paragraphCount": len(paragraphs),
            "avgSentenceLen": 0,
            "sentenceLenStd": 0,
            "longSentenceRatio": 0.0,
            "rhythmVariance": 0.0,
        }

    avg = sum(lengths) / len(lengths)
    std = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    long_ratio = sum(1 for n in lengths if n > 40) / len(lengths)

    return {
        "sentenceCount": len(lengths),
        "paragraphCount": len(paragraphs),
        "avgSentenceLen": round(avg, 1),
        "sentenceLenStd": round(std, 1),
        "longSentenceRatio": round(long_ratio, 3),
        # 变异系数：越小说明句子越"整齐划一"，机器感越强
        "rhythmVariance": round(std / avg, 3) if avg else 0.0,
    }


def mask_preview(text: str, limit: int = 40) -> str:
    """生成用于列表展示的摘要。"""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def count_cjk(text: str) -> int:
    """统计文本长度（按字符计，与小程序端「字数」保持一致）。"""
    return len(text.strip())
