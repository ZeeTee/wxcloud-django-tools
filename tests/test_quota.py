"""使用次数逻辑与微信封装（不需要数据库、不联网）。

次数相关的 DB 部分（真的扣减、真的按天分桶）由冒烟脚本覆盖，
这里只测纯逻辑：上限取值、重置时刻、配置项有没有被换错。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from deai import quota, wechat  # noqa: E402


class TestDailyLimit(unittest.TestCase):
    """每人每天固定一个上限，按 openid 计，跟手机号授权无关。"""

    def test_default_is_ten(self):
        self.assertEqual(quota.daily_limit(), 10)

    def test_reads_live_settings(self):
        """每次都读当前设置、不缓存——改了立刻生效。"""
        with patch.object(quota.settings, "DEAI_DAILY_QUOTA", 3):
            self.assertEqual(quota.daily_limit(), 3)
        with patch.object(quota.settings, "DEAI_DAILY_QUOTA", 99):
            self.assertEqual(quota.daily_limit(), 99)

    def test_signature_takes_no_openid(self):
        """上限是「每个人的」，不该因为用户不同而不同。

        以前是 limits() 返回两档、由调用方按 verified 选一档；
        现在只有一个值，所以函数不该再接受用户参数。
        """
        import inspect

        params = list(inspect.signature(quota.daily_limit).parameters)
        self.assertEqual(params, [], f"daily_limit 不该有参数，实际有 {params}")


class TestResetsAt(unittest.TestCase):
    """北京时间 0 点归零——靠「按天分桶」实现，不依赖定时任务。"""

    def test_resets_at_next_midnight(self):
        fake_now = datetime(2026, 10, 8, 23, 30, 0)
        with patch.object(quota.timezone, "now", return_value=fake_now):
            self.assertEqual(
                quota.resets_at(), datetime(2026, 10, 9, 0, 0, 0)
            )

    def test_resets_at_is_always_after_now(self):
        for hour in (0, 1, 12, 23):
            fake_now = datetime(2026, 10, 8, hour, 15, 0)
            with patch.object(quota.timezone, "now", return_value=fake_now):
                self.assertGreater(quota.resets_at(), fake_now)

    def test_resets_within_24h(self):
        fake_now = datetime(2026, 10, 8, 13, 37, 0)
        with patch.object(quota.timezone, "now", return_value=fake_now):
            delta = quota.resets_at() - fake_now
        self.assertLessEqual(delta, timedelta(days=1))
        self.assertGreater(delta, timedelta(0))

    def test_format_is_readable(self):
        fake_now = datetime(2026, 10, 8, 13, 37, 0)
        with patch.object(quota.timezone, "now", return_value=fake_now):
            info = quota.resets_at().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(info, "2026-10-09 00:00:00")


class TestConfigNames(unittest.TestCase):
    """锁住变量名，防止回退到已经废弃的那几个。

    历史：DEAI_DAILY_LIMIT（一配就同时覆盖两档，静默），
    以及两档时代的 DEAI_DAILY_LIMIT_ANONYMOUS / _VERIFIED。
    现在只剩 DEAI_DAILY_QUOTA 一个。
    """

    def test_current_setting_exists(self):
        self.assertTrue(hasattr(settings, "DEAI_DAILY_QUOTA"))

    def test_retired_settings_are_gone(self):
        for name in (
            "DEAI_DAILY_LIMIT",
            "DEAI_DAILY_LIMIT_ANONYMOUS",
            "DEAI_DAILY_LIMIT_VERIFIED",
        ):
            self.assertFalse(hasattr(settings, name), f"{name} 应该已经删掉")

    def test_no_legacy_helpers_left(self):
        for name in ("limits", "legacy_override", "warn_legacy_override"):
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
