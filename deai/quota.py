"""每日免费额度。

规则层（体检 + 规则改写）**不计次**，因为它不花钱、毫秒级返回；
只有调用大模型的「AI 深度改写」才消耗额度。
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import QuotaUsage


class QuotaExceeded(Exception):
    pass


def _today():
    """今天的日期。

    注意不要用 ``timezone.localdate()``：本项目 ``USE_TZ=False``，此时
    ``timezone.now()`` 返回 naive datetime，而 ``localdate()`` 内部会对它调用
    ``localtime()``，直接抛 ``ValueError: localtime() cannot be applied to a
    naive datetime``。容器时区已在 Dockerfile 里设为 Asia/Shanghai，
    所以 ``now().date()`` 就是正确的「今天」。
    """
    return timezone.now().date()


def get_quota(openid: str) -> dict:
    limit = settings.DEAI_DAILY_LIMIT
    row = QuotaUsage.objects.filter(openid=openid, day=_today()).first()
    used = row.used if row else 0
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}


def consume(openid: str) -> dict:
    """消耗一次额度。超额时抛 ``QuotaExceeded``。"""
    limit = settings.DEAI_DAILY_LIMIT
    row, _created = QuotaUsage.objects.get_or_create(
        openid=openid, day=_today(), defaults={"used": 0}
    )
    if row.used >= limit:
        raise QuotaExceeded(f"今天的 {limit} 次 AI 改写额度用完了，明天恢复")

    # 用 F() 做原子自增，避免并发下的丢更新
    QuotaUsage.objects.filter(pk=row.pk).update(used=F("used") + 1)
    used = row.used + 1
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}
