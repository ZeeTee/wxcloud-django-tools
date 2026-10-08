"""额度双档逻辑与微信登录封装（不需要数据库、不联网）。

数据库相关的部分（mark_verified 后额度真的提升）由冒烟脚本覆盖，
这里只测纯逻辑与错误分支。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from deai import quota, wechat  # noqa: E402


class TestLimits(unittest.TestCase):
    """两档额度：未授权 5 次，已授权 10 次。

    这里**只**认 DEAI_DAILY_LIMIT_ANONYMOUS / _VERIFIED 两个变量。
    早期那个会把两档一起覆盖的 DEAI_DAILY_LIMIT 已彻底移除，
    所以没有任何「旧变量优先」的用例了——那是刻意的。
    """

    @staticmethod
    def _limits_with(anonymous: int, verified: int) -> tuple[int, int]:
        with (
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_ANONYMOUS", anonymous),
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_VERIFIED", verified),
        ):
            return quota.limits()

    def test_default_two_tiers(self):
        self.assertEqual(self._limits_with(5, 10), (5, 10))

    def test_custom_tiers(self):
        self.assertEqual(self._limits_with(3, 30), (3, 30))

    def test_verified_can_be_lower_than_anonymous(self):
        """不假设「已授权一定更高」——它只是另一个配置项。"""
        self.assertEqual(self._limits_with(20, 10), (20, 10))

    def test_limits_reads_live_settings(self):
        """limits() 每次都读当前设置，不缓存——改了立刻生效，测试也好写。"""
        with (
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_ANONYMOUS", 1),
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_VERIFIED", 2),
        ):
            self.assertEqual(quota.limits(), (1, 2))
        with (
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_ANONYMOUS", 7),
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_VERIFIED", 8),
        ):
            self.assertEqual(quota.limits(), (7, 8))


class TestNoLegacyVariable(unittest.TestCase):
    """锁住「旧变量已彻底移除」这件事，防止以后又被加回来。

    它被移除是因为覆盖行为是静默的：环境变量里配了 ..._ANONYMOUS=5，
    但控制台还留着旧变量 20，接口就返回 20，光看接口只能靠猜。
    """

    def test_setting_does_not_exist(self):
        """settings 里已经没有这个属性。

        ``env_int`` 是在 import 时读环境的，所以「环境变量里配了也没用」
        等价于「settings 里根本没这个键」——配了也没人读它。
        """
        with self.assertRaises(AttributeError):
            settings.DEAI_DAILY_LIMIT  # noqa: B018

    def test_no_legacy_helpers_left(self):
        for name in ("legacy_override", "warn_legacy_override"):
            self.assertFalse(hasattr(quota, name), f"quota.{name} 应该已经删掉")


class TestWeChatLogin(unittest.TestCase):
    def test_not_configured(self):
        with (
            patch.object(wechat.settings, "WX_APPID", ""),
            patch.object(wechat.settings, "WX_SECRET", ""),
        ):
            self.assertFalse(wechat.is_configured())
            with self.assertRaises(wechat.WeChatError) as ctx:
                wechat.code2session("some-code")
            self.assertIn("AppID", str(ctx.exception))

    def test_configured_requires_both(self):
        with (
            patch.object(wechat.settings, "WX_APPID", "wx123"),
            patch.object(wechat.settings, "WX_SECRET", ""),
        ):
            self.assertFalse(wechat.is_configured(), "只配 appid 不算配好")
        with (
            patch.object(wechat.settings, "WX_APPID", "wx123"),
            patch.object(wechat.settings, "WX_SECRET", "secret"),
        ):
            self.assertTrue(wechat.is_configured())

    def test_empty_code_rejected(self):
        with (
            patch.object(wechat.settings, "WX_APPID", "wx123"),
            patch.object(wechat.settings, "WX_SECRET", "secret"),
        ):
            with self.assertRaises(wechat.WeChatError) as ctx:
                wechat.code2session("   ")
            self.assertIn("code", str(ctx.exception))

    def test_error_hints_are_human_readable(self):
        """微信错误码要翻译成人话——这些 message 会直接展示给用户。"""
        for code in (-1, 40029, 45011):
            self.assertIn(code, wechat._ERROR_HINTS)
            self.assertNotIn("errcode", wechat._ERROR_HINTS[code])


class TestQuotaMessages(unittest.TestCase):
    """未登录额度用尽时，错误文案应该顺带引导登录。"""

    def test_quota_exceeded_message_mentions_login(self):
        from deai.quota import QuotaExceeded

        exc = QuotaExceeded("今天的 5 次免登录额度用完了。登录后每天可用 10 次，明天也会自动恢复")
        text = str(exc)
        self.assertIn("登录", text)
        self.assertIn("10", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
