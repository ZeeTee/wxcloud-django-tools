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
from concurrent.futures import TimeoutError as FuturesTimeout
from decimal import Decimal

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


def submit(task_id: str):
    """把任务丢进线程池，返回 ``Future``。

    返回 Future 是为了支持「同步等一小会儿」的混合模式：实测一次改写只要
    0.5-2.3 秒，与其让前端立刻开始轮询，不如在这里等一等直接把结果给出去。
    """
    return _executor.submit(_run, task_id)


def wait(future, timeout: float) -> bool:
    """最多等 ``timeout`` 秒，返回任务是否已结束（成功或失败都算结束）。

    超时时**刻意不 cancel**：任务可能已经写了一半状态，让它跑完更安全，
    前端拿 taskId 转轮询即可。
    """
    try:
        future.result(timeout=max(0.0, timeout))
        return True
    except FuturesTimeout:
        return False
    except Exception:  # noqa: BLE001 - _run 内部已兜住异常，这里只是防御
        logger.exception("等待任务结束时出现异常")
        return True


def _run(task_id: str) -> None:
    started = time.monotonic()
    task = (
        RewriteTask.objects.filter(pk=task_id)
        .only("id", "source_text", "mode", "skill", "intensity")
        .first()
    )
    if task is None:  # 任务被清理掉了
        return

    RewriteTask.objects.filter(pk=task_id).update(status=RewriteTask.STATUS_RUNNING)

    try:
        result = llm_rewrite(task.source_text, task.mode, skill=task.skill, intensity=task.intensity)
    except LLMError as exc:
        _fail(task_id, str(exc), started)
        logger.warning("任务 %s 模型调用失败: %s", task_id, exc)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
        _fail(task_id, "服务端处理失败，请稍后重试", started)
        logger.exception("任务 %s 未预期异常: %s", task_id, exc)
    else:
        usage = result.get("usage")
        cost = result.get("cost") or {}
        RewriteTask.objects.filter(pk=task_id).update(
            status=RewriteTask.STATUS_DONE,
            llm_text=result["text"],
            llm_report=result.get("report") or "",
            warnings=json.dumps(result.get("warnings") or [], ensure_ascii=False),
            model_name=result.get("model", ""),
            provider=result.get("provider") or "",
            # 记下实际用的 skill 与版本：发生回退时这里会和请求的不一致，
            # 正好是排查「为什么这次效果不一样」的线索
            skill=result.get("skill") or task.skill,
            skill_version=result.get("skillVersion") or "",
            prompt_fingerprint=result.get("promptFingerprint") or "",
            protocol_ok=bool(result.get("protocolOk", True)),
            llm_added_facts=result.get("addedFacts") or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_hit_tokens=getattr(usage, "cache_hit_tokens", 0) or 0,
            cost_cny=Decimal(str(cost.get("costCNY") or 0)),
            # 把「这个金额是怎么来的」也存下来：provider = 供应商上报，
            # local_table = 按本地价格表估算。不存的话事后分不出可信度。
            cost_source=str(cost.get("source") or ""),
            error="",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "任务 %s 完成，skill=%s/%s protocol_ok=%s tokens=%s+%s 缓存命中=%s 费用≈%s元 耗时 %dms",
            task_id,
            result.get("skill"),
            result.get("skillVersion"),
            result.get("protocolOk"),
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
            getattr(usage, "cache_hit_tokens", 0),
            cost.get("costCNY"),
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
