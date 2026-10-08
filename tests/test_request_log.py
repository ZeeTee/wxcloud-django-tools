"""请求日志中间件单测。

两个重点：
1. **日志内容对不对**——身份头、布尔标记、参数、截断、敏感头过滤；
2. **中间件不能把请求搞坏**——它在 view 之前读了 ``request.body``，
   必须确认 view 之后还能读到同一份（这是最容易踩的坑）。

按项目惯例**不依赖数据库**：所有用例都打在不碰 DB 的接口上
（``/api/health`` ``/api/skills`` ``/api/analyze``）。``/api/quota`` 会查
``UserProfile``，在没有 test DB 的情况下会直接抛 OperationalError。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from deai import middleware  # noqa: E402

# 用来验 body 的接口必须是「读 body 且不碰数据库」的
BODY_ENDPOINT = "/api/analyze"
# 简单的 GET，不碰数据库
GET_ENDPOINT = "/api/health"


def encode(payload: dict) -> bytes:
    """按真实客户端的方式编码：不转义中文，字节是 UTF-8。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def get_logs(testcase, call):
    """跑一次请求，返回它产生的日志文本。"""
    with testcase.assertLogs("deai.middleware", level="INFO") as captured:
        call()
    return "\n".join(captured.output)


class TestRequestLogContent(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_logs_identity_header(self):
        text = get_logs(
            self,
            lambda: self.client.get(
                GET_ENDPOINT,
                HTTP_X_WX_OPENID="o-abc123",
                HTTP_X_WX_APPID="wx123",
                HTTP_X_WX_ENV="prod-1",
                HTTP_X_WX_SOURCE="1",
            ),
        )
        self.assertIn(f"[req] GET {GET_ENDPOINT}", text)
        self.assertIn("has_openid=True", text)
        self.assertIn("has_identity=True", text)
        self.assertIn("openid=o-abc123", text)
        self.assertIn("appid=wx123", text)
        self.assertIn("env=prod-1", text)
        self.assertIn("source=1", text)

    def test_missing_headers_are_explicit(self):
        """没有值时打 '-'，而不是把字段省掉——省掉的话看日志会以为是漏打。"""
        text = get_logs(self, lambda: self.client.get(GET_ENDPOINT))
        self.assertIn("has_openid=False", text)
        self.assertIn("has_identity=False", text)
        self.assertIn("openid=-", text)
        self.assertIn("source=-", text)

    def test_from_openid_only_is_not_reported_as_has_openid(self):
        """资源复用场景：只有 X-WX-FROM-OPENID。

        这时 has_openid 必须是 False——否则会打出「has_openid=True 但 openid=-」
        这种自相矛盾的日志，排查时反而误导人。
        """
        text = get_logs(
            self,
            lambda: self.client.get("/api/skills", HTTP_X_WX_FROM_OPENID="o-reuse"),
        )
        self.assertIn("has_openid=False", text)
        self.assertIn("has_identity=True", text)
        self.assertIn("from_openid=o-reuse", text)
        self.assertNotIn("has_openid=True", text)

    def test_logs_json_body_as_readable_chinese(self):
        """客户端发的是 UTF-8 中文时，日志里也要是中文，不是 \\uXXXX 转义。"""
        text = get_logs(
            self,
            lambda: self.client.post(
                BODY_ENDPOINT,
                data=encode({"text": "首先，我们要明确目标。"}),
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            ),
        )
        self.assertIn("首先，我们要明确目标。", text)
        self.assertNotIn("\\u9996", text)

    def test_escaped_json_body_is_decoded_for_readability(self):
        """客户端真发了 \\uXXXX 转义时，日志里也重排成可读中文。"""
        escaped = json.dumps({"text": "首先"}, ensure_ascii=True).encode("utf-8")
        text = get_logs(
            self,
            lambda: self.client.post(
                BODY_ENDPOINT,
                data=escaped,
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            ),
        )
        self.assertIn("首先", text)

    def test_illegal_json_body_is_logged_raw(self):
        text = get_logs(
            self,
            lambda: self.client.post(
                BODY_ENDPOINT,
                data=b"{not json at all",
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            ),
        )
        self.assertIn("{not json at all", text)

    def test_logs_query_string(self):
        text = get_logs(self, lambda: self.client.get(GET_ENDPOINT + "?foo=bar&baz=1"))
        self.assertIn("query=foo=bar&baz=1", text)

    def test_logs_status_code_and_duration(self):
        text = get_logs(self, lambda: self.client.get(GET_ENDPOINT))
        self.assertIn("-> 200", text)
        self.assertIn("ms", text)

    def test_logs_forwarded_ip(self):
        text = get_logs(
            self,
            lambda: self.client.get(GET_ENDPOINT, HTTP_X_ORIGINAL_FORWARDED_FOR="1.2.3.4"),
        )
        self.assertIn("ip=1.2.3.4", text)

    def test_authorization_and_cookie_are_dropped(self):
        """这两个头将来可能带凭证，绝不进日志。"""
        text = get_logs(
            self,
            lambda: self.client.get(
                GET_ENDPOINT,
                HTTP_AUTHORIZATION="Bearer super-secret-token",
                HTTP_COOKIE="sessionid=deadbeef",
            ),
        )
        self.assertNotIn("super-secret-token", text)
        self.assertNotIn("deadbeef", text)

    def test_long_body_is_truncated_with_marker(self):
        with patch.object(middleware.settings, "DEAI_LOG_MAX_CHARS", 200):
            text = get_logs(
                self,
                lambda: self.client.post(
                    BODY_ENDPOINT,
                    data=encode({"text": "啊" * 5000}),
                    content_type="application/json",
                    HTTP_X_WX_OPENID="o-x",
                ),
            )
        self.assertIn("截断", text)
        self.assertLess(len(text), 3000, "截断没生效，日志里还是整个 body")

    def test_one_line_per_request(self):
        """一个请求只占一行，多行会让人没法 grep。"""
        text = get_logs(
            self,
            lambda: self.client.post(
                BODY_ENDPOINT,
                data=encode({"text": "第一行\n第二行\n第三行"}),
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            ),
        )
        self.assertEqual(len([ln for ln in text.splitlines() if "[req]" in ln]), 1, text)


class TestMiddlewareDoesNotBreakRequest(unittest.TestCase):
    """中间件在 view 之前读了 body，必须确认 view 还能读到。"""

    def setUp(self):
        self.client = Client()

    def test_view_still_gets_its_body(self):
        """body 被中间件吃掉的话，这里会变成 400 TEXT_EMPTY。"""
        with self.assertLogs("deai.middleware", level="INFO"):
            resp = self.client.post(
                BODY_ENDPOINT,
                data=encode({"text": "首先，我们要明确目标。其次，要不断优化流程。"}),
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        data = resp.json()["data"]
        self.assertIn("report", data)
        self.assertTrue(data["report"].get("hits") is not None)

    def test_analyze_actually_saw_the_text(self):
        """再确认一次 view 拿到的确实是请求里那份文本，不是空串。"""
        with self.assertLogs("deai.middleware", level="INFO"):
            resp = self.client.post(
                BODY_ENDPOINT,
                data=encode({"text": "首先，我们要明确目标。"}),
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            )
        self.assertEqual(resp.json()["data"]["report"]["charCount"], 11)

    def test_empty_body_does_not_crash(self):
        with self.assertLogs("deai.middleware", level="INFO") as captured:
            resp = self.client.get(GET_ENDPOINT)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any("[req]" in line for line in captured.output))

    def test_broken_json_body_still_returns_400(self):
        with self.assertLogs("deai.middleware", level="INFO") as captured:
            resp = self.client.post(
                BODY_ENDPOINT,
                data=b"{this is not json",
                content_type="application/json",
                HTTP_X_WX_OPENID="o-x",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any("[req]" in line for line in captured.output))

    def test_rejected_request_is_also_logged(self):
        """401 的请求更要记——排查身份问题看的就是它。

        要显式关掉 DEAI_ALLOW_ANONYMOUS：它默认跟着 DEBUG 走，
        本地带 DJANGO_DEBUG=1 跑测试时是开的，不带 openid 也能过。
        """
        with patch.object(middleware.settings, "DEAI_ALLOW_ANONYMOUS", False):
            with self.assertLogs("deai.middleware", level="INFO") as captured:
                resp = self.client.get("/api/skills")  # 没带 openid
        self.assertEqual(resp.status_code, 401, resp.content[:200])
        self.assertTrue(any("-> 401" in line for line in captured.output), captured.output)

    def test_404_is_also_logged(self):
        with self.assertLogs("deai.middleware", level="INFO") as captured:
            self.client.get("/api/nope")
        self.assertTrue(any("-> 404" in line for line in captured.output), captured.output)


class TestSwitch(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_disabled_emits_nothing(self):
        with patch.object(middleware.settings, "DEAI_LOG_REQUESTS", False):
            with self.assertNoLogs("deai.middleware", level="INFO"):
                self.client.get(GET_ENDPOINT, HTTP_X_WX_OPENID="o-x")

    def test_disabled_still_serves_request(self):
        with patch.object(middleware.settings, "DEAI_LOG_REQUESTS", False):
            resp = self.client.get(GET_ENDPOINT, HTTP_X_WX_OPENID="o-x")
        self.assertEqual(resp.status_code, 200)

    def test_enabled_by_default(self):
        """默认必须是开的：关着的话部署上去什么都看不到，还得再来一轮。"""
        from django.conf import settings

        self.assertTrue(settings.DEAI_LOG_REQUESTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
