"""provider 切换与用量解析的单元测试（不需要 Django、不联网）。

覆盖两个 provider 的实质差异：密钥来源、默认端点与模型名、
两种 usage 格式的解析、以及「优先用供应商上报成本」的策略。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from deai.engine import llm


class TestProviderSwitch(unittest.TestCase):
    def test_default_is_deepseek(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm.active_provider(), "deepseek")

    def test_switch_to_openrouter(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=True):
            cfg = llm.load_config()
            self.assertEqual(cfg.provider, "openrouter")
            self.assertEqual(cfg.base_url, "https://openrouter.ai/api/v1")
            # OpenRouter 的模型名是 `厂商/模型` 格式
            self.assertEqual(cfg.model, "deepseek/deepseek-chat")

    def test_unknown_provider_falls_back(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "并不存在的供应商"}, clear=True):
            self.assertEqual(llm.active_provider(), "deepseek")

    def test_provider_specific_key_wins(self):
        env = {
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "or-key",
            "LLM_API_KEY": "generic-key",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm.load_config().api_key, "or-key")

    def test_generic_key_is_fallback(self):
        """老部署只配了 LLM_API_KEY，换 provider 时不该突然失效。"""
        env = {"LLM_PROVIDER": "openrouter", "LLM_API_KEY": "generic-key"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm.load_config().api_key, "generic-key")

    def test_two_keys_coexist(self):
        """两个 key 同时配好，只靠 LLM_PROVIDER 切换——这是本功能的核心诉求。"""
        env = {"DEEPSEEK_API_KEY": "ds-key", "OPENROUTER_API_KEY": "or-key"}
        for provider, expected in (("deepseek", "ds-key"), ("openrouter", "or-key")):
            with patch.dict(os.environ, {**env, "LLM_PROVIDER": provider}, clear=True):
                self.assertEqual(llm.load_config().api_key, expected, provider)

    def test_env_can_override_base_and_model(self):
        env = {
            "LLM_PROVIDER": "openrouter",
            "LLM_BASE_URL": "https://custom.example/v1",
            "LLM_MODEL": "custom/model",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = llm.load_config()
            self.assertEqual(cfg.base_url, "https://custom.example/v1")
            self.assertEqual(cfg.model, "custom/model")

    def test_provider_info_is_serializable(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=True):
            info = llm.provider_info()
        self.assertEqual(info["provider"], "openrouter")
        self.assertIn("deepseek", info["available"])
        self.assertIn("openrouter", info["available"])
        self.assertFalse(info["keyConfigured"])


class TestUsageParsing(unittest.TestCase):
    """两个 provider 的 usage 字段名不同，必须都认。"""

    def test_deepseek_style(self):
        raw = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        }
        usage = llm.Usage.from_api(raw)
        self.assertEqual(usage.cache_hit_tokens, 80)
        self.assertEqual(usage.cache_miss_tokens, 20)
        self.assertEqual(usage.cost_usd, 0.0, "DeepSeek 不返回 cost")
        self.assertAlmostEqual(usage.cache_hit_rate, 0.8)

    def test_openrouter_style(self):
        raw = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost": 0.000123,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
        usage = llm.Usage.from_api(raw)
        self.assertEqual(usage.cache_hit_tokens, 60, "缓存命中在 prompt_tokens_details 里")
        self.assertEqual(usage.cache_miss_tokens, 40, "未命中应由总数推出来")
        self.assertAlmostEqual(usage.cost_usd, 0.000123)

    def test_empty_payload(self):
        usage = llm.Usage.from_api(None)
        self.assertEqual(usage.total_tokens, 0)
        self.assertEqual(usage.cache_hit_rate, 0.0)
        self.assertEqual(usage.cost_usd, 0.0)


class TestCostEstimation(unittest.TestCase):
    def test_prefers_provider_reported_cost(self):
        """OpenRouter 直接给美元成本，比本地价格表准，应该优先用。"""
        usage = llm.Usage(prompt_tokens=1000, completion_tokens=100, cost_usd=0.01)
        with patch.dict(os.environ, {"USD_CNY_RATE": "7"}, clear=True):
            cost = llm.estimate_cost(usage, "openrouter")
        self.assertEqual(cost["source"], "provider")
        self.assertAlmostEqual(cost["costCNY"], 0.07, places=6)
        self.assertAlmostEqual(cost["rawCostUSD"], 0.01, places=8)

    def test_falls_back_to_local_table(self):
        usage = llm.Usage(prompt_tokens=1_000_000, completion_tokens=0, cache_hit_tokens=0)
        with patch.dict(
            os.environ, {"LLM_PRICE_IN": "2.0", "LLM_PRICE_OUT": "8.0"}, clear=True
        ):
            cost = llm.estimate_cost(usage, "deepseek")
        self.assertEqual(cost["source"], "local_table")
        self.assertAlmostEqual(cost["costCNY"], 2.0, places=4)

    def test_cache_hit_is_much_cheaper(self):
        miss = llm.Usage(prompt_tokens=1_000_000, cache_hit_tokens=0)
        hit = llm.Usage(prompt_tokens=1_000_000, cache_hit_tokens=1_000_000)
        with patch.dict(os.environ, {}, clear=True):
            miss_cost = llm.estimate_cost(miss, "deepseek")["costCNY"]
            hit_cost = llm.estimate_cost(hit, "deepseek")["costCNY"]
        self.assertLess(hit_cost, miss_cost)
        self.assertLess(hit_cost * 10, miss_cost, "缓存命中价应远低于未命中（差 50 倍）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
