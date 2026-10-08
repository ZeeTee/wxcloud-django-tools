#!/usr/bin/env python3
"""接口文档一致性校验：README 第六节写的字段，和接口真实返回的是不是一回事。

为什么需要它
------------
接口文档最危险的漂移不是「写错了」，而是「代码改了、文档没跟上」——
这种漂移项目里没有任何东西会报警，只能靠人肉发现。

这个脚本把 README 第六节「接口详解」里的字段承诺变成可执行的断言：
**改了接口字段却忘了改文档，跑一下就会红。**

特点
----
- **不需要联网、不需要模型密钥**：模型地址指向 discard 端口，余额查询必然失败，
  正好也验证了「余额查不到时接口仍返回 200、只是换成 balanceError」这条承诺。
- 用临时 SQLite，不碰任何真实数据库。

用法::

    PYTHONPATH=./.deps python3 scripts/check_docs.py
    python3 scripts/check_docs.py            # 正常 venv 里直接跑

注意：本脚本里的字段集是**照着 README 抄的**。如果你改了接口并且是有意的，
就同时改 README 和这里；只改其中一处，这个脚本就会失败——这正是它的作用。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# --- 环境准备：必须在 django.setup() 之前 ------------------------------------

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_workdir = tempfile.mkdtemp(prefix="deai-doccheck-")
os.environ["SQLITE_PATH"] = os.path.join(_workdir, "doccheck.sqlite3")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")
os.environ["DJANGO_DEBUG"] = "false"
os.environ["DEAI_ALLOW_ANONYMOUS"] = "true"
os.environ["LLM_API_KEY"] = "doccheck-not-real"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # discard 端口，必定连不上
os.environ["LLM_TIMEOUT"] = "2"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

client = Client()
AUTH = {"HTTP_X_WX_OPENID": "doccheck-openid"}

FAILURES: list[str] = []


def check(name: str, condition: bool, extra: object = "") -> None:
    print(("  [OK]   " if condition else "  [FAIL] ") + name + ("" if condition else f"   <- {extra}"))
    if not condition:
        FAILURES.append(name)


# --- README 第六节承诺的字段集 ----------------------------------------------

# GET 接口的顶层字段
DOC_TOP = {
    "/api/health": {
        "status", "llmConfigured", "llm", "defaultSkill", "skills",
        "promptFingerprints", "phoneAuthReady", "phoneAuthMode", "quotaLimit",
    },
    "/api/skills": {"default", "scenes", "intensities", "skills"},
    "/api/quota": {
        "used", "limit", "remaining", "resetsAt", "verified", "phoneMasked",
    },
    "/api/usage": {"balance", "balanceError", "today"},
    "/api/feedback/summary": {
        "total", "good", "bad", "goodRate", "badReasons", "availableReasons",
    },
}

# 嵌套结构
DOC_HEALTH_LLM = {"provider", "providerLabel", "model", "baseUrl", "keyConfigured", "available"}
DOC_USAGE_TODAY = {
    "tasks", "done", "failed",
    "promptTokens", "completionTokens", "cacheHitTokens", "costCNY",
}
DOC_SKILL_ITEM = {"slug", "name", "version", "description", "scenes", "defaultScene"}

# 「二选一」的字段：文档写明两者只会出现一个
EITHER_OR = {"/api/usage": {"balance", "balanceError"}}


def get(path: str) -> dict:
    return client.get(path, **AUTH).json()


print("=== 1. GET 接口顶层字段 ===")
for path, expected in DOC_TOP.items():
    resp = client.get(path, **AUTH)
    check(f"{path} 返回 200", resp.status_code == 200, resp.status_code)
    body = resp.json()
    if not body.get("ok"):
        check(f"{path} 信封 ok=true", False, body)
        continue

    got = set(body["data"].keys())
    missing = expected - got
    extra = got - expected

    # 二选一字段：少一个不算错，因为另一个必然在（下面单独断言）
    either = EITHER_OR.get(path)
    if either and (got & either):
        missing -= either

    check(f"{path} 文档写了的字段都存在", not missing, f"文档有、接口没有：{sorted(missing)}")
    check(f"{path} 没有未写进文档的字段", not extra, f"接口有、文档没写：{sorted(extra)}")

print("\n=== 2. 嵌套结构与二选一 ===")
usage = get("/api/usage")["data"]
either_present = EITHER_OR["/api/usage"] & set(usage.keys())
check("usage 恰好返回 balance / balanceError 之一", len(either_present) == 1, sorted(either_present))
check(
    "连不上模型时返回 balanceError（而不是让接口失败）",
    "balanceError" in usage,
    sorted(usage.keys()),
)
check(
    "usage.today 字段一致",
    set(usage.get("today", {}).keys()) == DOC_USAGE_TODAY,
    f"差集：{set(usage.get('today', {}).keys()) ^ DOC_USAGE_TODAY}",
)

health = get("/api/health")["data"]
check(
    "health.llm 字段一致",
    set((health.get("llm") or {}).keys()) == DOC_HEALTH_LLM,
    f"差集：{set((health.get('llm') or {}).keys()) ^ DOC_HEALTH_LLM}",
)
check(
    "health.quotaLimit 是正整数",
    isinstance(health.get("quotaLimit"), int) and health["quotaLimit"] > 0,
    health.get("quotaLimit"),
)
check(
    "health.phoneAuthMode ∈ {cloudcall, token}",
    health.get("phoneAuthMode") in ("cloudcall", "token"),
    health.get("phoneAuthMode"),
)

skills = get("/api/skills")["data"]
first = (skills.get("skills") or [{}])[0]
check(
    "skills[] 元素字段一致",
    set(first.keys()) == DOC_SKILL_ITEM,
    f"差集：{set(first.keys()) ^ DOC_SKILL_ITEM}",
)
check("skills.default 是字符串", isinstance(skills.get("default"), str), skills.get("default"))

print("\n=== 3. /api/quota 的语义（README 里逐条写过的）===")
quota = get("/api/quota")["data"]
check("remaining == max(0, limit - used)", quota["remaining"] == max(0, quota["limit"] - quota["used"]), quota)
check(
    "limit == 配置的 DEAI_DAILY_QUOTA（不再是按用户算出来的档位）",
    quota["limit"] == settings.DEAI_DAILY_QUOTA,
    {"返回": quota["limit"], "配置": settings.DEAI_DAILY_QUOTA},
)
check(
    "resetsAt 在未来 24 小时内（北京时间 0 点）",
    quota["resetsAt"].endswith("00:00:00") and len(quota["resetsAt"]) == 19,
    quota["resetsAt"],
)
check("未授权时 phoneMasked 为空串", quota["phoneMasked"] == "", repr(quota["phoneMasked"]))
check("初始 used == 0", quota["used"] == 0, quota["used"])
check("remaining == limit（还没用过）", quota["remaining"] == quota["limit"], quota)

# 「这个接口只读，不消耗额度」
check("连查两次额度不变（只读）", get("/api/quota")["data"]["used"] == 0)

analyze = client.post(
    "/api/analyze",
    data=json.dumps({"text": "首先，我们要明确目标。其次，要不断优化流程。"}),
    content_type="application/json",
    **AUTH,
)
check("/api/analyze 返回 200", analyze.status_code == 200, analyze.status_code)
check("/api/analyze 不消耗额度", get("/api/quota")["data"]["used"] == 0)

print("\n=== 4. 未知接口不该静默返回 200 ===")
check("/api/nope 不是 200", client.get("/api/nope", **AUTH).status_code != 200)

print("\n" + "=" * 60)
if FAILURES:
    print(f"文档与接口不一致 {len(FAILURES)} 处：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("文档与接口完全一致 ✓")
