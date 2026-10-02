"""引擎单元测试（不依赖 Django、不联网）。

运行::

    cd server && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import time
import unittest

from deai.engine import build_report, rewrite_by_rules
from deai.engine.llm import is_configured
from deai.engine.prompts import SYSTEM_GENERAL, SYSTEM_XHS, build_user_message, get_system_prompt
from deai.engine.rewriter import check_fidelity, strip_wrapping
from deai.engine.rules import get_engine, parse_thresholds, required_matches
from deai.engine.textutil import cleanup_punct, rhythm_stats, split_sentences

AI_SAMPLE = (
    "随着人工智能技术的不断发展，越来越多的企业开始关注 AI 助手在实际业务场景中的应用。"
    "首先，AI 助手能够极大地提升工作效率，帮助员工从繁琐的重复性工作中解放出来。"
    "其次，它还可以显著降低企业的运营成本，实现降本增效的目标。"
    "再次，通过智能化的数据分析能力，AI 助手能够为企业决策提供有力支撑。"
    "值得注意的是，在实际应用中，AI 助手并非万能的，它也存在一定的局限性。"
    "综上所述，企业应当根据自身需求，合理引入 AI 助手，让它真正发挥应有的作用，为业务增长赋能。"
    "希望本文能够对您有所帮助。"
)

HUMAN_SAMPLE = (
    "我们公司去年给客服组配了 AI 助手，本意是让它们接掉重复问题。"
    "效果有，但没宣传的那么神。以前一天两百条工单，现在一百二，剩下的还是要人接管。"
    "省钱是真的，可也多了新活——得有人盯着它别乱答。"
    "所以别一上来就全铺开。挑一个流程最固定、最不要脑子的环节先试，跑顺了再加。"
)


class TestThresholdParsing(unittest.TestCase):
    """threshold 字段是中文描述串，且可能是「或」连接的复合条件。"""

    def test_single_condition(self):
        self.assertEqual(parse_thresholds(">=2 次/千字"), ((2, "kchar"),))
        self.assertEqual(parse_thresholds(">=1 次/全文"), ((1, "doc"),))

    def test_compound_condition_keeps_both_units(self):
        # 只取第一个数字会配错单位，这是最初的 bug
        self.assertEqual(
            parse_thresholds(">=2 次/段 或 >=3 次/千字"),
            ((2, "para"), (3, "kchar")),
        )

    def test_greater_than_means_plus_one(self):
        self.assertEqual(parse_thresholds("全篇 >1 处，或单段 >=2 处"), ((2, "doc"), (2, "para")))

    def test_kchar_scales_with_length_but_compound_takes_conservative(self):
        thr = parse_thresholds(">=2 次/段 或 >=3 次/千字")
        # 132 字：max(2, ceil(3*132/1000)=1) = 2 —— 单个破折号不该报警
        self.assertEqual(required_matches(thr, 132), 2)
        # 3000 字：max(2, ceil(9)) = 9
        self.assertEqual(required_matches(thr, 3000), 9)

    def test_int_and_garbage_input(self):
        self.assertEqual(parse_thresholds(3), ((3, "doc"),))
        self.assertEqual(parse_thresholds(None), ((1, "doc"),))
        self.assertEqual(parse_thresholds("随便写点什么"), ((1, "doc"),))


class TestRuleEngine(unittest.TestCase):
    def setUp(self):
        self.engine = get_engine()

    def test_lexicon_loaded(self):
        engine = self.engine
        self.assertGreaterEqual(len(engine._rules), 250)
        self.assertEqual(len(engine._patterns), 36)

    def test_longest_match_wins(self):
        """「此外还有」必须整体替换，不能被「此外」抢先。"""
        out, _ = rewrite_by_rules("此外还有一点要注意。")
        self.assertEqual(out, "还有一点要注意。")

    def test_template_rule_does_not_cross_sentence(self):
        """「在…方面」不能跨句匹配「存在一定门槛。一方面」。

        注意：skill 词表确实会命中「一方面」这个套话，那是**对的**。
        这里要验的是「跨句」——模板规则不该把「在」和「方面」隔着句子连起来。
        """
        hits = self.engine.scan("存在一定门槛。一方面，它需要时间。")
        crossed = [h for h in hits if "门槛" in h.text]
        self.assertEqual(crossed, [], f"模板规则跨句误匹配: {[h.text for h in crossed]}")

    def test_template_rule_applies_within_sentence(self):
        """同句内该匹配的要匹配到，并且捕获组正确回填。"""
        out, hits = rewrite_by_rules("我们对他进行了讨论。")
        texts = [h.text for h in hits]
        self.assertTrue(any("进行" in t for t in texts), texts)

    def test_ordinal_skeleton_gets_deleted(self):
        """序数词构成骨架时应当被删掉。"""
        out, hits = rewrite_by_rules("首先，要做对。其次，要做完。再次，要做好。")
        for word in ("首先", "其次", "再次"):
            self.assertNotIn(word, out)
        self.assertTrue(all(h.kind == "replace" for h in hits if h.text.startswith("首先")))

    def test_single_ordinal_is_kept(self):
        """孤立的一个「首先」不构成骨架，硬删会改坏教程类文本。"""
        out, _ = rewrite_by_rules("首先，我想说明一个前提。这个前提很重要，后面的判断都依赖它。")
        self.assertIn("首先", out)

    def test_dash_density_not_flagged_by_single_occurrence(self):
        """单个破折号不该被判为「破折号密度」问题。"""
        hits = self.engine.scan("省钱是真的，可也多了新活——得有人盯着它别乱答。")
        dash = [h for h in hits if h.category == "pattern_P07"]
        self.assertEqual(dash, [])

    def test_scan_is_empty_for_plain_text(self):
        hits = self.engine.scan("今天下雨，我没带伞。")
        self.assertEqual(hits, [])


class TestReportScoring(unittest.TestCase):
    def test_ai_sample_scores_high(self):
        _out, hits = rewrite_by_rules(AI_SAMPLE)
        report = build_report(AI_SAMPLE, hits)
        self.assertEqual(report["level"], "high")
        self.assertGreaterEqual(report["score"], 60)
        self.assertGreater(report["totalHits"], 0)

    def test_human_sample_scores_low(self):
        _out, hits = rewrite_by_rules(HUMAN_SAMPLE)
        report = build_report(HUMAN_SAMPLE, hits)
        self.assertEqual(report["level"], "low")
        self.assertLess(report["score"], 30)

    def test_ai_scores_above_human(self):
        ai_report = build_report(AI_SAMPLE, rewrite_by_rules(AI_SAMPLE)[1])
        human_report = build_report(HUMAN_SAMPLE, rewrite_by_rules(HUMAN_SAMPLE)[1])
        self.assertGreater(ai_report["score"], human_report["score"] + 20)

    def test_report_shape(self):
        _out, hits = rewrite_by_rules(AI_SAMPLE)
        report = build_report(AI_SAMPLE, hits)
        for key in ("score", "level", "verdict", "totalHits", "charCount", "categories", "advice", "stats", "hits"):
            self.assertIn(key, report)
        self.assertEqual(report["charCount"], len(AI_SAMPLE.strip()))
        self.assertTrue(0 <= report["score"] <= 100)

    def test_empty_text_does_not_crash(self):
        out, hits = rewrite_by_rules("")
        self.assertEqual(out, "")
        report = build_report("", hits)
        self.assertEqual(report["score"], 0)
        self.assertEqual(report["level"], "low")

    def test_report_and_rewrite_are_consistent(self):
        """报告里说「已删除」的词，改写结果里不该还留着。"""
        out, hits = rewrite_by_rules(AI_SAMPLE)
        deleted = [h.text for h in hits if h.kind == "replace" and not h.suggestion]
        still_there = [t for t in deleted if t in out]
        self.assertEqual(still_there, [], f"报告称已删除但仍存在: {still_there}")


class TestTextUtil(unittest.TestCase):
    def test_split_sentences(self):
        self.assertEqual(split_sentences("今天下雨。我没带伞！"), ["今天下雨。", "我没带伞！"])

    def test_cleanup_punct_removes_orphans(self):
        self.assertEqual(cleanup_punct("这句话，。"), "这句话。")
        self.assertEqual(cleanup_punct("，开头就带逗号。"), "开头就带逗号。")
        self.assertEqual(cleanup_punct("前面，，后面。"), "前面，后面。")

    def test_rhythm_stats(self):
        stats = rhythm_stats("今天下雨。我没带伞就淋湿了。好冷。")
        self.assertEqual(stats["sentenceCount"], 3)
        self.assertGreater(stats["avgSentenceLen"], 0)

    def test_rhythm_stats_empty(self):
        stats = rhythm_stats("")
        self.assertEqual(stats["sentenceCount"], 0)


class TestRewriterHelpers(unittest.TestCase):
    def test_strip_code_fence(self):
        self.assertEqual(strip_wrapping("```\n你好世界\n```"), "你好世界")
        self.assertEqual(strip_wrapping("```text\n你好世界\n```"), "你好世界")

    def test_strip_preface(self):
        self.assertEqual(strip_wrapping("以下是改写后的版本：\n你好世界"), "你好世界")

    def test_strip_quotes(self):
        self.assertEqual(strip_wrapping("“你好世界”"), "你好世界")

    def test_check_fidelity_detects_lost_numbers(self):
        warnings = check_fidelity("去年处理了 200 条工单，成本降了 30%。", "工单少了很多，成本也降了。")
        self.assertTrue(any("200" in w for w in warnings), warnings)

    def test_check_fidelity_ok_when_numbers_kept(self):
        warnings = check_fidelity("处理了 200 条工单。", "工单从 200 条降下来了。")
        self.assertFalse(any("200" in w for w in warnings), warnings)

    def test_check_fidelity_detects_shrink(self):
        warnings = check_fidelity("一" * 200, "一" * 50)
        self.assertTrue(any("删掉" in w or "字数" in w for w in warnings), warnings)


class TestPrompts(unittest.TestCase):
    def test_prompt_lookup(self):
        self.assertEqual(get_system_prompt("xhs"), SYSTEM_XHS)
        self.assertEqual(get_system_prompt("general"), SYSTEM_GENERAL)
        self.assertEqual(get_system_prompt("不存在的模式"), SYSTEM_GENERAL)

    def test_general_prompt_forbids_key_phrases(self):
        for phrase in ("首先", "综上所述", "赋能", "随着…的不断发展"):
            self.assertIn(phrase, SYSTEM_GENERAL)

    def test_user_message_wraps_text(self):
        msg = build_user_message("测试文本")
        self.assertIn("测试文本", msg)
        self.assertIn("<待改写文本>", msg)


class TestNoBrokenSentences(unittest.TestCase):
    """回归测试：删除型规则不能把句子成分也删掉。

    这几条来自真实语料——成语在句中作定语/谓语时，直接删除会产出病句，
    所以它们必须是 conditional（只建议、不自动改）。
    """

    def test_idiom_as_attribute_is_kept(self):
        # 曾经会变成「他一直坚持的态度。」
        out, _ = rewrite_by_rules("他一直坚持精益求精的态度。")
        self.assertIn("精益求精", out)

    def test_negation_phrase_as_attribute_is_kept(self):
        # 曾经会变成「这是确实的事实。」
        out, _ = rewrite_by_rules("这是不可否认的事实。")
        self.assertIn("不可否认", out)

    def test_hollow_phrase_as_predicate_is_kept(self):
        # 曾经会变成「这为行业发展。」
        out, _ = rewrite_by_rules("这为行业发展注入了新的活力。")
        self.assertIn("注入了新的活力", out)

    def test_outlook_phrase_as_predicate_is_kept(self):
        # 曾经会变成「这个行业。」
        out, _ = rewrite_by_rules("这个行业前景广阔。")
        self.assertIn("前景广阔", out)

    def test_safe_insertions_are_still_removed(self):
        """独立插入语仍然要删掉，别因为保守化把功能改没了。"""
        out, _ = rewrite_by_rules("综上所述，这件事很重要。值得注意的是，它也很紧急。")
        self.assertNotIn("综上所述", out)
        self.assertNotIn("值得注意的是", out)
        self.assertIn("这件事很重要", out)


class TestAddedContentDetection(unittest.TestCase):
    """本功能最大的信任风险：模型为了「具体化」而编出原文没有的内容。

    实测案例：原文只说「繁琐的重复性工作」，模型写成「填表、整理、来回搬运数据」。
    这类内容读起来很具体，但全是编的，必须让用户看得见。
    """

    def test_detects_new_enumeration(self):
        original = "AI 助手能提升效率。"
        rewritten = "AI 助手能把填表、整理、来回搬运数据这些活儿接过去。"
        warnings = check_fidelity(original, rewritten)
        self.assertTrue(any("找不到出处" in w for w in warnings), f"应检测出新增内容，实际: {warnings}")

    def test_no_false_positive_when_enumeration_came_from_original(self):
        # 原文用「和」连接、改写换成「、」，连接词变了但内容没变，不该误报
        original = "它支持批处理、快捷键和离线模式。"
        rewritten = "批处理、快捷键、离线模式它都支持。"
        warnings = check_fidelity(original, rewritten)
        self.assertFalse(any("找不到出处" in w for w in warnings), f"不该误报: {warnings}")

    def test_detects_new_quoted_phrase(self):
        original = "他说这个方案有问题。"
        rewritten = "他说「这根本跑不通」，方案有问题。"
        warnings = check_fidelity(original, rewritten)
        self.assertTrue(any("引述" in w for w in warnings), warnings)

    def test_self_reported_added_facts_warns(self):
        warnings = check_fidelity(
            "原文很短。", "原文被改写得更长了。", added_facts="补了「填表、整理数据」这类举例"
        )
        self.assertTrue(any("自报" in w for w in warnings), warnings)

    def test_self_reported_none_does_not_warn(self):
        for claim in ("无", "无。", "没有", "None", "n/a", "-", ""):
            warnings = check_fidelity("同一段文字。", "同一段文字。", added_facts=claim)
            self.assertFalse(
                any("自报" in w for w in warnings), f"claim={claim!r} 不该告警: {warnings}"
            )

    def test_helper_is_exported(self):
        from deai.engine.rewriter import check_added_content

        self.assertTrue(callable(check_added_content))


class TestReportAdvice(unittest.TestCase):
    """advice 不能因为前几个类别都是结构模式就变空。"""

    def test_pattern_categories_produce_advice(self):
        report = build_report(AI_SAMPLE, rewrite_by_rules(AI_SAMPLE)[1])
        self.assertTrue(report["categories"], "样例应当有命中类别")
        self.assertTrue(report["advice"], "advice 不该为空")

    def test_advice_is_non_empty_for_pure_pattern_hits(self):
        # 「首先/其次/最后」骨架 + 开头宏大背景，都是 pattern 类
        text = "随着行业的不断发展，我们要做三件事。首先，做对。其次，做完。最后，做好。"
        report = build_report(text, rewrite_by_rules(text)[1])
        self.assertTrue(report["advice"], report["categories"])


class TestRobustness(unittest.TestCase):
    def test_long_text_performance(self):
        text = AI_SAMPLE * 20  # 约 4800 字
        start = time.monotonic()
        rewrite_by_rules(text)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, f"规则层太慢: {elapsed:.2f}s")

    def test_punctuation_only(self):
        out, hits = rewrite_by_rules("。。。！！！")
        self.assertIsInstance(out, str)
        self.assertEqual(hits, [])

    def test_repeated_same_rule(self):
        out, _ = rewrite_by_rules("此外，此外，此外。")
        self.assertNotIn("此外", out)

    def test_llm_not_configured_by_default(self):
        # 测试环境不应有密钥；有的话说明环境被污染了
        import os

        if not os.environ.get("LLM_API_KEY"):
            self.assertFalse(is_configured())


if __name__ == "__main__":
    unittest.main(verbosity=2)
