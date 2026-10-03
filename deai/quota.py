"""每日免费额度，分「未登录 / 已登录」两档。

规则层（体检 + 规则改写）**不计次**，因为它不花钱、毫秒级返回；
只有调用大模型的「AI 深度改写」才消耗额度。

为什么匿名也按 openid 记在后端
------------------------------
``X-WX-OPENID`` 是云托管**自动注入**的，前端不需要做任何事，未登录也能拿到。
有人会想「未登录就用前端本地存储计数」——但那样用户**清一次小程序缓存次数就归零**，
等于没有限制，而且前端数据可被篡改。用 openid 记在后端，清缓存重置不了
（要换微信号才行），成本只是一次数据库查询。

「已登录」为什么需要额外动作
----------------------------
openid 未登录就有，所以匿名和登录**在身份上没有区别**。要区分两档额度，
必须有一个用户主动做过的、可验证的动作——见 ``deai/wechat.py`` 的 code2Session。
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import QuotaUsage, UserProfile


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


def _limits() -> tuple[int, int]:
    """返回 ``(匿名上限, 已登录上限)``。

    旧的 ``DEAI_DAILY_LIMIT`` 若被显式配置（>0），则两档都用它——
    这样老部署升级上来行为不变，不会突然把额度从 20 砍到 5。
    """
    legacy = settings.DEAI_DAILY_LIMIT
    if legacy > 0:
        return legacy, legacy
    return settings.DEAI_DAILY_LIMIT_ANONYMOUS, settings.DEAI_DAILY_LIMIT_VERIFIED


def is_verified(openid: str) -> bool:
    """该用户是否主动登录过（决定走哪一档额度）。"""
    return UserProfile.objects.filter(openid=openid, verified_at__isnull=False).exists()


def mark_verified(openid: str, session_key: str = "") -> None:
    """把用户标记为已登录。幂等，可以重复调用（会刷新 session_key）。"""
    UserProfile.objects.update_or_create(
        openid=openid,
        defaults={"session_key": session_key, "verified_at": timezone.now()},
    )


def get_quota(openid: str) -> dict:
    """查询当日额度。前端据此显示「还能用几次 / 登录可提升到 N 次」。"""
    anonymous_limit, verified_limit = _limits()
    verified = is_verified(openid)
    limit = verified_limit if verified else anonymous_limit
    row = QuotaUsage.objects.filter(openid=openid, day=_today()).first()
    used = row.used if row else 0
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        # 下面三个字段给前端做引导用：未登录时提示「登录后每天 N 次」
        "verified": verified,
        "anonymousLimit": anonymous_limit,
        "verifiedLimit": verified_limit,
    }


def consume(openid: str) -> dict:
    """消耗一次额度。超额时抛 ``QuotaExceeded``。"""
    anonymous_limit, verified_limit = _limits()
    verified = is_verified(openid)
    limit = verified_limit if verified else anonymous_limit

    row, _created = QuotaUsage.objects.get_or_create(
        openid=openid, day=_today(), defaults={"used": 0}
    )
    if row.used >= limit:
        if verified:
            raise QuotaExceeded(f"今天的 {limit} 次 AI 改写额度用完了，明天恢复")
        # 未登录时把「登录能提升额度」写进错误里——这正是此刻最该给的引导
        raise QuotaExceeded(
            f"今天的 {anonymous_limit} 次免登录额度用完了。"
            f"登录后每天可用 {verified_limit} 次，明天也会自动恢复"
        )

    # 用 F() 做原子自增，避免并发下的丢更新
    QuotaUsage.objects.filter(pk=row.pk).update(used=F("used") + 1)
    used = row.used + 1
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "verified": verified,
        "anonymousLimit": anonymous_limit,
        "verifiedLimit": verified_limit,
    }


__all__ = [
    "QuotaExceeded",
    "is_verified",
    "mark_verified",
    "get_quota",
    "consume",
]
