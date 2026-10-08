"""每日使用次数：**按 openid 计**，每人每天 10 次。

规则层（体检 + 规则改写）**不计次**，因为它不花钱、毫秒级返回；
只有调用大模型的「AI 深度改写」才消耗次数。

为什么按 openid 记在后端
------------------------
``X-WX-OPENID`` 是云托管**自动注入**的，前端不需要做任何事、也伪造不了。
有人会想「就用前端本地存储计数」——但那样用户**清一次小程序缓存次数就归零**，
等于没有限制，而且前端数据可被篡改。记在后端，清缓存重置不了
（要换微信号才行），成本只是一次数据库查询。

为什么不分档
------------
这里曾经分「未授权 5 次 / 授权手机号后 10 次」两档，靠手机号授权区分。
现在改成**只要拿到 openid 就是 10 次**：手机号授权要花钱（0.03 元/次）、
还有主体资质门槛（个人主体用不了），为了一次额度差异付这些成本不划算。

次数怎么重置
------------
``QuotaUsage`` 按 ``(openid, 日期)`` 记一行，日期取**北京时间**当天，
所以北京时间 0 点一过自然是新的一行、从 0 开始。不需要定时任务，
也不怕漏跑任务——这是「按天分桶」相比「定时清零」的好处。
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import QuotaUsage, UserProfile


class QuotaExceeded(Exception):
    pass


def _today():
    """今天的日期（北京时间）。

    注意不要用 ``timezone.localdate()``：本项目 ``USE_TZ=False``，此时
    ``timezone.now()`` 返回 naive datetime，而 ``localdate()`` 内部会对它调用
    ``localtime()``，直接抛 ``ValueError: localtime() cannot be applied to a
    naive datetime``。容器时区已在 Dockerfile 里设为 Asia/Shanghai，
    所以 ``now().date()`` 就是正确的「今天」。
    """
    return timezone.now().date()


def resets_at():
    """下一次重置的时刻（北京时间 0 点，naive datetime）。"""
    now = timezone.now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def daily_limit() -> int:
    """每人每天的改写次数上限。

    变量叫 ``DEAI_DAILY_QUOTA`` 而不是 ``DEAI_DAILY_LIMIT``：后者是早期那个
    「一配就同时覆盖两档」的变量，已彻底废弃。**刻意不复用这个名字**——
    老部署的环境变量里可能还留着 ``DEAI_DAILY_LIMIT=20``，复用的话它会
    悄无声息地又生效一次，正是我们刚花力气铲掉的坑。
    """
    return settings.DEAI_DAILY_QUOTA


def is_verified(openid: str) -> bool:
    """该用户是否做过手机号授权。

    注意：**它不再影响次数**（现在是统一 10 次）。保留它是因为
    ``/api/quota`` 要回显「已授权 138****8000」这类用户状态。
    """
    return UserProfile.objects.filter(openid=openid, verified_at__isnull=False).exists()


def mark_verified(
    openid: str,
    session_key: str = "",
    *,
    phone_masked: str = "",
    phone_hash: str = "",
    method: str = "phone",
) -> None:
    """记录一次手机号授权。幂等，可以重复调用。

    手机号字段只在传了值时才覆盖，避免旧的 code2Session 登录把已授权的
    手机号抹掉。
    """
    defaults = {"verified_at": timezone.now(), "login_method": method}
    if session_key:
        defaults["session_key"] = session_key
    if phone_masked:
        defaults["phone_masked"] = phone_masked
    if phone_hash:
        defaults["phone_hash"] = phone_hash
    UserProfile.objects.update_or_create(openid=openid, defaults=defaults)


def get_quota(openid: str) -> dict:
    """查询当日剩余次数。前端据此显示「今日还剩 N / M 次」。"""
    limit = daily_limit()
    row = QuotaUsage.objects.filter(openid=openid, day=_today()).first()
    used = row.used if row else 0

    # 顺手把用户状态带出去（一次查询），前端可以显示「已授权 138****8000」
    profile = UserProfile.objects.filter(openid=openid).first()

    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        # 北京时间下一次归零的时刻。前端显示「明天 N 点恢复」用得上
        "resetsAt": resets_at().strftime("%Y-%m-%d %H:%M:%S"),
        "verified": bool(profile and profile.verified_at),
        "phoneMasked": (profile.phone_masked if profile else "") or "",
    }


def consume(openid: str) -> dict:
    """消耗一次次数。超额时抛 ``QuotaExceeded``。

    比 ``get_quota`` 少查一次用户表：改写接口不需要回显手机号，
    这里只关心计数。
    """
    limit = daily_limit()
    row, _created = QuotaUsage.objects.get_or_create(
        openid=openid, day=_today(), defaults={"used": 0}
    )
    if row.used >= limit:
        raise QuotaExceeded(
            f"今天的 {limit} 次 AI 改写额度用完了，明天 0 点自动恢复"
        )

    # 用 F() 做原子自增，避免并发下的丢更新
    QuotaUsage.objects.filter(pk=row.pk).update(used=F("used") + 1)
    used = row.used + 1
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "resetsAt": resets_at().strftime("%Y-%m-%d %H:%M:%S"),
    }


__all__ = [
    "QuotaExceeded",
    "daily_limit",
    "resets_at",
    "is_verified",
    "mark_verified",
    "get_quota",
    "consume",
]
