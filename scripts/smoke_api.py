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
os.environ["DEAI_DAILY_LIMIT"] = "2"
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

print("\n=== 1.2 用量与余额 ===")
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
if status == "failed":
    check("失败时有人话 error", bool(payload.get("error")), payload.get("error"))

print("\n=== 6. 任务归属 ===")
r = client.get(f"/api/task/{task_id}", HTTP_X_WX_OPENID="someone-else")
check("他人任务 -> 403 FORBIDDEN", r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", r.json())
r = client.get("/api/task/does-not-exist", **AUTH)
check("不存在的任务 -> 404 TASK_NOT_FOUND", r.status_code == 404 and r.json()["error"]["code"] == "TASK_NOT_FOUND", r.json())

print("\n=== 7. 额度耗尽 ===")
r = post_json("/api/rewrite", {"text": AI_TEXT}, **AUTH)
check("第 2 次改写成功（used=2）", r.status_code == 200 and r.json()["data"]["quota"]["used"] == 2, r.json().get("data", {}).get("quota"))
r = post_json("/api/rewrite", {"text": AI_TEXT}, **AUTH)
check("第 3 次 -> 429 QUOTA_EXCEEDED", r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED", r.json())

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
