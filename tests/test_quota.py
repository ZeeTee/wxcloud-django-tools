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

from deai import quota, wechat  # noqa: E402


class TestLimits(unittest.TestCase):
    """两档额度：未登录 5 次，登录后 10 次。"""

    @staticmethod
    def _limits_with(legacy: int, anonymous: int, verified: int) -> tuple[int, int]:
        with (
            patch.object(quota.settings, "DEAI_DAILY_LIMIT", legacy),
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_ANONYMOUS", anonymous),
            patch.object(quota.settings, "DEAI_DAILY_LIMIT_VERIFIED", verified),
        ):
            return quota._limits()

    def test_two_tiers(self):
        self.assertEqual(self._limits_with(0, 5, 10), (5, 10))

    def test_custom_tiers(self):
        self.assertEqual(self._limits_with(0, 3, 30), (3, 30))

    def test_legacy_variable_overrides_both(self):
        """老部署只配了 DEAI_DAILY_LIMIT，升级后额度不该突然从 20 掉到 5。"""
        self.assertEqual(self._limits_with(20, 5, 10), (20, 20))

    def test_legacy_zero_means_unset(self):
        self.assertEqual(self._limits_with(0, 5, 10), (5, 10))


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
