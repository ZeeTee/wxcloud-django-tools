"""Skill 子系统：加载、编译、输出解析。

引擎（``deai.engine``）不依赖这里的任何东西 —— 规则层可以完全脱离 skill 运行，
skill 只影响「深度改写」那一步。

    from deai.skills import get_registry, parse

    compiled = get_registry().compile("humanizer", scene="xhs")
    parsed = parse(model_output)
"""

from __future__ import annotations

from .output import ParsedOutput, parse
from .registry import (
    CompiledSkill,
    SkillError,
    SkillInfo,
    SkillRegistry,
    get_registry,
    reset_registry,
)

__all__ = [
    "CompiledSkill",
    "SkillError",
    "SkillInfo",
    "SkillRegistry",
    "get_registry",
    "reset_registry",
    "ParsedOutput",
    "parse",
]
