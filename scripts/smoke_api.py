#!/usr/bin/env python3
"""端到端冒烟：验证 HTTP 契约与异步任务链路。

特点：**不需要联网、不需要模型密钥**。脚本故意把 LLM 地址指向一个连不上的
端口，于是「创建任务 → 后台线程调用模型 → 轮询 → 失败兜底」这条最容易出错的
链路会被真实走一遍，最终断言任务落到 ``failed`` 且带人话错误。

用法::

    cd server
    PYTHONPATH=./.deps python3 scripts/smoke_api.py

（``PYTHONPATH`` 只在用 --target 装的依赖时需要；正常 venv 里直接 python3 即可。）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

# --- 环境准备：必须在 django.setup() 之前 ------------------------------------

# 脚本在 scripts/ 下，sys.path[0] 是 scripts/，要把项目根加进来才能 import wxcloudrun
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_workdir = tempfile.mkdtemp(prefix="deai-smoke-")
os.environ["SQLITE_PATH"] = os.path.join(_workdir, "smoke.sqlite3")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")
os.environ["DJANGO_DEBUG"] = "false"
os.environ["DEAI_ALLOW_ANONYMOUS"] = "true"
os.environ["DEAI_DAILY_QUOTA"] = "2"  # 把次数压到 2，方便验「耗尽」
os.environ["DEAI_REWRITE_RATE_LIMIT"] = "1000"  # 主流程先关掉限流，单独一节验它
os.environ["LLM_API_KEY"] = "smoke-test-key-not-real"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # discard 端口，必定连不上
os.environ["LLM_TIMEOUT"] = "2"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

client = Client()
AUTH = {"HTTP_X_WX_OPENID": "smoke-openid"}

FAILURES: list[str] = []


def check(name: str, condition: bool, extra: object = "") -> None:
    print(("  [OK]   " if condition else "  [FAIL] ") + name + ("" if condition else f"   <- {extra}"))
    if not condition:
        FAILURES.append(name)


def post_json(path: str, payload: dict, **headers):
    return client.post(
        path, data=json.dumps(payload), content_type="application/json", **headers
    )


AI_TEXT = "首先，我们要明确目标。其次，要不断优化流程。综上所述，这件事至关重要。"

print("\n=== 1. 基础接口 ===")
r = client.get("/api/health", **AUTH)
check("GET /api/health 返回 200", r.status_code == 200, r.status_code)
body = r.json()
check("health 信封正确", body.get("ok") is True and "llmConfigured" in body.get("data", {}), body)
check("health 识别到已配置密钥", body["data"]["llmConfigured"] is True, body)
check("health 暴露了已加载的 skill", "humanizer" in body["data"].get("skills", []), body["data"].get("skills"))
hd = body["data"]
check(
    "health 报告手机号授权就绪（默认云调用模式）",
    hd.get("phoneAuthReady") is True and hd.get("phoneAuthMode") == "cloudcall",
    {k: hd.get(k) for k in ("phoneAuthReady", "phoneAuthMode")},
)
check(
    "health 如实报告每人每天的次数上限",
    hd.get("quotaLimit") == 2,
    hd.get("quotaLimit"),
)

print("\n=== 1.1 Skill 列表 ===")
r = client.get("/api/skills", **AUTH)
check("GET /api/skills 返回 200", r.status_code == 200, r.status_code)
d = r.json().get("data", {})
slugs = [s.get("slug") for s in d.get("skills", [])]
check("含 humanizer", "humanizer" in slugs, slugs)
check("含 legacy 回退项", "legacy" in slugs, slugs)

check("默认 skill 是 humanizer", d.get("default") == "humanizer", d.get("default"))
check(
    "场景列表含全部四个场景",
    set(d.get("scenes", [])) == {"general", "xhs", "academic", "official"},
    d.get("scenes"),
)
check(
    "强度档位齐全",
    set(d.get("intensities", [])) == {"light", "medium", "heavy"},
    d.get("intensities"),
)
humanizer = next((s for s in d.get("skills", []) if s.get("slug") == "humanizer"), {})
check("humanizer 带版本号", bool(humanizer.get("version")), humanizer)

print("\n=== 1.2 身份头诊断接口（/api/debug/headers）===")
r = client.get("/api/debug/headers", **AUTH)
check(
    "默认关闭时按「接口不存在」处理（404，不暴露存在性）",
    r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND",
    r.json(),
)

from django.conf import settings as dj_settings_dbg  # noqa: E402

dj_settings_dbg.DEAI_DEBUG_HEADERS = True

r = client.get(
    "/api/debug/headers",
    HTTP_X_WX_OPENID="o-test",
    HTTP_X_WX_SOURCE="1",
    HTTP_X_WX_FROM_OPENID="o-from",
)
d = r.json().get("data", {})
check("打开后返回 200", r.status_code == 200, r.json())
check("认到 X-WX-OPENID", d.get("identity") == "o-test" and d.get("hasOpenid") is True, d)
check("hasSource 正确", d.get("hasSource") is True, d)
check(
    "回显了 X-WX-FROM-OPENID",
    (d.get("headers") or {}).get("X-WX-FROM-OPENID") == "o-from",
    d.get("headers"),
)

# 什么身份头都没有 = 公网直连的样子。这时更要能把现场打出来，
# 所以这个接口故意不做身份校验（否则会被 401 挡住，什么都看不到）
r = client.get("/api/debug/headers")
d = r.json().get("data", {})
check("无任何身份头时不报 401，而是照常回显", r.status_code == 200, r.status_code)
check("此时 identity 回落到 anonymous", d.get("identity") == "anonymous", d)
check("hasOpenid / hasSource 都是 false", not d.get("hasOpenid") and not d.get("hasSource"), d)

# 资源复用：没有 X-WX-OPENID，只有 FROM 形式
r = client.get("/api/debug/headers", HTTP_X_WX_FROM_OPENID="o-reuse")
d = r.json().get("data", {})
check("资源复用场景认到 X-WX-FROM-OPENID", d.get("identity") == "o-reuse" and d.get("hasFromOpenid") is True, d)
check("此时 hasOpenid 为 false（证明确实是兜底读的）", d.get("hasOpenid") is False, d)

dj_settings_dbg.DEAI_DEBUG_HEADERS = False

print("\n=== 1.3 用量与余额 ===")
r = client.get("/api/usage", **AUTH)
check("GET /api/usage 返回 200", r.status_code == 200, r.status_code)
d = r.json().get("data", {})
check(
    "返回余额或余额错误（假密钥下应为错误）",
    ("balance" in d) or ("balanceError" in d),
    list(d.keys()),
)
check("返回今日用量汇总", isinstance(d.get("today"), dict), list(d.keys()))
today = d.get("today", {})
check(
    "今日用量字段齐全",
    {"tasks", "done", "failed", "promptTokens", "completionTokens", "costCNY"} <= set(today),
    today,
)

print("\n=== 1.5 模板原有功能（确认整合没有把它们改坏）===")
r = client.get("/api/count", **AUTH)
check("GET /api/count 返回 200", r.status_code == 200, r.status_code)
body = r.json()
check(
    "count 仍是模板自己的信封 {code,data}",
    body.get("code") == 0 and "data" in body,
    body,
)
_before = body.get("data")
r = post_json("/api/count", {"action": "inc"}, **AUTH)
_after = r.json().get("data")
check("POST /api/count inc 自增", r.status_code == 200 and _after == _before + 1, (_before, _after))
r = post_json("/api/count", {"action": "clear"}, **AUTH)
check("POST /api/count clear 清零", r.json().get("data") == 0, r.json())
r = post_json("/api/count", {"action": "nonsense"}, **AUTH)
check("非法 action -> code=-1", r.json().get("code") == -1, r.json())
r = client.get("/", **AUTH)
check("GET / 仍返回主页 HTML", r.status_code == 200 and b"<html" in r.content.lower(), r.status_code)

print("\n=== 2. 体检 + 规则改写（同步、不消耗额度）===")
r = post_json("/api/analyze", {"text": AI_TEXT}, **AUTH)
check("POST /api/analyze 返回 200", r.status_code == 200, r.status_code)
d = r.json().get("data", {})
check("返回 report", isinstance(d.get("report"), dict), d.keys())
check("返回 rulesText", isinstance(d.get("rulesText"), str), d.keys())
check("分数落在 0-100", 0 <= d.get("report", {}).get("score", -1) <= 100, d.get("report", {}).get("score"))
check("报告有 hits", len(d.get("report", {}).get("hits", [])) > 0, d.get("report", {}).get("hits"))
check("规则层确有改动", d.get("rulesText") != AI_TEXT, d.get("rulesText"))

print("\n=== 3. 入参校验 ===")
r = post_json("/api/analyze", {"text": "   "}, **AUTH)
check("空文本 -> 400 TEXT_EMPTY", r.status_code == 400 and r.json()["error"]["code"] == "TEXT_EMPTY", r.json())
r = post_json("/api/analyze", {"text": "字" * 5001}, **AUTH)
check("超长 -> 400 TEXT_TOO_LONG", r.status_code == 400 and r.json()["error"]["code"] == "TEXT_TOO_LONG", r.json())
r = post_json("/api/analyze", {}, **AUTH)
check("缺字段 -> 400 TEXT_EMPTY", r.status_code == 400 and r.json()["error"]["code"] == "TEXT_EMPTY", r.json())
r = client.get("/api/analyze", **AUTH)
check("GET 打 POST 接口 -> 405 且是 JSON", r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED", r.status_code)

print("\n=== 3.5 skill 参数校验 ===")
r = post_json("/api/rewrite", {"text": AI_TEXT, "skill": "no-such-skill"}, **AUTH)
check(
    "不存在的 skill -> 400 SKILL_NOT_FOUND（不静默兜底）",
    r.status_code == 400 and r.json()["error"]["code"] == "SKILL_NOT_FOUND",
    r.json(),
)
check(
    "非法 skill 不消耗额度（校验发生在扣额度之前）",
    client.get("/api/quota", **AUTH).json()["data"]["used"] == 0,
)

print("\n=== 4. 额度 ===")
r = client.get("/api/quota", **AUTH)
q = r.json()["data"]
check("初始额度 used=0 limit=2", q["used"] == 0 and q["limit"] == 2 and q["remaining"] == 2, q)
check(
    "返回剩余次数与重置时刻（used/limit/remaining/resetsAt）",
    {"used", "limit", "remaining", "resetsAt"} <= set(q),
    q,
)
check(
    "不再有分档字段（anonymousLimit / verifiedLimit 已移除）",
    "anonymousLimit" not in q and "verifiedLimit" not in q,
    sorted(q.keys()),
)
check("未授权时 verified=false", q.get("verified") is False, q)
check(
    "resetsAt 是「YYYY-MM-DD HH:MM:SS」格式",
    len(str(q.get("resetsAt", ""))) == 19 and str(q.get("resetsAt")).endswith("00:00:00"),
    q.get("resetsAt"),
)

print("\n=== 4.5 手机号授权（不再影响次数）===")
# 用独立的 openid，避免影响第 7 节「次数耗尽」的断言
PHONE_AUTH = {"HTTP_X_WX_OPENID": "smoke-phone-openid"}
r = post_json("/api/auth/login", {}, **PHONE_AUTH)
check(
    "缺 phoneCode -> 400 CODE_EMPTY",
    r.status_code == 400 and r.json()["error"]["code"] == "CODE_EMPTY",
    r.json(),
)

# 冒烟脚本不联网，把微信接口换成本地桩。真实链路（云调用 HTTP 报文、
# watermark 校验、错误码翻译）在 tests/test_phone_auth.py 里覆盖。
from unittest.mock import patch  # noqa: E402

import deai.wechat as wechat_mod  # noqa: E402

FAKE_PHONE = {
    "phone": "+86 13800138000",
    "pure_phone": "13800138000",
    "country_code": "86",
    "masked": "138****8000",
    "fingerprint": "0123456789abcdef0123456789abcdef",
}
with patch.object(wechat_mod, "get_phone_number", return_value=FAKE_PHONE) as phone_mock:
    r = post_json("/api/auth/login", {"phoneCode": "fake-phone-code"}, **PHONE_AUTH)
check("手机号授权 -> 200", r.status_code == 200, r.json())
d = r.json().get("data", {})
check("返回 verified=true", d.get("verified") is True, d)
check("返回打码手机号（不返回完整号码）", d.get("phoneMasked") == "138****8000", d)
check(
    "响应里没有完整手机号",
    "13800138000" not in r.content.decode("utf-8"),
    r.content.decode("utf-8"),
)
check("授权时把 phoneCode 透传给微信", phone_mock.call_args.args[0] == "fake-phone-code", phone_mock.call_args)
check("quota 里 verified 已置位", d.get("quota", {}).get("verified") is True, d.get("quota"))

r = client.get("/api/quota", **PHONE_AUTH)
qd = r.json().get("data", {})
check("再查仍是已授权", qd.get("verified") is True, qd)
check("quota 带回打码手机号", qd.get("phoneMasked") == "138****8000", qd)

# 关键：手机号授权**不再影响次数**。验证过的用户和不验证的用户上限必须一样，
# 这正是这次从「两档」改成「统一 N 次」的核心。
anon_q = client.get("/api/quota", **AUTH).json()["data"]
check(
    "手机号授权不改变次数上限（与未授权用户一致）",
    qd.get("limit") == anon_q.get("limit"),
    {"已授权": qd.get("limit"), "未授权": anon_q.get("limit")},
)
check(
    "授权前后已用次数也不变（不会因为授权被重置）",
    qd.get("used") == 0,
    qd,
)

with patch.object(
    wechat_mod,
    "get_phone_number",
    side_effect=wechat_mod.WeChatError("这个手机号不属于当前小程序，已拒绝"),
):
    r = post_json("/api/auth/login", {"phoneCode": "stolen"}, **PHONE_AUTH)
check(
    "微信侧校验失败 -> 400 WX_PHONE_FAILED",
    r.status_code == 400 and r.json()["error"]["code"] == "WX_PHONE_FAILED",
    r.json(),
)
check(
    "失败文案是人话",
    "不属于当前小程序" in r.json()["error"]["message"],
    r.json()["error"]["message"],
)

r = post_json("/api/auth/login", {"code": "fake-code"}, **PHONE_AUTH)
check(
    "旧 code2Session 路径仍兼容：未配密钥 -> 503",
    r.status_code == 503 and r.json()["error"]["code"] == "WX_NOT_CONFIGURED",
    r.json(),
)

print("\n=== 5. 异步改写链路（模型连不上，验证失败兜底）===")
r = post_json("/api/rewrite", {"text": AI_TEXT, "mode": "general"}, **AUTH)
check("POST /api/rewrite 返回 200", r.status_code == 200, r.json())
d = r.json()["data"]
task_id = d.get("taskId")
check("拿到 taskId", isinstance(task_id, str) and len(task_id) > 0, d.keys())
check("拿到规则层结果（无论如何都有兜底）", isinstance(d.get("rulesText"), str) and d["rulesText"], d.keys())
check("拿到体检报告", isinstance(d.get("report"), dict), d.keys())
check("额度已扣减到 1", d.get("quota", {}).get("used") == 1, d.get("quota"))

# 混合模式：同步窗口内跑完就直接给结果，否则返回 pending 让前端轮询。
# 这里用的是假密钥（模型连不上），任务会快速失败，所以通常走同步返回。
check(
    "返回 status（混合模式：done=同步命中 / pending=转轮询）",
    d.get("status") in ("pending", "done", "failed"),
    d.get("status"),
)
if d.get("status") == "done":
    check("同步命中时必须带 llmText", bool(d.get("llmText")), list(d.keys()))
    check("同步命中时必须带 usage", isinstance(d.get("usage"), dict), list(d.keys()))
if d.get("status") == "failed":
    check("同步失败时带人话 error", bool(d.get("error")), d.get("error"))

deadline = time.monotonic() + 30
status = d.get("status")
payload = d
while status not in ("done", "failed") and time.monotonic() < deadline:
    r = client.get(f"/api/task/{task_id}", **AUTH)
    payload = r.json().get("data", {})
    status = payload.get("status")
    if status in ("done", "failed"):
        break
    time.sleep(0.5)
check("最终进入终态（而不是永远 pending）", status in ("done", "failed"), status)
check("终态下仍可拿到 rulesText 兜底", bool(payload.get("rulesText")), payload.keys())
check(
    "轮询接口也回 taskId（前端切后台回来续跑要用）",
    payload.get("taskId") == task_id,
    payload.get("taskId"),
)
if status == "failed":
    check("失败时有人话 error", bool(payload.get("error")), payload.get("error"))

print("\n=== 5.5 用量与费用来源落库 ===")
# 冒烟里真实的任务一定会失败（模型地址指向 discard 端口），所以这里桩掉
# llm_rewrite 走一遍成功的写入路径，验证 token / 金额 / 来源都真的落库了。
from deai import tasks as deai_tasks  # noqa: E402
from deai.engine.llm import Usage  # noqa: E402
from deai.models import RewriteTask  # noqa: E402

USAGE_TASK_ID = "smoke-usage-task"
RewriteTask.objects.filter(pk=USAGE_TASK_ID).delete()
RewriteTask.objects.create(
    id=USAGE_TASK_ID,
    openid="smoke-usage",
    mode="general",
    skill="humanizer",
    intensity="medium",
    source_text="首先，我们要明确目标。",
    rules_text="规则版",
    status=RewriteTask.STATUS_PENDING,
)

_fake_result = {
    "text": "改好的文本",
    "report": "",
    "warnings": [],
    "model": "deepseek-chat",
    "provider": "deepseek",
    "skill": "humanizer",
    "skillVersion": "4.1.0",
    "promptFingerprint": "abc123456789",
    "protocolOk": True,
    "addedFacts": "",
    "usage": Usage(
        prompt_tokens=15233, completion_tokens=77, cache_hit_tokens=14592
    ),
    # DeepSeek 不返回 cost，所以走本地价格表 —— 这正是要落库区分的那种情况
    "cost": {"costCNY": 0.002482, "source": "local_table", "provider": "deepseek"},
}

with patch.object(deai_tasks, "llm_rewrite", return_value=_fake_result):
    deai_tasks._run(USAGE_TASK_ID)

u_task = RewriteTask.objects.get(pk=USAGE_TASK_ID)
check("status 落库为 done", u_task.status == RewriteTask.STATUS_DONE, u_task.status)
check(
    "token 用量落库",
    u_task.prompt_tokens == 15233
    and u_task.completion_tokens == 77
    and u_task.cache_hit_tokens == 14592,
    (u_task.prompt_tokens, u_task.completion_tokens, u_task.cache_hit_tokens),
)
check("金额落库", float(u_task.cost_cny) == 0.002482, float(u_task.cost_cny))
check(
    "费用来源落库（provider / local_table）",
    u_task.cost_source == "local_table",
    u_task.cost_source,
)

r = client.get(f"/api/task/{USAGE_TASK_ID}", HTTP_X_WX_OPENID="smoke-usage")
usage = r.json().get("data", {}).get("usage", {})
check("接口 200", r.status_code == 200, r.status_code)
check("usage 返回 token 用量", usage.get("totalTokens") == 15310, usage)
check(
    "usage 返回 cacheHitRate",
    usage.get("cacheHitRate") == round(14592 / 15233, 4),
    usage.get("cacheHitRate"),
)
check("usage 返回 costCNY", usage.get("costCNY") == 0.002482, usage.get("costCNY"))
check(
    "usage 返回 costSource（前端据此标「估算值」）",
    usage.get("costSource") == "local_table",
    usage.get("costSource"),
)
check(
    "usage 字段集合与文档一致",
    set(usage.keys())
    == {
        "promptTokens",
        "completionTokens",
        "totalTokens",
        "cacheHitTokens",
        "cacheHitRate",
        "costCNY",
        "costSource",
    },
    sorted(usage.keys()),
)

print("\n=== 6. 任务归属 ===")
r = client.get(f"/api/task/{task_id}", HTTP_X_WX_OPENID="someone-else")
check("他人任务 -> 403 FORBIDDEN", r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", r.json())
r = client.get("/api/task/does-not-exist", **AUTH)
check("不存在的任务 -> 404 TASK_NOT_FOUND", r.status_code == 404 and r.json()["error"]["code"] == "TASK_NOT_FOUND", r.json())

print("\n=== 6.5 用户反馈 ===")
r = post_json("/api/feedback", {"taskId": "does-not-exist", "rating": "good"}, **AUTH)
check(
    "评价不存在的任务 -> 404 TASK_NOT_FOUND",
    r.status_code == 404 and r.json()["error"]["code"] == "TASK_NOT_FOUND",
    r.json(),
)
r = post_json("/api/feedback", {"taskId": task_id, "rating": "nonsense"}, **AUTH)
check(
    "非法 rating -> 400 BAD_RATING",
    r.status_code == 400 and r.json()["error"]["code"] == "BAD_RATING",
    r.json(),
)
r = post_json("/api/feedback", {"taskId": task_id, "rating": "bad", "reason": "not_natural"}, **AUTH)
check(
    "提交差评 -> 200 且 created=true",
    r.status_code == 200 and r.json()["data"].get("created") is True,
    r.json(),
)
r = post_json("/api/feedback", {"taskId": task_id, "rating": "good", "reason": "not_natural"}, **AUTH)
check(
    "重复提交视为改主意（created=false，不堆记录）",
    r.status_code == 200 and r.json()["data"].get("created") is False,
    r.json(),
)
r = post_json("/api/feedback", {"taskId": task_id, "rating": "good"}, HTTP_X_WX_OPENID="someone-else")
check(
    "评价他人任务 -> 403 FORBIDDEN（防刷统计）",
    r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN",
    r.json(),
)

r = client.get("/api/feedback/summary", **AUTH)
check("GET /api/feedback/summary 返回 200", r.status_code == 200, r.status_code)
d = r.json().get("data", {})
check("统计：total=1 / good=1（改主意后只算最后一次）", d.get("total") == 1 and d.get("good") == 1, d)
check("返回可选原因枚举", "not_natural" in (d.get("availableReasons") or []), d.get("availableReasons"))

r = client.get(f"/api/task/{task_id}", **AUTH)
check(
    "任务详情回填已评价状态",
    (r.json().get("data", {}).get("feedback") or {}).get("rating") == "good",
    r.json().get("data", {}).get("feedback"),
)

print("\n=== 6.8 改写接口的频率限制 ===")
# 用全新 openid，窗口内计数从 0 开始，断言才确定。
# 限流默认 5 次/60 秒，这里临时压到 2 次，少打几发。
RATE_AUTH = {"HTTP_X_WX_OPENID": "smoke-rate-openid"}

from django.conf import settings as dj_settings_rate  # noqa: E402

_old_rate = dj_settings_rate.DEAI_REWRITE_RATE_LIMIT
dj_settings_rate.DEAI_REWRITE_RATE_LIMIT = 2
dj_settings_rate.DEAI_DAILY_QUOTA = 50  # 让每日次数远高于频率限制，避免混淆两者

try:
    # 前两次应当放行
    for i in (1, 2):
        r = post_json("/api/rewrite", {"text": AI_TEXT}, **RATE_AUTH)
        check(f"窗口内第 {i} 次放行", r.status_code == 200, r.json())

    used_before = client.get("/api/quota", **RATE_AUTH).json()["data"]["used"]

    # 第 3 次应当被频率限制挡下
    r = post_json("/api/rewrite", {"text": AI_TEXT}, **RATE_AUTH)
    body = r.json()
    check(
        "超出频率 -> 429 RATE_LIMITED（而不是 QUOTA_EXCEEDED）",
        r.status_code == 429 and body.get("error", {}).get("code") == "RATE_LIMITED",
        body,
    )
    msg = body.get("error", {}).get("message", "")
    check("错误文案里有「太频繁」和等待秒数", "太频繁" in msg and "秒后再试" in msg, msg)

    used_after = client.get("/api/quota", **RATE_AUTH).json()["data"]["used"]
    check(
        "被限流的请求不扣每日次数",
        used_before == used_after,
        {"限流前 used": used_before, "限流后 used": used_after},
    )

    # 关掉限流应当立刻恢复
    dj_settings_rate.DEAI_REWRITE_RATE_LIMIT = 0
    r = post_json("/api/rewrite", {"text": AI_TEXT}, **RATE_AUTH)
    check("DEAI_REWRITE_RATE_LIMIT=0 时关闭限流", r.status_code == 200, r.json())
finally:
    dj_settings_rate.DEAI_REWRITE_RATE_LIMIT = _old_rate
    dj_settings_rate.DEAI_DAILY_QUOTA = 2

print("\n=== 7. 额度耗尽 ===")
# 注意：额度按「北京时间当天」重置。如果测试恰好跨过午夜（真的遇到过：
# 第 5 部分在 23:59 消耗、第 7 部分在 00:00 检查，计数已归零），
# 「第 N 次一定失败」这种断言就会假失败。所以改成一直调到触顶为止。
hit_limit = False
for attempt in range(1, 6):
    r = post_json("/api/rewrite", {"text": AI_TEXT}, **AUTH)
    if r.status_code == 429:
        body = r.json()
        check(
            f"第 {attempt} 次触顶 -> 429 QUOTA_EXCEEDED",
            body.get("error", {}).get("code") == "QUOTA_EXCEEDED",
            body,
        )
        check(
            "次数耗尽的错误文案说清上限和恢复时间",
            all(k in body.get("error", {}).get("message", "") for k in ("2 次", "明天", "恢复")),
            body.get("error", {}).get("message"),
        )
        hit_limit = True
        break
    check(f"第 {attempt} 次仍可用（返回 200）", r.status_code == 200, r.status_code)
check("额度最终会被耗尽（对跨天鲁棒）", hit_limit, "调了 5 次仍未触顶")

print("\n=== 8. 身份校验（关闭匿名）===")
from django.conf import settings as dj_settings  # noqa: E402

dj_settings.DEAI_ALLOW_ANONYMOUS = False
r = post_json("/api/analyze", {"text": AI_TEXT})
check("无身份 -> 401 UNAUTHORIZED", r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED", r.json())

print("\n" + "=" * 60)
if FAILURES:
    print(f"冒烟失败 {len(FAILURES)} 项：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部冒烟项通过 ✓")
