"""Skill 的加载与编译。

为什么需要「编译」
------------------
Humanizer 是为**有文件工具的 agent** 写的：正文里反复出现
「（加载 ``references/banned-words.md``）」。而产品后台是单向 LLM 调用，
模型手里只有我们塞进 prompt 的东西，它没法自己去读文件。

所以 SKILL.md 不能原样当 system prompt 用，必须先编译：

1. 剥掉 YAML frontmatter —— 那是给 agent 做路由用的，模型不需要看；
2. 把「加载 references/xxx.md」改写成「见【附录 X】」；
3. 按 ``skill.json`` 的 ``inject`` 规则，把 references 正文拼成附录；
4. 追加场景覆盖（``overrides/<scene>.json``）；
5. 追加输出协议，让返回结果可被稳定解析。

第 5 步很关键：原始 skill 用 markdown 标题（``## 润色后全文``）分隔输出，
但模型完全可能在改写正文里写出同样的标题，导致解析错位。改用 XML 标签。

Skill 目录是随镜像发布的只读内容，所以编译结果可以安全缓存；
真要热更新时调用 ``reload()`` 即可。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = Path(__file__).resolve().parent

# YAML frontmatter（只在文件开头）
_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
# 「（加载 references/xxx.md）」/「加载 references/xxx.md 核查」
_LOAD_HINT_RE = re.compile(r"[（(]?\s*加载\s+references/([A-Za-z0-9_\-]+)\.md\s*[）)]?")

# 输出协议：用 XML 标签而不是 markdown 标题做分隔，正文里出现标题也不会解析错位
_OUTPUT_PROTOCOL = """
---
【输出协议（必须严格遵守）】

严格按下面两个标签输出，标签之外不要写任何字：

<REPORT>
AI味等级：{轻度/中度/重度}
主要问题：{1-3 个关键词}
总修改数：{N} 处
</REPORT>
<REWRITTEN>
{润色后的全文}
</REWRITTEN>

硬性要求：
1. <REWRITTEN> 里只放正文，不要加标题、说明、代码块，也不要用引号整体包裹。
2. 正文里不得出现 <REPORT> 或 <REWRITTEN> 字样。
3. 不新增原文没有的事实、数字、人名、案例、来源。原文没有的细节不要编，
   宁可保留抽象表达——「补具体细节」只在原文已有素材的前提下做。
