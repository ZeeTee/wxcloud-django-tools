"""规则层引擎：AI 味词库的加载、命中检测与「可安全执行」的替换。

设计要点
--------
1. 词库来自 ``lexicon/rules.json``：177 条替换 + 95 条只标记项 + 36 条结构正则。
2. 含省略号 ``…`` 的规则视为**带捕获的模板**，如 ``对…进行优化`` → ``优化…``：
   编译成正则 ``对(.{0,12}?)进行优化``，替换时把捕获组回填到 ``to`` 的省略号位置。
3. 采用「长串优先 + 区间占用」扫描，避免 ``此外`` 抢先吃掉 ``此外还有``。
4. ``conditional`` 与 ``flagged`` **只标记不改写**：它们强依赖上下文
   （「首先」在教程步骤里是合理的），盲替会把文章改坏。它们的 ``to`` 作为
   「建议」展示在体检报告里，由用户自己决定是否采纳。

本模块不依赖 Django，也不联网，可脱离项目单独运行与测试。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .textutil import cleanup_punct

# 词库目录刻意叫 lexicon 而不是 data：
# `data/` 是 .gitignore / .dockerignore 里的常见条目，而 Git 的无斜杠模式会匹配
# **任意层级**的同名目录，会把词库一起忽略掉（曾因此让 49KB 的词库没进仓库，
# 部署后引擎直接 FileNotFoundError）。换掉目录名比加 `!` 例外可靠。
_DATA_FILE = Path(__file__).resolve().parent / "lexicon" / "rules.json"

# 模板规则里 `…` 最多吃掉多少个字符（避免跨句贪婪匹配）
_MAX_GAP = 12
# 结构正则与词汇命中区间的重叠比例超过该值即视为重复，不再单独计数
_OVERLAP_IOU = 0.5

# 「需看上下文」的词：只有当对应结构模式确实成立时才升级为可删除
_CONTEXT_DELETIONS: dict[str, tuple[str, ...]] = {
    "P01": ("首先", "其次", "再次", "最后", "接下来"),
}
# 升级为删除所需的模式命中次数（比报告门槛更严，因为删除不可逆）
_UPGRADE_MIN_MATCHES = 2

# 严重度排序：数值越小越严重
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------------------
# 语义分类：把命中归到人话类别，用于「体检报告」按类聚合
# ---------------------------------------------------------------------------

_CATEGORY_SPECS: list[tuple[str, str, str]] = [
    (
        "connector",
        "套话连接词",
        r"首先|其次|再次|最后|综上|总而言之|总的来说|概括而言|归根结底|由此可见|"
        r"不难发现|不难看出|值得(注意|一提|关注)的是|需要(指出|明白)的是|不可否认|"
        r"毋庸置疑|不言而喻|众所周知|毫无疑问|此外|另外|与此同时|与之相对|"
        r"更重要的是|不仅如此|除此之外|接下来|从而",
    ),
    ("opening", "开头铺垫", r"在当今|在实际应用中|在实践中|随着|21\s?世纪|在…的背景下"),
    (
        "empty_adj",
        "空洞形容词/副词",
        r"极大地|显著地|有效地|深刻地|深远地|至关重要|不可或缺|举足轻重|"
        r"必不可少|令人瞩目|可圈可点",
    ),
    (
        "hollow_result",
        "空话式成效",
        r"取得了显著成效|发挥了重要作用|起到了|有着重要意义|具有里程碑|标志着|"
        r"注入了新的活力|提供了有力支撑|奠定了坚实基础|树立了新的标杆|产生了深远影响",
    ),
    (
        "closing",
        "结尾套话",
        r"让我们一起|携起手来|携手前行|共创美好|未来可期|拭目以待|大有可为|前景广阔|"
        r"充满无限可能|我们有理由相信|相信在未来|希望本文|希望以上|如有疑问|"
        r"欢迎在评论区|以上就是我的分享",
    ),
    (
        "meta",
        "元评论/导览",
        r"本文将从|接下来我将|接下来我们来看|下面我们来看|在开始之前|让我们来|"
        r"我想说的是|回到正题",
    ),
    (
        "translation",
        "译制腔",
        r"对…进行|进行了|加以解决|给予帮助|使…得到|起到了促进|对于…而言|"
        r"就目前而言|作为一个|被誉为|堪称|可谓|称得上|不啻为|通过…来|在…方面|呈现出",
    ),
    (
        "buzzword",
        "互联网黑话",
        r"赋能|助力|深耕|打造|聚焦|抓手|闭环|打通|底层逻辑|认知升级|颗粒度|心智|"
        r"链路|降本增效|数智化|新质生产力|痛点|爽点|赛道|破圈|指明方向|奠定基础",
    ),
    ("hedge", "虚化副词", r"很大程度上|在一定程度上|在某种意义上|进一步|不断|持续地|更好地|愈发|切实|真正地"),
    (
        "idiom",
        "成语/烂比喻堆砌",
        r"双刃剑|新时代的石油|欣欣向荣|蓬勃发展|日新月异|如火如荼|方兴未艾|势不可挡|"
        r"砥砺前行|勇立潮头|精益求精|匠心独运|锦上添花|画龙点睛|一目了然|得心应手|"
        r"事半功倍|游刃有余|息息相关|相辅相成",
    ),
    ("written_verb", "书面动词", r"进行|实现|具备|拥有|旨在|致力于|获取"),
]

_CATEGORY_COMPILED = [(k, n, re.compile(p)) for k, n, p in _CATEGORY_SPECS]
_OTHER_CATEGORY = ("other", "其他 AI 痕迹")


def classify(text: str) -> tuple[str, str]:
    """把一段命中文本归入语义类别，返回 ``(key, 中文名)``。"""
    for key, name, regex in _CATEGORY_COMPILED:
        if regex.search(text):
            return key, name
    return _OTHER_CATEGORY


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    """一次命中。``start``/``end`` 是相对**原文**的字符下标。"""

    start: int
    end: int
    text: str
    kind: str  # replace（已改写）| flag（仅标记）
    severity: str  # high | medium | low
    reason: str
    suggestion: str | None
    category: str
    category_name: str
    rule_id: str

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "kind": self.kind,
            "severity": self.severity,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "category": self.category,
            "categoryName": self.category_name,
            "ruleId": self.rule_id,
        }


@dataclass
class _Edit:
    """一条对文本的实际改动指令。

    与 ``Hit`` 分开维护：``Hit`` 服务于体检报告，会因为「择优去重」被替换或
    合并；``_Edit`` 服务于改写，必须始终保持完整，否则会出现「报告说该删，
    改写却没删」的不一致。
    """

    start: int
    end: int
    replacement: str


@dataclass
class _Rule:
    rule_id: str
    mode: str  # unconditional | conditional | delete | flag
    severity: str
    reason: str
    suggestion: str | None
    category: str
    category_name: str
    regex: re.Pattern[str] | None  # None 表示纯字面量，用 str.find 加速
    literal: str | None
    replacement: str | None  # 仅 replace/delete 有效
    # 结构正则专用
    pattern_name: str = ""
    # 命中阈值条件列表 [(标称次数, 单位)]，单位 kchar=每千字 / doc=全篇 / para=每段
    thresholds: tuple[tuple[int, str], ...] = ((1, "doc"),)
    source: str = ""  # 原始 from 文本，用于「长串优先」排序

    @property
    def applies(self) -> bool:
        """是否真的改写文本（conditional / flag 不改写）。"""
        return self.mode in ("unconditional", "delete")

    def finditer(self, text: str):
        if self.literal is not None:
            if self.literal not in text:
                return []
            return re.finditer(re.escape(self.literal), text)
        assert self.regex is not None
        return self.regex.finditer(text)


def _severity_of(raw: object) -> str:
    s = str(raw or "medium").strip().lower()
    return s if s in ("high", "medium", "low") else "medium"


_NUM_RE = re.compile(r"(>=|≥|>)?\s*(\d+)")
# 阈值串里多个条件是「或」的关系，也可能用顿号/逗号分隔
_CLAUSE_SPLIT = re.compile(r"[或，,、；;]|以及")


def _per_of(clause: str) -> str:
    if "千字" in clause:
        return "kchar"
    if "段" in clause:
        return "para"
    return "doc"


def parse_thresholds(raw: object) -> tuple[tuple[int, str], ...]:
    """解析词库里的人类可读阈值，返回 ``((标称次数, 单位), ...)``。

    词库里的 ``threshold`` 不是数字而是描述串，且写法**不统一**，例如::

        ">=2 次/千字"                  -> ((2, "kchar"),)
        ">=1 次/全文"                  -> ((1, "doc"),)
        ">=2 次/段 或 >=3 次/千字"      -> ((2, "para"), (3, "kchar"))
        "全篇 >1 处，或单段 >=2 处"     -> ((2, "doc"), (2, "para"))
        "同一句内 >=2 个不同称呼"       -> ((2, "doc"),)

    注意后两种**没有斜杠**，单位要从子句里的「全篇 / 段 / 千字」推断。
    这个字段原先被当成 int 解析，会静默退化成 1，导致结构规则严重误报。
    """
    if isinstance(raw, bool):
        return ((1, "doc"),)
    if isinstance(raw, int):
        return ((max(raw, 1), "doc"),)

    s = str(raw or "").strip()
    if not s:
        return ((1, "doc"),)

    conds: list[tuple[int, str]] = []
    for clause in _CLAUSE_SPLIT.split(s):
        clause = clause.strip()
        if not clause:
            continue
        m = _NUM_RE.search(clause)
        if not m:
            continue
        num = int(m.group(2))
        if (m.group(1) or ">=") == ">":  # 「>1」意味着至少 2
            num += 1
        conds.append((max(num, 1), _per_of(clause)))

    if not conds:
        m2 = re.search(r"(\d+)", s)
        return ((int(m2.group(1)) if m2 else 1, "doc"),)
    return tuple(conds)


def required_matches(thresholds: tuple[tuple[int, str], ...], char_count: int) -> int:
    """把阈值条件换算成这篇文本实际需要的命中次数。

    复合条件（「或」）取各条件中**最保守**的一个：宁可少报，也不要误报。
    """
    best = 1
    for num, per in thresholds:
        if per == "kchar":
            need = max(1, math.ceil(num * max(char_count, 1) / 1000))
        else:
            need = max(num, 1)
        best = max(best, need)
    return best


def _build_template_regex(template: str) -> tuple[str, int]:
    """把 ``对…进行优化`` 编译成正则，返回 ``(正则串, 捕获组数)``。"""
    parts = template.split("…")
    # 用 % 而不是 f-string：花括号在正则里是量词，f-string 下要写成 {{ }}，容易被改错。
    # 间隔不允许跨句末标点/换行，否则「存在一定门槛。一方面」会被当成「在…方面」。
    gap = "([^。！？；!?;\n]{0,%d}?)" % _MAX_GAP
    return gap.join(re.escape(p) for p in parts), len(parts) - 1


def _fill_captures(target: str, groups: int) -> str:
    """把 ``to`` 里的省略号依次替换成捕获组引用。"""
    out = target
    for i in range(groups):
        if "…" not in out:
            break
        out = out.replace("…", f"\\g<{i + 1}>", 1)
    return out


@lru_cache(maxsize=1)
def _load_raw() -> dict:
    with _DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


class RuleEngine:
    """词库引擎。构造一次可复用；模块底部提供进程级单例。"""

    def __init__(self, data: dict | None = None) -> None:
        raw = data if data is not None else _load_raw()
        self.meta: dict = raw.get("meta", {})
        self._rules: list[_Rule] = []
        self._patterns: list[_Rule] = []
        self._build_replacements(raw.get("replacements", []))
        self._build_flagged(raw.get("flagged", []))
        self._build_patterns(raw.get("patterns", []))
        # 长串优先：避免短规则吃掉长规则的区间。
        # 必须用原始 from 文本排序 —— 模板规则的 literal 是 None，用它会全部排到最后。
        self._rules.sort(key=lambda r: -len(r.source))

    # -- 构建 ---------------------------------------------------------------

    def _build_replacements(self, items: list[dict]) -> None:
        for idx, item in enumerate(items):
            src = str(item.get("from", "")).strip()
            if not src:
                continue
            mode = str(item.get("mode", "unconditional")).strip() or "unconditional"
            if mode not in ("unconditional", "conditional", "delete"):
                mode = "conditional"
            to = str(item.get("to", "") or "")
            note = str(item.get("note", "") or "")

            key, name = classify(src)
            if "…" in src:
                body, groups = _build_template_regex(src)
                regex: re.Pattern[str] | None = re.compile(body)
                literal: str | None = None
                replacement: str | None = _fill_captures(to, groups) if mode != "conditional" else to
            else:
                regex = None
                literal = src
                replacement = to if mode != "conditional" else to

            self._rules.append(
                _Rule(
                    rule_id=f"R{idx:03d}",
                    mode=mode,
                    severity=_severity_of(item.get("severity")),
                    reason=note or ("可安全替换" if mode == "unconditional" else "需结合上下文判断"),
                    suggestion=(to or None),
                    category=key,
                    category_name=name,
                    regex=regex,
                    literal=literal,
                    replacement=replacement,
                    source=src,
                )
            )

    def _build_flagged(self, items: list[dict]) -> None:
        for idx, item in enumerate(items):
            src = str(item.get("pattern", "")).strip()
            if not src:
                continue
            reason = str(item.get("reason", "") or "")
            key, name = classify(src)
            self._rules.append(
                _Rule(
                    rule_id=f"F{idx:03d}",
                    mode="flag",
                    severity=_severity_of(item.get("severity")),
                    reason=reason or "高危 AI 用词",
                    suggestion=None,
                    category=key,
                    category_name=name,
                    regex=None,
                    literal=src,
                    replacement=None,
                    source=src,
                )
            )

    def _build_patterns(self, items: list[dict]) -> None:
        for item in items:
            raw_regex = str(item.get("regex", "")).strip()
            if not raw_regex:
                continue
            try:
                compiled = re.compile(raw_regex)
            except re.error:
                continue  # 词库里若有非法正则，跳过而不是让整站 500
            pid = str(item.get("id", "P??"))
            thresholds = parse_thresholds(item.get("threshold", 1))
            self._patterns.append(
                _Rule(
                    rule_id=f"pattern:{pid}",
                    mode="pattern",
                    severity=_severity_of(item.get("severity")),
                    reason=str(item.get("intent", "") or ""),
                    suggestion=None,
                    category=f"pattern_{pid}",
                    category_name=str(item.get("name", "") or pid),
                    regex=compiled,
                    literal=None,
                    replacement=None,
                    pattern_name=str(item.get("name", "") or pid),
                    thresholds=thresholds,
                    source=raw_regex,
                )
            )

    # -- 扫描 ---------------------------------------------------------------

    def _scan(self, text: str) -> tuple[list[Hit], list[_Edit]]:
        n = len(text)

        # 1) 先判定结构模式：只有达到阈值的模式才进入报告。
        #    同时记下「已成立」的模式，供上下文升级使用。
        active_patterns: list[tuple[_Rule, list[re.Match[str]]]] = []
        triggered: set[str] = set()
        for rule in self._patterns:
            matches = [m for m in rule.regex.finditer(text) if m.span()[1] > m.span()[0]]
            if len(matches) < required_matches(rule.thresholds, n):
                continue
            active_patterns.append((rule, matches))
            triggered.add(rule.rule_id)

        upgrade_words = self._upgrade_words(triggered, active_patterns)

        # 2) 词汇层：长串优先 + 区间占用
        taken = bytearray(n)
        hits: list[Hit] = []
        edits: list[_Edit] = []
        for rule in self._rules:
            for m in rule.finditer(text):
                s, e = m.span()
                if e <= s or any(taken[s:e]):
                    continue
                taken[s:e] = b"\x01" * (e - s)
                hit = self._make_hit(text, rule, s, e, m)

                if rule.applies:
                    edits.append(_Edit(s, e, rule.replacement or ""))
                elif hit.kind == "flag" and hit.text in upgrade_words:
                    # 上下文已确认这是「分点骨架」，此时删除才是安全的
                    hit.kind = "replace"
                    hit.suggestion = ""
                    hit.reason = "序数词已构成分点骨架，整段是模板结构，可以直接去掉"
                    edits.append(_Edit(s, e, ""))

                hits.append(hit)

        # 3) 结构层：与词汇命中重叠时「择优保留」——
        #    结构模式的解释力更强（「开头宏大背景」比「在当今」更有信息量），
        #    严重度不低时用它替换掉零散词汇命中，而不是先到先得直接丢弃。
        for rule, matches in active_patterns:
            for m in matches:
                self._merge_pattern_hit(hits, text, rule, m, upgrade_words)

        hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
        return hits, edits

    def _merge_pattern_hit(
        self,
        hits: list[Hit],
        text: str,
        rule: _Rule,
        m: re.Match[str],
        upgrade_words: set[str],
    ) -> None:
        s, e = m.span()
        new_hit = self._make_hit(text, rule, s, e, m)

        # 结构模式若同时触发了「序数词可删」，报告要如实反映它会被删掉，
        # 否则会出现「报告说只是标记、结果正文里它没了」的不一致。
        token = new_hit.text.strip("，,、。！？；： \n")
        if upgrade_words and token in upgrade_words:
            new_hit.kind = "replace"
            new_hit.suggestion = ""
            new_hit.reason = "序数词已构成分点骨架，整段是模板结构，可以直接去掉"

        dup = [i for i, h in enumerate(hits) if self._iou(s, e, h.start, h.end) > _OVERLAP_IOU]
        if not dup:
            hits.append(new_hit)
            return

        worst = min((hits[i] for i in dup), key=lambda h: _SEVERITY_RANK.get(h.severity, 9))
        if _SEVERITY_RANK.get(rule.severity, 9) <= _SEVERITY_RANK.get(worst.severity, 9):
            drop = set(dup)
            hits[:] = [h for i, h in enumerate(hits) if i not in drop]
            hits.append(new_hit)

    @staticmethod
    def _upgrade_words(
        triggered: set[str],
        active_patterns: list[tuple[_Rule, list[re.Match[str]]]],
    ) -> set[str]:
        """把「需看上下文」的词升级为可删除。

        例如「首先/其次/再次」单独出现时未必是 AI 味（教程步骤里很合理），
        但一旦「序数词分点骨架」模式成立，它们就是模板结构，可以放心删掉。
        删除不可逆，所以这里用比报告更严的门槛。
        """
        words: set[str] = set()
        for pid, vocab in _CONTEXT_DELETIONS.items():
            rid = f"pattern:{pid}"
            if rid not in triggered:
                continue
            for rule, matches in active_patterns:
                if rule.rule_id == rid and len(matches) >= _UPGRADE_MIN_MATCHES:
                    words.update(vocab)
        return words

    @staticmethod
    def _iou(a1: int, a2: int, b1: int, b2: int) -> float:
        inter = max(0, min(a2, b2) - max(a1, b1))
        if inter == 0:
            return 0.0
        union = max(a2, b2) - min(a1, b1)
        return inter / union if union else 0.0

    @staticmethod
    def _make_hit(text: str, rule: _Rule, s: int, e: int, m: re.Match[str]) -> Hit:
        if rule.mode == "pattern":
            suggestion = None
            reason = rule.reason
            kind = "flag"
        elif rule.mode == "conditional":
            suggestion = rule.suggestion
            reason = rule.reason
            kind = "flag"
        else:
            suggestion = rule.replacement
            reason = rule.reason
            kind = "replace"
        return Hit(
            start=s,
            end=e,
            text=m.group(0),
            kind=kind,
            severity=rule.severity,
            reason=reason,
            suggestion=suggestion,
            category=rule.category,
            category_name=rule.category_name,
            rule_id=rule.rule_id,
        )

    # -- 对外 ---------------------------------------------------------------

    def scan(self, text: str) -> list[Hit]:
        """只体检，不改写。"""
        hits, _ = self._scan(text)
        return hits

    def analyze(self, text: str) -> tuple[str, list[Hit]]:
        """返回 ``(规则层改写后的文本, 全部命中)``。

        只有 ``unconditional``、``delete``，以及「上下文已确认」的序数词会真正
        改动文本；``conditional``/``flag``/结构正则仅作为体检结论出现。
        """
        hits, edits = self._scan(text)
        if not edits:
            return text, hits

        pieces: list[str] = []
        cursor = 0
        for ed in sorted(edits, key=lambda x: x.start):
            if ed.start < cursor:  # 区间重叠时以先出现者为准
                continue
            pieces.append(text[cursor : ed.start])
            pieces.append(ed.replacement)
            cursor = ed.end
        pieces.append(text[cursor:])
        return cleanup_punct("".join(pieces)), hits


@lru_cache(maxsize=1)
def get_engine() -> RuleEngine:
    """进程级单例。词库是只读的，可安全共享。"""
    return RuleEngine()


__all__ = ["Hit", "RuleEngine", "get_engine", "classify"]
