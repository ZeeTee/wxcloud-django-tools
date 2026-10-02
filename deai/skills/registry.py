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

import hashlib
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

# 用户指定的改写强度。skill 正文里本来就有一套「按文本自动分级」的流程，
# 这里额外给用户一个显式旋钮：两者结合，用户的指定优先。
_INTENSITY_HINTS = {
    "light": """
【本次强度：轻度】
用户要求保守改写。请严格照做：
- 只处理最明显的问题：禁用词、禁用标点、"不是A而是B"三毒句式。
- **不要重写句子结构**，不要调整段落顺序，不要改变叙述人称。
- 能改一个词就不改一句，能删一句就不重写一段。
- 原文读起来正常的句子一律保留原样。

正反例（务必照「轻度」一侧写）：
  原文：首先，我们要明确目标。其次，要不断优化流程。
  轻度（✓）：我们要明确目标。要不断优化流程。     ← 只删了连接词
  过度（✗）：先把目标定下来，流程再一点点改。      ← 重写了句子结构
  原文：综上所述，这件事至关重要。
  轻度（✓）：这件事至关重要。                    ← 只删了收束词
  过度（✗）：这事挺要紧的。                       ← 换了口语说法，超出轻度范围
""",
    "heavy": """
【本次强度：重度】
用户要求彻底改写。
- 执行完整的 Pass 1 / Pass 2 / Pass 3，逐段处理，不留死角。
- 长句该拆就拆，该打碎的节奏主动打碎。
- 结尾的升华、总结、口号整段删掉。
- 但仍然**不得新增原文没有的事实**——强度高指的是改得彻底，不是编得多。
""",
}
_OUTPUT_PROTOCOL = """
---
【输出协议（必须严格遵守）】

严格按下面两个标签输出，标签之外不要写任何字：

<REPORT>
AI味等级：{轻度/中度/重度}
主要问题：{1-3 个关键词}
总修改数：{N} 处
新增事实：{无 / 逐条列出}
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

关于「新增事实」这一行——这是自检，必须诚实：
- 如果你**完全**没有添加原文没有的内容，写「无」。
- 如果你为了让文字更具体而补充了原文没有的东西（举例、场景、动作、细节、
  数据），必须逐条列出来，哪怕只是一句「补了『填表、整理数据』这类举例」。
- 宁可多报也不要漏报。这一行会直接展示给用户，用来提醒他核对。
- 注意：把原文的抽象说法**换一种说法**（「提升效率」→「快了不少」）不算新增；
  凭空补出原文没有的**具体内容**才算。
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
    intensity: str = "medium"

    @property
    def prompt_chars(self) -> int:
        return len(self.system_prompt)

    @property
    def fingerprint(self) -> str:
        """编译后 prompt 的短哈希。

        ``skill.json`` 里的 ``version`` 是**手写的**，改了内容忘记 bump 是常事，
        版本号会说谎。指纹是自动算的，能精确回答「这次任务用的到底是哪份 prompt」。

        它的实际用途：配合用户反馈，可以看出「改了 prompt 之后好评率有没有变化」。
        skill 随镜像发布，回滚靠切镜像版本，指纹则告诉你该回滚到哪一版。
        """
        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()[:12]


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

    def compile(
        self, slug: str, scene: str | None = None, intensity: str | None = None
    ) -> CompiledSkill:
        """编译成可直接用的 system prompt。

        ``scene``（场景）与 ``intensity``（强度）任一不合法时都回落到默认值，
        不抛异常——这两个值直接影响的是措辞，不是正确性，没必要让请求失败。
        """
        raw = self._skills.get(slug)
        if raw is None:
            raise SkillError(f"未知的 skill：{slug}")

        chosen_scene = scene or raw.info.default_scene
        if chosen_scene not in raw.info.scenes:
            chosen_scene = raw.info.default_scene

        intensities = tuple(raw.manifest.get("intensities") or ("medium",))
        chosen_intensity = intensity or str(raw.manifest.get("defaultIntensity") or "medium")
        if chosen_intensity not in intensities:
            chosen_intensity = "medium" if "medium" in intensities else intensities[0]

        key = (chosen_scene, chosen_intensity)
        if key in raw._compiled:
            return raw._compiled[key]

        compiled = self._compile(raw, chosen_scene, chosen_intensity)
        raw._compiled[key] = compiled
        return compiled

    def _compile(self, raw: _RawSkill, scene: str, intensity: str) -> CompiledSkill:
        inject_specs = list(raw.manifest.get("inject") or [])
        active = [spec for spec in inject_specs if str(spec.get("when")) == "always"]
        label_of = {str(spec.get("ref")): str(spec.get("label") or "?") for spec in active}

        # 1) 把正文里的「加载 references/xxx.md」改写成附录引用
        body = _LOAD_HINT_RE.sub(lambda m: self._hint_replacement(m, label_of), raw.body)

        parts: list[str] = []
        injected: list[str] = []

        # 2) 场景覆盖**放在最前面**，并明确声明它优先。
        #    实测教训：放在末尾时会被上面一万多字的通用方法论淹没——academic 场景
        #    要求「不要口语化」，模型照样写成「这两年」「越堆越多」。
        override = self._load_override(raw, scene)
        if override:
            parts.append(override)
            parts.append(
                "\n---\n"
                "【重要】以上是本次请求的场景补充。下面的通用方法论中，"
                "任何与场景补充冲突的要求（尤其是标点禁令、口语化程度、语体风格）"
                "**一律以场景补充为准**。\n"
            )

        parts.append(body)

        # 3) 拼接附录
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

        # 4) 强度提示（medium 是 skill 的默认行为，不需要额外说明）
        hint = _INTENSITY_HINTS.get(intensity)
        if hint:
            parts.append("\n---\n" + hint.strip())

        # 5) 输出协议
        parts.append(_OUTPUT_PROTOCOL)

        rounds = raw.manifest.get("rounds") or {}
        return CompiledSkill(
            slug=raw.slug,
            version=raw.info.version,
            scene=scene,
            system_prompt="\n".join(parts),
            injected=tuple(injected),
            max_rounds=int(rounds.get(intensity, rounds.get("medium", 1)) or 1),
            intensity=intensity,
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
