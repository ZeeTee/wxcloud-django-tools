"""从 skill 的 ``references/banned-words.md`` 里提取规则。

为什么要做这件事
----------------
skill 的禁用词表和规则引擎的 ``lexicon/rules.json`` 是**两份各说各话的数据**：
模型看得见前者，而免费、毫秒级的「体检报告」只认后者。结果是用户看到体检报告说
「这篇挺像人写的」，深度改写却把「仿佛」「眼中闪过一丝」全改掉了——两处结论打架。

把这份 markdown 解析成规则后，**单一数据源**：改词表，模型侧和规则侧同时生效。

为什么只标记、不自动替换
------------------------
这张表里的词大多是**文学/网文语境**的：「坚定」「深邃」「仿佛」「缓缓」。
机械替换会直接毁掉句子（「仿佛能穿透一切」→ 删掉「仿佛」就成了病句）。
所以全部走 ``flag``（只标记、给建议），由体检报告呈现给用户。规则层宁可改得少，
也不能产出病句——这条原则在 ``rules.py`` 里已经贯彻过一次了。
"""

from __future__ import annotations

import re

# 项目符号行：- "值得一提的是" / - 一切...都...
_BULLET_RE = re.compile(r"^[-*+]\s+(.+?)\s*$")
# markdown 表格行
_TABLE_RE = re.compile(r"^\|(.+)\|$")
# 表格分隔行：|---|---|
_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")
# 顿号/斜杠分隔的词表
_WORD_SPLIT_RE = re.compile(r"[、，,/]")
# 形如「不是A，而是B」「一边...一边...」的模板：这是句式不是词，
# 已经由 lexicon/rules.json 里的 36 条结构正则覆盖，这里跳过避免污染
_TEMPLATE_RE = re.compile(r"[A-Z](?![a-z])|x{2,}|\.\.\.|…|以上|以下")
# 括号里的补充说明
_NOTE_RE = re.compile(r"[（(]([^）)]+)[）)]")
# 只保留看着像「词/短语」的项
_VALID_ITEM_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9·\-]+$")

# 表格表头单元格：这些是列名，不是词
_HEADER_CELLS = {
    "毒级", "句式", "错误例", "修法", "书面腔", "口语化替换",
    "类型", "说明", "示例", "类别", "词", "替换", "例句", "问题",
}
# 这些章节讲的是「出处/版本」而不是词，整节跳过
_SKIP_SECTIONS = ("参考", "来源", "附录", "修订", "更新", "版本", "出处", "引用")


def _clean(text: str) -> str:
    """去掉引号、书名号、括号说明，留下词本身。"""
    t = text.strip()
    t = _NOTE_RE.sub("", t)
    for ch in "\"\u201c\u201d\u300c\u300d\u300e\u300f'`":
        t = t.replace(ch, "")
    return t.strip(" \u3002\uff0c,\u3001")


def _is_usable(item: str) -> bool:
    """判断一项是否值得做成规则。

    过滤四类噪声：模板句式（含 A/B/xxx/... 占位）、纯英文（参考资料里的项目名，
    如 ``Humanizer-zh``）、描述性长句、标点残留。
    """
    if not item or len(item) < 2 or len(item) > 12:
        return False
    if _TEMPLATE_RE.search(item):
        return False
    # 纯 ASCII 的条目几乎都是参考资料链接文本，不是中文禁用词
    if item.isascii():
        return False
    return bool(_VALID_ITEM_RE.match(item))


def parse_banned_words(markdown: str) -> list[dict]:
    """解析禁用词表，返回规则列表（均为只标记项）。

    每条形如::

        {"pattern": "仿佛", "reason": "...", "severity": "high", "source": "skill:banned-words"}
    """
    rules: list[dict] = []
    seen: set[str] = set()
    severity = "medium"
    section = ""
    note = ""
    skip_section = False
    in_code = False

    def add(item: str, why: str) -> None:
        item = _clean(item)
        if not _is_usable(item) or item in seen:
            return
        seen.add(item)
        rules.append(
            {
                "pattern": item,
                "reason": why or "skill 禁用词表里的高风险表达",
                "severity": severity,
                "source": "skill:banned-words",
            }
        )

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue

        # 代码块里的内容多半是示例代码，不是词
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue

        # ---- 章节标题：决定严重度，并跳过「参考来源」这类非词表章节 ----
        if line.startswith("#"):
            skip_section = any(k in line for k in _SKIP_SECTIONS)
            if "最毒" in line or "一级" in line or "填充短语" in line or "虚假宣告" in line:
                severity = "high"
            else:
                severity = "medium"
            section = line.lstrip("#").strip()
            note = ""
            continue
        if skip_section:
            continue

        if _TABLE_SEP_RE.match(line):
            continue

        # ---- 表格行 ----
        if _TABLE_RE.match(line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            # 表头行（列名）不是词
            if cells and any(c in _HEADER_CELLS for c in cells):
                continue
            if len(cells) >= 2:
                add(cells[0], cells[-1] if len(cells) >= 3 else section)
            continue

        # ---- 括号说明：给紧随其后的词当理由（例如「角色口语…时可保留」）----
        m_note = _NOTE_RE.match(line)
        if m_note:
            note = m_note.group(1).strip()
            continue

        # ---- 项目符号 ----
        m_bullet = _BULLET_RE.match(line)
        if m_bullet:
            add(m_bullet.group(1), note or section)
            continue

        # ---- 顿号/斜杠分隔的词表 ----
        if "、" in line or "/" in line:
            for part in _WORD_SPLIT_RE.split(line):
                add(part, note or section)
            continue

        # ---- 单个词一行 ----
        add(line, note or section)

    return rules


__all__ = ["parse_banned_words"]
