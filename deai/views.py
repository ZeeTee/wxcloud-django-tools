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
from .models import RewriteTask

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
VALID_MODES = ("general", "xhs")


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


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@api("GET")
def health(request):
    """探活。顺带暴露「模型密钥是否配好」，方便部署后自查。"""
    return ok({"status": "up", "llmConfigured": is_configured()})


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
        source_text=text,
        rules_text=rules_text,
        status=RewriteTask.STATUS_PENDING,
    )
    tasks.submit(task_id)

    return ok(
        {
            "taskId": task_id,
            "rulesText": rules_text,
            "report": report,
            "quota": quota,
        }
    )


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

    payload = {
        "status": task.status,
        "rulesText": task.rules_text,
        "elapsedMs": task.elapsed_ms,
    }
    if task.status == RewriteTask.STATUS_DONE:
        payload["llmText"] = task.llm_text
        payload["model"] = task.model_name
        try:
            payload["warnings"] = json.loads(task.warnings or "[]")
        except json.JSONDecodeError:
            payload["warnings"] = []
    elif task.status == RewriteTask.STATUS_FAILED:
        payload["error"] = task.error or "改写失败，请重试"

    return ok(payload)


@api("GET")
def quota(request):
    """查询今日剩余额度。"""
    identity, err = _identity_or_error(request)
    if err:
        return err
    return ok(quota_service.get_quota(identity))
