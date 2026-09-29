"""LLM 改写的后台执行。

为什么必须异步：``wx.cloud.callContainer`` 单次请求上限 15 秒，而 LLM 改写要
10-60 秒；而且小程序**切到后台 5 秒后请求会被系统杀掉**。所以只能是
「创建任务 → 轮询结果」。

云托管的扩缩容/重启会杀掉后台线程，因此任务必须落库，并且提供两种兜底：
* ``expire_stale``：轮询时把超时仍停在 pending/running 的任务判为失败；
* 前端可以重新提交（重新消耗一次额度，但至少不会卡死）。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.utils import timezone

from .engine import LLMError
from .engine import rewrite as llm_rewrite
from .models import RewriteTask

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(
    max_workers=max(1, settings.DEAI_WORKERS),
    thread_name_prefix="deai-rewrite",
)


def submit(task_id: str) -> None:
    """把任务丢进线程池，立即返回。"""
    _executor.submit(_run, task_id)


def _run(task_id: str) -> None:
    started = time.monotonic()
    task = (
        RewriteTask.objects.filter(pk=task_id)
        .only("id", "source_text", "mode", "skill")
        .first()
    )
    if task is None:  # 任务被清理掉了
        return

    RewriteTask.objects.filter(pk=task_id).update(status=RewriteTask.STATUS_RUNNING)

    try:
        result = llm_rewrite(task.source_text, task.mode, skill=task.skill)
    except LLMError as exc:
        _fail(task_id, str(exc), started)
        logger.warning("任务 %s 模型调用失败: %s", task_id, exc)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
        _fail(task_id, "服务端处理失败，请稍后重试", started)
        logger.exception("任务 %s 未预期异常: %s", task_id, exc)
    else:
        RewriteTask.objects.filter(pk=task_id).update(
            status=RewriteTask.STATUS_DONE,
            llm_text=result["text"],
            llm_report=result.get("report") or "",
            warnings=json.dumps(result.get("warnings") or [], ensure_ascii=False),
            model_name=result.get("model", ""),
            # 记下实际用的 skill 与版本：发生回退时这里会和请求的不一致，
            # 正好是排查「为什么这次效果不一样」的线索
            skill=result.get("skill") or task.skill,
            skill_version=result.get("skillVersion") or "",
            protocol_ok=bool(result.get("protocolOk", True)),
            error="",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "任务 %s 完成，skill=%s/%s protocol_ok=%s 耗时 %dms",
            task_id,
            result.get("skill"),
            result.get("skillVersion"),
            result.get("protocolOk"),
            int((time.monotonic() - started) * 1000),
        )


def _fail(task_id: str, message: str, started: float) -> None:
    RewriteTask.objects.filter(pk=task_id).update(
        status=RewriteTask.STATUS_FAILED,
        error=message,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def expire_stale(task: RewriteTask) -> RewriteTask:
    """把超时仍未结束的任务判为失败。返回（可能已更新的）任务对象。"""
    if task.status not in (RewriteTask.STATUS_PENDING, RewriteTask.STATUS_RUNNING):
        return task

    age = (timezone.now() - task.created_at).total_seconds()
    if age <= settings.DEAI_TASK_TIMEOUT_SECONDS:
        return task

    message = "改写超时了，可能是文本太长或服务繁忙，请重试"
    RewriteTask.objects.filter(pk=task.pk).update(
        status=RewriteTask.STATUS_FAILED,
        error=message,
    )
    task.status = RewriteTask.STATUS_FAILED
    task.error = message
    return task
