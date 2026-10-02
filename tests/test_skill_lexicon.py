"""skill 禁用词表 → 规则引擎的解析与集成测试。

不依赖 Django、不联网。核心是把「skill 的词表」和「规则引擎的体检报告」
对齐：模型看得见前者，而免费体检只认后者，两边不能各说各话。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from deai.engine import build_report, rewrite_by_rules
from deai.engine.rules import get_engine
from deai.engine.skill_lexicon import parse_banned_words

_BANNED = (
    Path(__file__).resolve().parent.parent
    / "deai"
    / "skills"
    / "humanizer"
    / "references"
    / "banned-words.md"
)


class TestParseBannedWords(unittest.TestCase):
    """解析器的难点全在噪声过滤：markdown 里混着表头、参考资料和占位模板。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = parse_banned_words(_BANNED.read_text(encoding="utf-8"))
        cls.patterns = {r["pattern"] for r in cls.rules}

    def test_parses_reasonable_count(self):
        self.assertGreater(len(self.rules), 100, "解析出的规则太少，可能正则没匹配上")

    def test_no_ascii_noise(self):
        """参考资料里的项目名（Humanizer-zh、stop-slop）不该被当成禁用词。"""
        for rule in self.rules:
            self.assertFalse(rule["pattern"].isascii(), f"混入英文噪声: {rule['pattern']}")

    def test_no_table_headers(self):
        for header in ("毒级", "句式", "修法", "书面腔", "错误例"):
            self.assertNotIn(header, self.patterns, f"表头被当成词: {header}")

    def test_no_placeholder_templates(self):
        for rule in self.rules:
            self.assertNotIn("xxx", rule["pattern"], f"占位模板混入: {rule['pattern']}")
            self.assertNotIn("...", rule["pattern"], f"省略号模板混入: {rule['pattern']}")

    def test_severity_is_valid(self):
        for rule in self.rules:
            self.assertIn(rule["severity"], ("high", "medium", "low"))

    def test_known_words_are_present(self):
        for word in ("仿佛", "眼中闪过", "嘴角勾起", "值得注意的是", "综上所述"):
            self.assertIn(word, self.patterns, f"漏掉了 {word}")

    def test_deduplicated(self):
        self.assertEqual(len(self.patterns), len(self.rules), "有重复条目")


class TestEngineUsesSkillLexicon(unittest.TestCase):
    def test_engine_loaded_skill_rules(self):
        engine = get_engine()
        skill_rules = [r for r in engine._rules if r.origin.startswith("skill:")]
        self.assertTrue(skill_rules, "规则引擎没有加载 skill 词表")

    def test_skill_words_are_flag_only(self):
        """这些词只标记、不自动替换。

        「坚定」「仿佛」「深邃」机械替换会直接毁掉句子，所以它们只能进体检报告，
        由用户自己决定。这是本模块最重要的约束。
        """
        engine = get_engine()
        for rule in engine._rules:
            if rule.origin.startswith("skill:"):
                self.assertEqual(
                    rule.mode, "flag", f"{rule.origin} 的规则竟然会自动改写文本"
                )

    def test_detects_skill_words_end_to_end(self):
        text = "他眼中闪过一丝悲伤，嘴角勾起一抹苦笑。仿佛能穿透一切一般。"
        _out, hits = rewrite_by_rules(text)
        found = {h.text for h in hits}
        for word in ("眼中闪过", "嘴角勾起", "仿佛"):
            self.assertIn(word, found, f"没能检出 {word}")

    def test_report_score_reflects_skill_words(self):
        plain = "今天天气不错，我出门买了点东西就回来了。"
        loaded = "他眼中闪过一丝悲伤，嘴角勾起一抹苦笑，仿佛能穿透一切一般。"
        plain_score = build_report(plain, rewrite_by_rules(plain)[1])["score"]
        loaded_score = build_report(loaded, rewrite_by_rules(loaded)[1])["score"]
        self.assertGreater(loaded_score, plain_score, "命中 skill 词表后分数反而没上升")

    def test_engine_still_works_without_skill_dir(self):
        """engine 必须能脱离 skills 目录独立运行（读不到就跳过）。"""
        from deai.engine.rules import RuleEngine, _load_raw

        engine = RuleEngine(_load_raw())  # 显式传数据，不依赖磁盘上的 skill
        self.assertGreater(len(engine._rules), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
