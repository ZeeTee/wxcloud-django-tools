"""手机号授权（``getPhoneNumber``）封装测试：不联网、不需要数据库。

覆盖两条调用链的**报文形态**与**安全校验**：
  - 云调用模式：必须是 HTTP、必须不带 ``access_token``、路径必须对得上白名单
  - 自管 token 模式：必须带 ``access_token``，且 token 失效时刷新重试一次
  - ``watermark.appid`` 校验、错误码翻译、手机号打码/指纹

真正打数据库的部分（token 落库、授权后额度真的从 5 变 10）由
``scripts/smoke_api.py`` 覆盖。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")

import django  # noqa: E402

django.setup()

from deai import wechat  # noqa: E402


class _FakeResponse:
    """冒充 ``urlopen`` 的返回值。"""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _phone_payload(phone: str = "13800138000", appid: str = "wx_test_appid") -> dict:
    return {
        "errcode": 0,
        "errmsg": "ok",
        "phone_info": {
            "phoneNumber": phone,
            "purePhoneNumber": phone,
            "countryCode": "86",
            "watermark": {"timestamp": 1700000000, "appid": appid},
        },
    }


class TestMaskAndFingerprint(unittest.TestCase):
    def test_mask_keeps_prefix_and_suffix(self):
        self.assertEqual(wechat.mask_phone("13800138000"), "138****8000")

    def test_mask_strips_non_digits(self):
        self.assertEqual(wechat.mask_phone("+86 138-0013-8000"), "138****8000")

    def test_mask_of_short_input(self):
        self.assertEqual(wechat.mask_phone("123"), "****")
        self.assertEqual(wechat.mask_phone(""), "")

    def test_fingerprint_is_stable_and_not_reversible(self):
        a = wechat.phone_fingerprint("13800138000")
        b = wechat.phone_fingerprint("+86 13800138000")
        self.assertEqual(a, b, "同一号码的不同写法要算出同一个指纹")
        self.assertEqual(len(a), 32)
        self.assertNotIn("13800138000", a, "指纹里不能出现原号码")

    def test_fingerprint_empty(self):
        self.assertEqual(wechat.phone_fingerprint(""), "")


class TestRequestShape(unittest.TestCase):
    """``_request`` 的报文形态：URL、方法、body。"""

    def _capture(self, payload: dict):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["data"] = request.data
            captured["timeout"] = timeout
            return _FakeResponse(payload)

        return captured, fake_urlopen

    def test_get_with_params(self):
        captured, fake = self._capture({"ok": 1})
        with patch.object(wechat.urllib.request, "urlopen", fake):
            out = wechat._request(
                "/cgi-bin/token", params={"a": "1", "b": "中"}, base="https://x.com"
            )
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(captured["method"], "GET")
        self.assertIn("https://x.com/cgi-bin/token?", captured["url"])
        self.assertIn("b=%E4%B8%AD", captured["url"], "中文参数要 URL 编码")
        self.assertIsNone(captured["data"])

    def test_post_with_body(self):
        captured, fake = self._capture({"errcode": 0})
        with patch.object(wechat.urllib.request, "urlopen", fake):
            wechat._request("/p", body={"code": "abc"}, base="http://x.com")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"].decode("utf-8")), {"code": "abc"})

    def test_errcode_inside_http_200_is_raised(self):
        """微信出错时 HTTP 仍是 200，错误在 body 里——必须靠 errcode 判断。"""
        _, fake = self._capture({"errcode": 40029, "errmsg": "invalid code"})
        with patch.object(wechat.urllib.request, "urlopen", fake):
            with self.assertRaises(wechat.WeChatError) as ctx:
                wechat.get_phone_number("bad")
        self.assertIn("重新点击授权", str(ctx.exception))


class TestCloudCallMode(unittest.TestCase):
    """云调用：HTTP + 不带 access_token。这是默认模式。"""

    def test_phone_ready_without_secret(self):
        """云调用模式下连 AppSecret 都不需要，只要开关开着就能用。"""
        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", True),
            patch.object(wechat.settings, "WX_APPID", ""),
            patch.object(wechat.settings, "WX_SECRET", ""),
        ):
            self.assertTrue(wechat.phone_ready())

    def test_not_ready_when_openapi_off_and_unconfigured(self):
        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", False),
            patch.object(wechat.settings, "WX_APPID", ""),
            patch.object(wechat.settings, "WX_SECRET", ""),
        ):
            self.assertFalse(wechat.phone_ready())
            with self.assertRaises(wechat.WeChatError):
                wechat.get_phone_number("abc")

    def test_request_has_no_token_and_uses_http(self):
        calls = []

        def fake_request(path, **kwargs):
            calls.append((path, kwargs))
            return _phone_payload()

        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", True),
            patch.object(wechat.settings, "WX_OPENAPI_BASE", "http://api.weixin.qq.com"),
            patch.object(wechat.settings, "WX_APPID", "wx_test_appid"),
            patch.object(wechat, "_request", fake_request),
            patch.object(wechat, "get_access_token") as token_mock,
        ):
            info = wechat.get_phone_number("phone-code")

        self.assertEqual(len(calls), 1)
        path, kwargs = calls[0]
        self.assertEqual(path, "/wxa/business/getuserphonenumber")
        self.assertEqual(kwargs["body"], {"code": "phone-code"})
        self.assertEqual(kwargs["base"], "http://api.weixin.qq.com")
        self.assertNotIn("params", kwargs, "云调用不能带 access_token")
        token_mock.assert_not_called()
        self.assertEqual(info["masked"], "138****8000")

    def test_default_base_is_http_and_path_matches_whitelist(self):
        """白名单里只填路径，且云调用必须走 HTTP（否则要手动信任容器证书）。"""
        self.assertTrue(wechat.settings.WX_OPENAPI_BASE.startswith("http://"))
        self.assertEqual(wechat._PHONE_PATH, "/wxa/business/getuserphonenumber")


class TestSelfManagedTokenMode(unittest.TestCase):
    """兜底模式：自管 access_token。"""

    def test_request_carries_token(self):
        calls = []

        def fake_request(path, **kwargs):
            calls.append((path, kwargs))
            return _phone_payload()

        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", False),
            patch.object(wechat.settings, "WX_APPID", "wx_test_appid"),
            patch.object(wechat.settings, "WX_SECRET", "s3cret"),
            patch.object(wechat, "_request", fake_request),
            patch.object(wechat, "get_access_token", return_value="TOKEN") as token_mock,
        ):
            wechat.get_phone_number("phone-code")

        self.assertEqual(calls[0][1]["params"], {"access_token": "TOKEN"})
        self.assertEqual(calls[0][1]["base"], wechat._CGI_BASE)
        token_mock.assert_called_once_with()

    def test_token_invalid_triggers_one_refresh_and_retry(self):
        """多副本可能撞车把 token 顶失效，要刷新一次再试。"""
        responses = [
            {"errcode": 40001, "errmsg": "invalid credential"},
            _phone_payload(),
        ]
        tokens = ["OLD", "NEW"]

        def fake_request(path, **kwargs):
            return responses.pop(0)

        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", False),
            patch.object(wechat.settings, "WX_APPID", "wx_test_appid"),
            patch.object(wechat.settings, "WX_SECRET", "s3cret"),
            patch.object(wechat, "_request", fake_request),
            patch.object(wechat, "get_access_token", side_effect=tokens) as token_mock,
        ):
            info = wechat.get_phone_number("phone-code")

        self.assertEqual(info["masked"], "138****8000")
        self.assertEqual(token_mock.call_count, 2)
        self.assertEqual(token_mock.call_args.kwargs, {"force": True})

    def test_token_invalid_twice_gives_up(self):
        def fake_request(path, **kwargs):
            return {"errcode": 40001, "errmsg": "invalid credential"}

        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", False),
            patch.object(wechat.settings, "WX_APPID", "wx_test_appid"),
            patch.object(wechat.settings, "WX_SECRET", "s3cret"),
            patch.object(wechat, "_request", fake_request),
            patch.object(wechat, "get_access_token", return_value="T"),
        ):
            with self.assertRaises(wechat.WeChatError) as ctx:
                wechat.get_phone_number("phone-code")
        self.assertIn("鉴权", str(ctx.exception))


class TestWatermark(unittest.TestCase):
    """手机号回包必须证明「属于本小程序」。"""

    def _run(self, payload, appid="wx_test_appid"):
        with (
            patch.object(wechat.settings, "WX_OPENAPI_ENABLED", True),
            patch.object(wechat.settings, "WX_APPID", appid),
            patch.object(wechat, "_request", lambda *a, **k: payload),
        ):
            return wechat.get_phone_number("code")

    def test_matching_appid_passes(self):
        info = self._run(_phone_payload(appid="wx_test_appid"))
        self.assertEqual(info["pure_phone"], "13800138000")

    def test_mismatched_appid_rejected(self):
        with self.assertRaises(wechat.WeChatError) as ctx:
            self._run(_phone_payload(appid="wx_other_app"), appid="wx_test_appid")
        self.assertIn("不属于当前小程序", str(ctx.exception))

    def test_missing_watermark_rejected_when_appid_known(self):
        payload = _phone_payload()
        payload["phone_info"].pop("watermark")
        with self.assertRaises(wechat.WeChatError):
            self._run(payload)

    def test_skips_check_when_appid_not_configured(self):
        """云调用模式可以只开开关、不配 AppID，此时跳过校验（仅告警）。"""
        info = self._run(_phone_payload(appid="whatever"), appid="")
        self.assertEqual(info["masked"], "138****8000")

    def test_empty_phone_rejected(self):
        payload = _phone_payload()
        payload["phone_info"] = {"watermark": {"appid": "wx_test_appid"}}
        with self.assertRaises(wechat.WeChatError) as ctx:
            self._run(payload)
        self.assertIn("没有返回手机号", str(ctx.exception))


class TestErrorHints(unittest.TestCase):
    def test_hints_are_human_readable(self):
        """这些 message 会直接展示给用户，不能出现技术术语。"""
        for code in (40029, 45011, 48001, -1):
            self.assertIn(code, wechat._ERROR_HINTS)
            self.assertNotIn("errcode", wechat._ERROR_HINTS[code])

    def test_48001_explains_permission_problem(self):
        self.assertIn("权限", wechat._ERROR_HINTS[48001])

    def test_empty_code_rejected(self):
        with self.assertRaises(wechat.WeChatError) as ctx:
            wechat.get_phone_number("   ")
        self.assertIn("code", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