4. 保持原文的观点方向与信息量，不要因为改风格而删掉实质信息点。
"""


class SkillError(RuntimeError):
    """skill 缺失或格式非法。这类问题应该在启动/构建阶段就暴露。"""


@dataclass(frozen=True)
class SkillInfo:
    slug: str
    name: str
    version: str
    description: str
    scenes: tuple[str, ...]
    default_scene: str
    prompt_chars: int

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "scenes": list(self.scenes),
            "defaultScene": self.default_scene,
        }


@dataclass(frozen=True)
class CompiledSkill:
    slug: str
    version: str
    scene: str
    system_prompt: str
    injected: tuple[str, ...] = ()
    max_rounds: int = 1

    @property
    def prompt_chars(self) -> int:
        return len(self.system_prompt)


@dataclass
class _RawSkill:
    slug: str
    manifest: dict
    root: Path
    info: SkillInfo
    body: str
    _compiled: dict = field(default_factory=dict)


class SkillRegistry:
    """扫描 ``deai/skills/<slug>/``，按需编译成可直接使用的 system prompt。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self._skills: dict[str, _RawSkill] = {}
        self.reload()

    # -- 加载 ---------------------------------------------------------------

    def reload(self) -> None:
        self._skills.clear()
        if not self.root.is_dir():
            logger.warning("skill 目录不存在：%s", self.root)
            return
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            manifest_path = child / "skill.json"
            system_path = child / "SKILL.md"
            if not manifest_path.is_file() or not system_path.is_file():
                continue
            try:
                self._skills[child.name] = self._load_one(child, manifest_path, system_path)
            except (OSError, ValueError, KeyError) as exc:
                # 单个 skill 坏掉不该让整个服务起不来，但必须吼一声
                logger.error("加载 skill %s 失败：%s", child.name, exc)
        logger.info("已加载 %d 个 skill：%s", len(self._skills), ", ".join(self._skills))

    def _load_one(self, root: Path, manifest_path: Path, system_path: Path) -> _RawSkill:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        slug = str(manifest.get("slug") or root.name)
        body = system_path.read_text(encoding="utf-8")
        body = _FRONTMATTER_RE.sub("", body, count=1).strip()
        if not body:
            raise SkillError(f"{system_path} 去掉 frontmatter 后是空的")

        scenes = tuple(manifest.get("scenes") or ["general"])
        default_scene = str(manifest.get("defaultScene") or scenes[0])
        info = SkillInfo(
            slug=slug,
            name=str(manifest.get("name") or slug),
            version=str(manifest.get("version") or "0"),
            description=str(manifest.get("description") or ""),
            scenes=scenes,
            default_scene=default_scene,
            prompt_chars=len(body),
        )
        return _RawSkill(slug=slug, manifest=manifest, root=root, info=info, body=body)

    # -- 查询 ---------------------------------------------------------------

    def list(self) -> list[SkillInfo]:
        return [s.info for s in self._skills.values()]

    def has(self, slug: str) -> bool:
        return slug in self._skills

    def stats(self) -> dict:
        return {
            "count": len(self._skills),
            "skills": [s.info.to_dict() for s in self._skills.values()],
        }

    # -- 编译 ---------------------------------------------------------------

    def compile(self, slug: str, scene: str | None = None) -> CompiledSkill:
        raw = self._skills.get(slug)
        if raw is None:
            raise SkillError(f"未知的 skill：{slug}")
        chosen = scene or raw.info.default_scene
        if chosen not in raw.info.scenes:
            chosen = raw.info.default_scene
        if chosen in raw._compiled:
            return raw._compiled[chosen]

        compiled = self._compile(raw, chosen)
        raw._compiled[chosen] = compiled
        return compiled

    def _compile(self, raw: _RawSkill, scene: str) -> CompiledSkill:
        inject_specs = list(raw.manifest.get("inject") or [])
        active = [spec for spec in inject_specs if str(spec.get("when")) == "always"]
        label_of = {str(spec.get("ref")): str(spec.get("label") or "?") for spec in active}

        # 1) 把正文里的「加载 references/xxx.md」改写成附录引用
        body = _LOAD_HINT_RE.sub(lambda m: self._hint_replacement(m, label_of), raw.body)

        parts = [body]
        injected: list[str] = []

        # 2) 拼接附录
        for spec in active:
            ref = str(spec.get("ref"))
            rel = str(spec.get("path") or "")
            path = raw.root / rel
            if not rel or not path.is_file():
                logger.warning("skill %s 的 references 缺失：%s", raw.slug, rel)
                continue
            content = path.read_text(encoding="utf-8").strip()
            title = str(spec.get("title") or ref)
            parts.append(f"\n---\n【附录 {spec.get('label')}】{title}\n\n{content}")
            injected.append(ref)

        # 3) 场景覆盖
        override = self._load_override(raw, scene)
        if override:
            parts.append("\n---\n" + override)

        # 4) 输出协议
        parts.append(_OUTPUT_PROTOCOL)

        rounds = raw.manifest.get("rounds") or {}
        return CompiledSkill(
            slug=raw.slug,
            version=raw.info.version,
            scene=scene,
            system_prompt="\n".join(parts),
            injected=tuple(injected),
            max_rounds=int(rounds.get("medium", 1) or 1),
        )

    @staticmethod
    def _hint_replacement(match: re.Match[str], label_of: dict[str, str]) -> str:
        ref = match.group(1)
        label = label_of.get(ref)
        # 没被注入的 references 不能留下「见附录」的死引用，直接抹掉
        return f"见【附录 {label}】" if label else ""

    def _load_override(self, raw: _RawSkill, scene: str) -> str:
        path = raw.root / "overrides" / f"{scene}.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skill %s 的场景覆盖 %s 解析失败：%s", raw.slug, scene, exc)
            return ""
        extra = str(data.get("extra") or "").strip()
        return extra


_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """进程级单例。"""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def reset_registry() -> None:
    """测试用：丢掉单例，下次重新扫描磁盘。"""
    global _registry
    _registry = None


__all__ = [
    "SkillError",
    "SkillInfo",
    "CompiledSkill",
    "SkillRegistry",
    "get_registry",
    "reset_registry",
]
