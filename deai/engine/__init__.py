"""去 AI 味引擎（纯 Python，无第三方依赖、不联网）。

对外主要入口::

    from deai.engine import analyze_text, rewrite_by_rules, build_report

    rewritten, hits = rewrite_by_rules(text)
    report = build_report(text, hits)
"""

from __future__ import annotations

from .detector import build_report
from .llm import LLMError, is_configured
from .rewriter import rewrite
from .rules import Hit, RuleEngine, get_engine

__all__ = [
    "Hit",
    "RuleEngine",
    "get_engine",
    "build_report",
    "rewrite_by_rules",
    "rewrite",
    "LLMError",
    "is_configured",
]


def rewrite_by_rules(text: str) -> tuple[str, list[Hit]]:
    """只走规则层：毫秒级返回 ``(改写后文本, 命中列表)``。"""
    return get_engine().analyze(text)
