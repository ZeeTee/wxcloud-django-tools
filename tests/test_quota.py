"""额度双档逻辑与微信登录封装（不需要数据库、不联网）。

数据库相关的部分（mark_verified 后额度真的提升）由冒烟脚本覆盖，
这里只测纯逻辑与错误分支。
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
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


@contextmanager
def _tiers(legacy: int, anonymous: int, verified: int):
    """临时改三档额度配置。"""
    with (
        patch.object(quota.settings, "DEAI_DAILY_LIMIT", legacy),
        patch.object(quota.settings, "DEAI_DAILY_LIMIT_ANONYMOUS", anonymous),
        patch.object(quota.settings, "DEAI_DAILY_LIMIT_VERIFIED", verified),
    ):
        yield


class TestLegacyOverrideNotice(unittest.TestCase):
    """旧变量 DEAI_DAILY_LIMIT 会静默覆盖两档，必须能被看见。

    真实踩到的场景：环境变量里配了 DEAI_DAILY_LIMIT_ANONYMOUS=5，
    但控制台还留着早期模板里的 DEAI_DAILY_LIMIT=20，于是 /api/quota
    返回的限额是 20。光看接口只会觉得「配置没生效」，很难想到是旧变量。
    """

    def tearDown(self):
        # 别把「已经喊过」的状态漏给别的用例
        quota._legacy_warned = False

    def test_no_override_returns_none(self):
        with _tiers(0, 5, 10):
            self.assertIsNone(quota.legacy_override())

    def test_override_reports_ignored_values(self):
        """除了旧值，还要把「被忽略掉的那两个配置」一并报出来——
        不然看到 legacy=20 也不知道自己配的 5 去哪了。"""
        with _tiers(20, 5, 10):
            info = quota.legacy_override()
        self.assertEqual(info, {"legacy": 20, "anonymous": 5, "verified": 10})

    def test_override_matches_effective_limits(self):
        """legacy_override 说覆盖时，_limits() 必须真的返回被覆盖的值。"""
        with _tiers(20, 5, 10):
            self.assertEqual(quota.limits(), (20, 20))
            self.assertIsNotNone(quota.legacy_override())

    def test_warns_loudly_when_overriding(self):
        with _tiers(20, 5, 10):
            with self.assertLogs("deai.quota", level="WARNING") as captured:
                quota.warn_legacy_override()
            text = "\n".join(captured.output)
        self.assertIn("DEAI_DAILY_LIMIT=20", text)
        # 要明确指出「你配的那两个没生效」，并给出怎么修
        self.assertIn("不生效", text)
        self.assertIn("DEAI_DAILY_LIMIT_ANONYMOUS=5", text)
        self.assertIn("DEAI_DAILY_LIMIT_VERIFIED=10", text)
        self.assertIn("删掉", text)

    def test_warns_only_once(self):
        with _tiers(20, 5, 10):
            with self.assertLogs("deai.quota", level="WARNING"):
                quota.warn_legacy_override()
            with self.assertNoLogs("deai.quota", level="WARNING"):
                quota.warn_legacy_override()

    def test_silent_when_not_overriding(self):
        with _tiers(0, 5, 10):
            with self.assertNoLogs("deai.quota", level="WARNING"):
                quota.warn_legacy_override()


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
