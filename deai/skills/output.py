"""解析模型返回的 ``<REPORT>`` / ``<REWRITTEN>`` 协议。

模型不一定会守规矩：可能漏闭合标签、加一句「以下是改写结果」、把正文包进
代码块、或者干脆忽略协议返回 markdown 小节。

所以解析必须是**多级容错**，而且无论多离谱都要给出一个可用的正文——
最差情况把整段当正文，让前端回落到规则版。绝不因为解析失败就丢弃模型产出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 闭合标签可缺失：\Z 兜住「只有开标签」的情况
_REWRITTEN_RE = re.compile(r"<\s*REWRITTEN\s*>(.*?)(?:<\s*/\s*REWRITTEN\s*>|\Z)", re.S | re.I)
_REPORT_RE = re.compile(r"<\s*REPORT\s*>(.*?)(?:<\s*/\s*REPORT\s*>|\Z)", re.S | re.I)

# 模型忽略协议时，退而求其次认这些小节标题
_MD_SECTION_RE = re.compile(
    r"^#{1,4}\s*(?:润色后全文|修改后全文|改写后全文|改写结果|润色后|改写后|正文)\s*$",
    re.M,
)
_MD_ANY_SECTION_RE = re.compile(r"^#{1,4}\s+\S", re.M)
_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*\n(.*?)\n?\s*```\s*$", re.S)


@dataclass(frozen=True)
class ParsedOutput:
    text: str
    report: str
    parsed: bool
    fallback: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "report": self.report,
            "parsed": self.parsed,
            "fallback": self.fallback,
        }


def _strip_fence(text: str) -> str:
    m = _CODE_FENCE_RE.match(text)
    return m.group(1).strip() if m else text.strip()


def _from_markdown_section(raw: str) -> str | None:
    """从 ``## 润色后全文`` 这类小节里取正文，砍掉后面的小节。"""
    m = _MD_SECTION_RE.search(raw)
    if not m:
        return None
    rest = raw[m.end() :]
    nxt = _MD_ANY_SECTION_RE.search(rest)
    if nxt:
        rest = rest[: nxt.start()]
    return rest.strip() or None


def parse(raw: str) -> ParsedOutput:
    """把模型原始输出解析成 ``(正文, 报告)``。"""
    if not raw or not raw.strip():
        return ParsedOutput("", "", False, "empty")

    source = raw.strip()

    report = ""
    m = _REPORT_RE.search(source)
    if m:
        report = m.group(1).strip()

    # 1) 首选：协议标签
    m = _REWRITTEN_RE.search(source)
    if m:
        text = _strip_fence(m.group(1))
        if text:
            return ParsedOutput(text, report, True, "")

    # 2) 退而求其次：markdown 小节标题
    section = _from_markdown_section(source)
    if section:
        return ParsedOutput(_strip_fence(section), report, False, "markdown_section")

    # 3) 整段被代码块包住
    if _CODE_FENCE_RE.match(source):
        return ParsedOutput(_strip_fence(source), report, False, "code_fence")

    # 4) 只有 REPORT 没有 REWRITTEN：把报告之外的残留当正文
    if report:
        rest = _REPORT_RE.sub("", source).strip()
        if rest:
            return ParsedOutput(_strip_fence(rest), report, False, "report_only")

    # 5) 彻底不守协议：整段当正文，宁可多留也不要丢
    return ParsedOutput(_strip_fence(source), report, False, "raw")


__all__ = ["ParsedOutput", "parse"]
