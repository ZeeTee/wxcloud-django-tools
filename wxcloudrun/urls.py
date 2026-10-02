"""URL 配置。

- ``/api/health`` ``/api/analyze`` ``/api/rewrite`` ``/api/task/<id>`` ``/api/quota``
  —— 去 AI 味接口，实现在 ``deai/views.py``
- ``/api/count`` —— 模板自带的计数器示例，行为保持不变
- ``/`` —— 模板自带的主页

原模板的两处问题在这里修掉了：

1. ``from django.conf.urls import url`` —— 该 API 在 Django 4.x 已被**移除**，
   直接 import 失败。改用 ``re_path`` / ``path``。
2. ``url(r'^^api/count(/)?$', ...)`` —— 正则开头写成了**两个 ``^``**，
   导致 ``/api/count`` 永远匹配不上，请求会掉进下面的 catch-all 返回主页 HTML。
   现在改成一个 ``^``。
"""

from __future__ import annotations

from django.http import JsonResponse
from django.urls import path, re_path

from deai import views as deai_views
from wxcloudrun import views as site_views

urlpatterns = [
    # ---------- 去 AI 味（必须排在 catch-all 之前）----------
    path("api/health", deai_views.health, name="deai-health"),
    path("api/analyze", deai_views.analyze, name="deai-analyze"),
    path("api/rewrite", deai_views.rewrite, name="deai-rewrite"),
    path("api/task/<str:task_id>", deai_views.task_status, name="deai-task-status"),
    path("api/quota", deai_views.quota, name="deai-quota"),
    path("api/feedback", deai_views.feedback, name="deai-feedback"),
    path("api/feedback/summary", deai_views.feedback_summary, name="deai-feedback-summary"),
    path("api/skills", deai_views.skills, name="deai-skills"),
    path("api/usage", deai_views.usage, name="deai-usage"),

    # ---------- 模板原有：计数器示例 ----------
    # 注意 views.counter 的签名是 (request, _)，这里的捕获组会被当作第二个位置参数
    re_path(r"^api/count(/)?$", site_views.counter),

    # ---------- 模板原有：主页（catch-all，保持原行为）----------
    # 原模板用的是没有 ^ 锚定的 r'(/)?$'，会兜住所有未匹配路径，这里原样保留
    re_path(r"(/)?$", site_views.index),
]


def _not_found(request, *_args, **_kwargs):
    return JsonResponse(
        {"ok": False, "error": {"code": "NOT_FOUND", "message": "接口不存在"}},
        status=404,
        json_dumps_params={"ensure_ascii": False},
    )


def _server_error(request, *_args, **_kwargs):
    # 状态码要和 404 区分开，否则线上排查会被误导
    return JsonResponse(
        {"ok": False, "error": {"code": "INTERNAL", "message": "服务端出错了，请稍后重试"}},
        status=500,
        json_dumps_params={"ensure_ascii": False},
    )


handler404 = _not_found
handler500 = _server_error
