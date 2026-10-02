"""HTTP 接口。

统一信封::

    成功: {"ok": true,  "data": {...}}
    失败: {"ok": false, "error": {"code": "XXX", "message": "人话"}}

前端按 ``error.code`` 做分支文案，``message`` 直接展示。
"""

from __future__ import annotations

import json
import logging
import uuid
from functools import wraps

from django.conf import settings
from django.http import JsonResponse

from . import quota as quota_service
from . import tasks
from .auth import AuthError, get_identity
from .engine import build_report, is_configured, rewrite_by_rules
from django.db.models import Count, Sum
from django.utils import timezone

from .engine.llm import LLMError as LLMBalanceError
from .engine.llm import get_balance
from .models import Feedback, RewriteTask
from .skills import get_registry

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
VALID_MODES = ("general", "xhs", "academic", "official")
VALID_INTENSITIES = ("light", "medium", "heavy")
# 除了 registry 里扫描到的 skill，额外允许 legacy（显式回退到旧硬编码提示词）
EXTRA_SKILLS = ("legacy",)
DEFAULT_SKILL = "humanizer"


# ---------------------------------------------------------------------------
# 响应helpers
# ---------------------------------------------------------------------------


def ok(data: object, status: int = 200) -> JsonResponse:
    return JsonResponse(
        {"ok": True, "data": data},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def fail(code: str, message: str, status: int = 400) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def api(*methods: str):
    """限制 HTTP 方法，并用统一信封返回 405（Django 默认返回 HTML，前端没法解析）。"""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return fail("METHOD_NOT_ALLOWED", "请求方法不支持", 405)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------


def _identity_or_error(request):
    try:
        return get_identity(request), None
    except AuthError as exc:
        return None, fail("UNAUTHORIZED", str(exc), 401)


def _parse_body(request):
    if len(request.body) > MAX_BODY_BYTES:
        return None, fail("BODY_TOO_LARGE", "请求体太大了", 413)
    try:
        raw = request.body.decode("utf-8")
    except UnicodeDecodeError:
        return None, fail("BAD_ENCODING", "请求体不是合法的 UTF-8")
    if not raw.strip():
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, fail("BAD_JSON", "请求体不是合法的 JSON")
    if not isinstance(data, dict):
        return None, fail("BAD_JSON", "请求体必须是 JSON 对象")
    return data, None


def _extract_text(data: dict):
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, fail("TEXT_EMPTY", "请先粘贴要处理的文本")
    text = text.strip()
    limit = settings.DEAI_MAX_INPUT_CHARS
    if len(text) > limit:
        return None, fail("TEXT_TOO_LONG", f"文本超过 {limit} 字，请分段处理")
    return text, None


def _resolve_skill(raw: object) -> str | None:
    """把请求里的 ``skill`` 解析成可用值；返回 None 表示该 skill 不存在。

    这里刻意**不做静默兜底**：前端传了一个不存在的 skill，就应该明确报错，
    否则用户以为自己在用新 skill，实际跑的是别的，出了问题极难排查。
    """
    name = str(raw or "").strip() or DEFAULT_SKILL
    if name in EXTRA_SKILLS or get_registry().has(name):
        return name
    return None


def _task_payload(task: RewriteTask) -> dict:
    """把任务序列化成前端契约里的结构。

    ``task_status`` 和「同步命中」两条路径共用，避免两处字段慢慢跑偏。
    """
    payload: dict = {
        "status": task.status,
        "rulesText": task.rules_text,
        "elapsedMs": task.elapsed_ms,
    }
    # 已评价过就带上，前端可以显示「你已评价」并回填选项。
    # 放在 status 分支之外：失败的任务也可能被评价（「它根本没改对」也是一种反馈）。
    fb = Feedback.objects.filter(task_id=task.id, openid=task.openid).first()
    if fb is not None:
        payload["feedback"] = {"rating": fb.rating, "reason": fb.reason}
    if task.status == RewriteTask.STATUS_DONE:
        payload["llmText"] = task.llm_text
        payload["model"] = task.model_name
        payload["skill"] = task.skill
        payload["skillVersion"] = task.skill_version
        payload["intensity"] = task.intensity
        # prompt 指纹：用来定位「这次任务用的是哪一份 prompt」
        payload["promptFingerprint"] = task.prompt_fingerprint
        # skill 模式会额外产出检测报告；legacy 模式下是空字符串
        payload["llmReport"] = task.llm_report
        # 模型是否守住了 <REPORT>/<REWRITTEN> 协议（容错解析成功时为 False）
        payload["protocolOk"] = task.protocol_ok
        # 模型自报「补充了哪些原文没有的内容」，前端应显著提示用户核对
        payload["addedFacts"] = task.llm_added_facts
        payload["usage"] = {
            "promptTokens": task.prompt_tokens,
            "completionTokens": task.completion_tokens,
            "totalTokens": task.prompt_tokens + task.completion_tokens,
            "cacheHitTokens": task.cache_hit_tokens,
            "cacheHitRate": (
                round(task.cache_hit_tokens / task.prompt_tokens, 4) if task.prompt_tokens else 0
            ),
            "costCNY": float(task.cost_cny or 0),
        }
        try:
            payload["warnings"] = json.loads(task.warnings or "[]")
        except json.JSONDecodeError:
            payload["warnings"] = []
    elif task.status == RewriteTask.STATUS_FAILED:
        payload["error"] = task.error or "改写失败，请重试"
    return payload


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@api("GET")
def health(request):
    """探活。顺带暴露「模型密钥是否配好」与已加载的 skill，方便部署后自查。"""
    registry = get_registry()
    # 带上 prompt 指纹：部署后一眼就能确认线上跑的是哪一份 prompt，
    # skill 随镜像发布时这是最直接的核对手段。
    fingerprints = {
        s.slug: registry.compile(s.slug).fingerprint for s in registry.list() if registry.has(s.slug)
    }
    return ok(
        {
            "status": "up",
            "llmConfigured": is_configured(),
            "defaultSkill": DEFAULT_SKILL,
            "skills": [s.slug for s in registry.list()] + list(EXTRA_SKILLS),
            "promptFingerprints": fingerprints,
        }
    )


@api("GET")
def skills(request):
    """列出可用的 skill，供前端动态渲染选项。

    前端不该把 general/xhs 写死 —— 加一个 skill 就应该自动出现在选项里。
    """
    _identity, err = _identity_or_error(request)
    if err:
        return err
    items = [s.to_dict() for s in get_registry().list()]
    items.append(
        {
            "slug": "legacy",
            "name": "经典模式",
            "version": "",
            "description": "早期硬编码的提示词，保留用于对比与故障回退。",
            "scenes": list(VALID_MODES),
            "defaultScene": "general",
        }
    )
    return ok(
        {
            "default": DEFAULT_SKILL,
            "scenes": list(VALID_MODES),
            "intensities": list(VALID_INTENSITIES),
            "skills": items,
        }
    )


@api("GET")
def usage(request):
    """账户余额 + 今日用量汇总。

    ⚠️ 余额只保留 2 位小数，**不要**用它做单次调用的费用核算——实测一次
    humanizer 调用约 0.0007 元，余额上看不出变化。单次费用请看
    ``/api/task/<id>`` 返回的 ``usage.costCNY``（取自模型的 usage 字段）。
    余额在这里只用于「够不够用」的粗粒度监控。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err

    payload: dict = {}

    try:
        payload["balance"] = get_balance()
    except LLMBalanceError as exc:
        payload["balanceError"] = str(exc)
    except Exception:  # noqa: BLE001 - 余额查不到不该让整个接口失败
        logger.exception("查询余额失败")
        payload["balanceError"] = "查询余额失败"

    today = timezone.now().date()
    mine = RewriteTask.objects.filter(openid=identity, created_at__date=today)
    done = mine.filter(status=RewriteTask.STATUS_DONE)
    agg = done.aggregate(
        prompt=Sum("prompt_tokens"),
        completion=Sum("completion_tokens"),
        cached=Sum("cache_hit_tokens"),
        cost=Sum("cost_cny"),
    )
    payload["today"] = {
        "tasks": mine.count(),
        "done": done.count(),
        "failed": mine.filter(status=RewriteTask.STATUS_FAILED).count(),
        "promptTokens": agg["prompt"] or 0,
        "completionTokens": agg["completion"] or 0,
        "cacheHitTokens": agg["cached"] or 0,
        "costCNY": float(agg["cost"] or 0),
    }
    return ok(payload)


@api("POST")
def analyze(request):
    """体检 + 规则改写。同步返回，毫秒级，不消耗额度。"""
    identity, err = _identity_or_error(request)
    if err:
        return err
    data, err = _parse_body(request)
    if err:
        return err
    text, err = _extract_text(data)
    if err:
        return err

    rules_text, hits = rewrite_by_rules(text)
    report = build_report(text, hits)
    return ok(
        {
            "report": report,
            "rulesText": rules_text,
            "rulesChanges": sum(1 for h in hits if h.kind == "replace"),
        }
    )


@api("POST")
def rewrite(request):
    """创建 AI 深度改写任务。

    立刻返回规则层结果 + taskId（因为 callContainer 上限 15 秒，等不了模型），
    前端拿到 taskId 后轮询 ``/api/task/<id>``。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err
    data, err = _parse_body(request)
    if err:
        return err
    text, err = _extract_text(data)
    if err:
        return err

    mode = str(data.get("mode") or "general").strip()
    if mode not in VALID_MODES:
        mode = "general"

    skill = _resolve_skill(data.get("skill"))
    if skill is None:
        return fail("SKILL_NOT_FOUND", f"没有这个 skill：{data.get('skill')}", 400)

    # 强度不合法就回落默认值，不报错——它影响的是措辞不是正确性
    intensity = str(data.get("intensity") or "medium").strip()
    if intensity not in VALID_INTENSITIES:
        intensity = "medium"

    if not is_configured():
        return fail("LLM_NOT_CONFIGURED", "服务端还没配置模型密钥，暂时无法深度改写", 503)

    try:
        quota = quota_service.consume(identity)
    except quota_service.QuotaExceeded as exc:
        return fail("QUOTA_EXCEEDED", str(exc), 429)

    rules_text, hits = rewrite_by_rules(text)
    report = build_report(text, hits)

    task_id = uuid.uuid4().hex
    RewriteTask.objects.create(
        id=task_id,
        openid=identity,
        mode=mode,
        skill=skill,
        intensity=intensity,
        source_text=text,
        rules_text=rules_text,
        status=RewriteTask.STATUS_PENDING,
    )
    future = tasks.submit(task_id)

    payload = {
        "taskId": task_id,
        "rulesText": rules_text,
        "report": report,
        "quota": quota,
    }

    # 混合模式：先同步等一小会儿。实测多数改写 0.5-2.3 秒就完成，
    # 直接给结果比让前端立刻开始轮询体验好得多（也少一次网络往返）。
    # 超时就不等了——任务继续在后台跑，前端拿 taskId 轮询即可。
    sync_wait = float(getattr(settings, "DEAI_SYNC_WAIT_SECONDS", 12.0) or 0)
    if sync_wait > 0 and tasks.wait(future, sync_wait):
        task = RewriteTask.objects.filter(pk=task_id).first()
        if task is not None:
            payload.update(_task_payload(task))
        else:  # 任务被清理掉了，退化成轮询
            payload["status"] = "pending"
    else:
        payload["status"] = "pending"

    return ok(payload)


@api("GET")
def task_status(request, task_id: str):
    """轮询任务结果。"""
    identity, err = _identity_or_error(request)
    if err:
        return err

    task = RewriteTask.objects.filter(pk=task_id).first()
    if task is None:
        return fail("TASK_NOT_FOUND", "任务不存在或已过期", 404)
    if task.openid != identity:
        return fail("FORBIDDEN", "无权访问该任务", 403)

    task = tasks.expire_stale(task)

    return ok(_task_payload(task))


@api("GET")
def quota(request):
    """查询今日剩余额度。"""
    identity, err = _identity_or_error(request)
    if err:
        return err
    return ok(quota_service.get_quota(identity))


@api("POST")
def feedback(request):
    """提交对一次改写结果的评价。

    为什么要校验任务归属：不校验的话，任何人拿到一个 taskId 就能刷评价，
    统计就失真了——而统计恰恰是这个接口存在的全部意义。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err
    data, err = _parse_body(request)
    if err:
        return err

    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return fail("TASK_NOT_FOUND", "缺少 taskId", 400)

    rating = str(data.get("rating") or "").strip()
    if rating not in (Feedback.RATING_GOOD, Feedback.RATING_BAD):
        return fail("BAD_RATING", "rating 只能是 good 或 bad", 400)

    task = RewriteTask.objects.filter(pk=task_id).only("id", "openid").first()
    if task is None:
        return fail("TASK_NOT_FOUND", "任务不存在或已过期", 404)
    if task.openid != identity:
        return fail("FORBIDDEN", "无权评价该任务", 403)

    reason = str(data.get("reason") or "").strip()
    if reason and reason not in Feedback.REASON_CHOICES:
        # 未知标签收敛成 other，而不是丢弃——至少还能统计到「有一条说不清的差评」
        reason = "other"
    if rating == Feedback.RATING_GOOD:
        # 满意时不需要问题标签，避免统计里混进无意义的 reason
        reason = ""

    comment = str(data.get("comment") or "")[:1000]

    _obj, created = Feedback.objects.update_or_create(
        task_id=task_id,
        openid=identity,
        defaults={"rating": rating, "reason": reason, "comment": comment},
    )
    logger.info(
        "反馈 task=%s rating=%s reason=%s（%s）",
        task_id,
        rating,
        reason or "-",
        "新建" if created else "更新",
    )
    return ok({"accepted": True, "created": created, "rating": rating})


@api("GET")
def feedback_summary(request):
    """当前用户的反馈统计。

    只返回自己的数据——全局统计请直接在数据库里查，不通过公开接口暴露。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err

    mine = Feedback.objects.filter(openid=identity)
    total = mine.count()
    good = mine.filter(rating=Feedback.RATING_GOOD).count()
    bad = mine.filter(rating=Feedback.RATING_BAD).count()
    rows = (
        mine.filter(rating=Feedback.RATING_BAD)
        .exclude(reason="")
        .values("reason")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return ok(
        {
            "total": total,
            "good": good,
            "bad": bad,
            "goodRate": round(good / total, 4) if total else 0,
            "badReasons": [{"reason": r["reason"], "count": r["count"]} for r in rows],
            "availableReasons": list(Feedback.REASON_CHOICES),
        }
    )
