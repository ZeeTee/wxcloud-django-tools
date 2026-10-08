"""每日免费额度，分「未授权 / 已授权手机号」两档。

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
openid 未登录就有，所以匿名和登录**在身份上没有区别**；``wx.login()`` 换 openid
也证明不了任何东西（谁都能触发，微信不做校验）。要区分两档额度，必须有
一个用户主动做过、且微信背书的动作——见 ``deai/wechat.py`` 的手机号授权。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import QuotaUsage, UserProfile

logger = logging.getLogger(__name__)


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
    """返回 ``(匿名上限, 已授权上限)``。

    旧的 ``DEAI_DAILY_LIMIT`` 若被显式配置（>0），则两档都用它——
    这样老部署升级上来行为不变，不会突然把额度从 20 砍到 5。

    ⚠️ 这个覆盖是**静默**的：配了 ``DEAI_DAILY_LIMIT_ANONYMOUS=5`` 但忘了删
    旧变量时，``/api/quota`` 会返回 20，光看接口只会觉得「配置没生效」。
    所以启动时会用 ``warn_legacy_override()`` 把它喊出来。
    """
    legacy = settings.DEAI_DAILY_LIMIT
    if legacy > 0:
        return legacy, legacy
    return settings.DEAI_DAILY_LIMIT_ANONYMOUS, settings.DEAI_DAILY_LIMIT_VERIFIED


def legacy_override() -> dict | None:
    """旧变量是否正在覆盖两档额度。

    :returns: 没覆盖返回 ``None``；覆盖时返回 ``{legacy, anonymous, verified}``，
              其中 ``anonymous`` / ``verified`` 是**被忽略掉**的两个配置值。
    """
    legacy = settings.DEAI_DAILY_LIMIT
    if legacy <= 0:
        return None
    return {
        "legacy": legacy,
        "anonymous": settings.DEAI_DAILY_LIMIT_ANONYMOUS,
        "verified": settings.DEAI_DAILY_LIMIT_VERIFIED,
    }


_legacy_warned = False


def warn_legacy_override() -> None:
    """启动时把「旧变量正在覆盖两档额度」喊进日志（全进程只喊一次）。

    Django 的 ``AppConfig.ready()`` 会调它，所以容器一起来就能在服务日志里看到，
    不用等到有人去调 ``/api/quota`` 才发现限额不对。
    """
    global _legacy_warned
    if _legacy_warned:
        return
    info = legacy_override()
    if info is None:
        return
    _legacy_warned = True
    logger.warning(
        "检测到旧环境变量 DEAI_DAILY_LIMIT=%s：未授权/已授权两档额度都被它覆盖成 %s 次，"
        "而 DEAI_DAILY_LIMIT_ANONYMOUS=%s 与 DEAI_DAILY_LIMIT_VERIFIED=%s 当前**不生效**。"
        "想用两档额度，请到「服务设置 → 环境变量」把 DEAI_DAILY_LIMIT 删掉。",
        info["legacy"],
        info["legacy"],
        info["anonymous"],
        info["verified"],
    )


def limits() -> tuple[int, int]:
    """生效的 ``(匿名上限, 已授权上限)``。给 views 做部署自检展示用。"""
    return _limits()


def is_verified(openid: str) -> bool:
    """该用户是否通过手机号授权验证过（决定走哪一档额度）。"""
    return UserProfile.objects.filter(openid=openid, verified_at__isnull=False).exists()


def mark_verified(
    openid: str,
    session_key: str = "",
    *,
    phone_masked: str = "",
    phone_hash: str = "",
    method: str = "phone",
) -> None:
    """把用户标记为已通过验证。幂等，可以重复调用。

    ``verified_at`` 一旦写上就只更新、不清空——用户已经拿到的额度不该被收回。
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
    """查询当日额度。前端据此显示「还能用几次 / 授权手机号可提升到 N 次」。"""
    anonymous_limit, verified_limit = _limits()
    # 一次查询同时拿「是否已验证」和「打码手机号」，不要分两次查
    profile = UserProfile.objects.filter(openid=openid).first()
    verified = bool(profile and profile.verified_at)
    limit = verified_limit if verified else anonymous_limit
    row = QuotaUsage.objects.filter(openid=openid, day=_today()).first()
    used = row.used if row else 0
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        # 下面几个字段给前端做引导用：未授权时提示「授权后每天 N 次」
        "verified": verified,
        "anonymousLimit": anonymous_limit,
        "verifiedLimit": verified_limit,
        # 已授权时前端可以显示「已授权 138****8000」
        "phoneMasked": (profile.phone_masked if profile else "") or "",
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
        # 未验证手机号时把「授权能提升额度」写进错误里——这正是此刻最该给的引导
        raise QuotaExceeded(
            f"今天的 {anonymous_limit} 次免授权额度用完了。"
            f"授权手机号后每天可用 {verified_limit} 次，明天也会自动恢复"
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
