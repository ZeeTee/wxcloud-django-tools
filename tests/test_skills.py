"""Skill 子系统与输出解析的单元测试（不需要 Django、不联网）。

    cd server 项目根 && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import unittest

from deai.skills import get_registry, parse, reset_registry
from deai.skills.registry import SkillError


class TestSkillCompile(unittest.TestCase):
    """编译的核心目标：让没有文件工具的模型也能拿到全部所需信息。"""

    def setUp(self):
        reset_registry()
        self.reg = get_registry()

    def test_humanizer_loaded(self):
        self.assertTrue(self.reg.has("humanizer"))
        info = next(s for s in self.reg.list() if s.slug == "humanizer")
        self.assertEqual(info.version, "4.1.0")
        self.assertIn("general", info.scenes)
        self.assertIn("xhs", info.scenes)

    def test_load_hints_are_compiled_away(self):
        """原始 skill 到处写着「加载 references/xxx.md」，产品侧必须全部编译掉。

        这是 P0 最关键的断言：漏掉一处，模型就会看到一条它无法执行的指令。
        """
        for scene in ("general", "xhs"):
            prompt = self.reg.compile("humanizer", scene).system_prompt
            self.assertNotIn("加载 references/", prompt, f"scene={scene} 仍有未编译的加载指令")

    def test_references_injected_as_appendix(self):
        compiled = self.reg.compile("humanizer", "general")
        self.assertIn("banned-words", compiled.injected)
        self.assertIn("structures", compiled.injected)
        self.assertIn("【附录 A】", compiled.system_prompt)
        self.assertIn("【附录 B】", compiled.system_prompt)

    def test_fewshot_reference_not_injected_by_default(self):
        """examples 标了 fewshot，P0 不注入（省 token），但不应出现死引用。"""
        compiled = self.reg.compile("humanizer", "general")
        self.assertNotIn("examples", compiled.injected)
        self.assertNotIn("【附录 C】", compiled.system_prompt)

    def test_output_protocol_appended(self):
        prompt = self.reg.compile("humanizer", "xhs").system_prompt
        self.assertIn("<REWRITTEN>", prompt)
        self.assertIn("<REPORT>", prompt)
        # 事实约束必须写进协议，这是最大的产品风险点
        self.assertIn("不新增", prompt)

    def test_frontmatter_stripped(self):
        prompt = self.reg.compile("humanizer", "general").system_prompt
        self.assertNotIn("allowed-tools", prompt)
        self.assertNotIn("trigger:", prompt)

    def test_scene_override_differs(self):
        general = self.reg.compile("humanizer", "general").system_prompt
        xhs = self.reg.compile("humanizer", "xhs").system_prompt
        self.assertNotEqual(general, xhs)
        self.assertIn("小红书", xhs)
        # 通用场景要放宽标点禁令，否则技术文档会被改得没法读
        self.assertIn("放宽", general)

    def test_unknown_scene_falls_back_to_default(self):
        compiled = self.reg.compile("humanizer", "并不存在的场景")
        self.assertEqual(compiled.scene, "general")

    def test_unknown_skill_raises(self):
        with self.assertRaises(SkillError):
            self.reg.compile("no-such-skill")

    def test_compile_is_cached(self):
        first = self.reg.compile("humanizer", "general")
        second = self.reg.compile("humanizer", "general")
        self.assertIs(first, second)

    def test_prompt_size_is_reasonable(self):
        """把大小钉住：意外注入整份 examples 会让 prompt 翻倍、成本失控。"""
        compiled = self.reg.compile("humanizer", "general")
        self.assertLess(compiled.prompt_chars, 30000, "system prompt 过大，检查 inject 规则")
        self.assertGreater(compiled.prompt_chars, 5000, "system prompt 过小，可能没注入成功")


class TestOutputParser(unittest.TestCase):
    """模型不一定会守协议，解析必须多级容错且永不丢正文。"""

    def test_protocol_ok(self):
        raw = "<REPORT>\nAI味等级：中度\n主要问题：套话\n</REPORT>\n<REWRITTEN>\n这是改写后的正文。\n</REWRITTEN>"
        parsed = parse(raw)
        self.assertTrue(parsed.parsed)
        self.assertEqual(parsed.text, "这是改写后的正文。")
        self.assertIn("中度", parsed.report)

    def test_missing_closing_tag(self):
        parsed = parse("<REWRITTEN>\n只有开标签的正文。")
        self.assertEqual(parsed.text, "只有开标签的正文。")

    def test_case_insensitive_and_spaces(self):
        parsed = parse("< rewritten >\n小写标签\n</ rewritten >")
        self.assertEqual(parsed.text, "小写标签")

    def test_markdown_section_fallback(self):
        raw = "## AI味检测报告\n等级：轻\n\n## 润色后全文\n这是正文。\n\n## 质检报告\nL1 通过"
        parsed = parse(raw)
        self.assertFalse(parsed.parsed)
        self.assertEqual(parsed.fallback, "markdown_section")
        # 必须砍掉「## 质检报告」之后的内容
        self.assertEqual(parsed.text, "这是正文。")

    def test_code_fence_fallback(self):
        parsed = parse("```\n被代码块包住的正文\n```")
        self.assertEqual(parsed.text, "被代码块包住的正文")
        self.assertEqual(parsed.fallback, "code_fence")

    def test_raw_fallback_never_loses_text(self):
        raw = "模型完全没守协议，直接给了正文。"
        parsed = parse(raw)
        self.assertEqual(parsed.text, raw)
        self.assertFalse(parsed.parsed)
        self.assertEqual(parsed.fallback, "raw")

    def test_report_only(self):
        raw = "<REPORT>\n等级：重度\n</REPORT>\n剩下的正文在这里。"
        parsed = parse(raw)
        self.assertIn("重度", parsed.report)
        self.assertEqual(parsed.text, "剩下的正文在这里。")

    def test_empty(self):
        parsed = parse("")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.fallback, "empty")

    def test_none_like_input(self):
        parsed = parse("   \n  ")
        self.assertEqual(parsed.text, "")
        self.assertFalse(parsed.parsed)

    def test_extracts_added_facts_when_present(self):
        raw = (
            "<REPORT>\nAI味等级：中度\n总修改数：5 处\n"
            "新增事实：- 补了「填表、整理数据」这类举例\n</REPORT>\n"
            "<REWRITTEN>\n正文。\n</REWRITTEN>"
        )
        parsed = parse(raw)
        self.assertIn("填表", parsed.added_facts)
        self.assertFalse(parsed.claims_no_added_facts)

    def test_claims_no_added_facts(self):
        for claim in ("无", "无。", "没有", "-"):
            raw = f"<REPORT>\n新增事实：{claim}\n</REPORT>\n<REWRITTEN>\n正文。\n</REWRITTEN>"
            parsed = parse(raw)
            self.assertTrue(parsed.claims_no_added_facts, f"claim={claim!r}")

    def test_missing_added_facts_line(self):
        raw = "<REPORT>\nAI味等级：轻度\n</REPORT>\n<REWRITTEN>\n正文。\n</REWRITTEN>"
        parsed = parse(raw)
        self.assertEqual(parsed.added_facts, "")
        self.assertFalse(parsed.claims_no_added_facts)

    def test_protocol_asks_for_added_facts_line(self):
        """协议里必须要求模型自报新增内容，否则解析器永远拿不到。"""
        prompt = get_registry().compile("humanizer", "general").system_prompt
        self.assertIn("新增事实", prompt)


class TestResolveSystemPrompt(unittest.TestCase):
    """skill 编译失败要回退，而不是让请求 500。"""

    def setUp(self):
        reset_registry()

    def test_legacy_returns_hardcoded_prompt(self):
        from deai.engine.rewriter import LEGACY_SKILL, resolve_system_prompt

        prompt, used, version, degraded = resolve_system_prompt(LEGACY_SKILL, "general")
        self.assertEqual(used, LEGACY_SKILL)
        self.assertFalse(degraded)
        self.assertIn("你是中文母语写作者", prompt)
        self.assertEqual(version, "")

    def test_humanizer_returns_compiled_prompt(self):
        from deai.engine.rewriter import resolve_system_prompt

        prompt, used, version, degraded = resolve_system_prompt("humanizer", "xhs")
        self.assertEqual(used, "humanizer")
        self.assertEqual(version, "4.1.0")
        self.assertFalse(degraded)
        self.assertIn("【附录 A】", prompt)

    def test_unknown_skill_degrades_to_legacy(self):
        from deai.engine.rewriter import LEGACY_SKILL, resolve_system_prompt

        prompt, used, _version, degraded = resolve_system_prompt("does-not-exist", "general")
        self.assertEqual(used, LEGACY_SKILL)
        self.assertTrue(degraded, "未知 skill 必须标记为已降级，否则问题会被掩盖")
        self.assertIn("你是中文母语写作者", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
