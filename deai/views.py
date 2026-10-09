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
from . import wechat
from .auth import ANONYMOUS, AuthError, get_identity
from .engine import build_report, is_configured, rewrite_by_rules
from django.db.models import Count, Sum
from django.utils import timezone

from .engine.llm import LLMError as LLMBalanceError
from .engine.llm import get_balance, provider_info
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
        # 两条路径都要带 taskId：前端「切后台后回来续跑」时只调轮询接口，
        # 拿不到 id 就没法把结果落回同一条历史记录。
        "taskId": task.id,
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
        payload["provider"] = task.provider
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
            # costCNY 的来源：provider = 供应商上报（可信）；
            # local_table = 按 LLM_PRICE_* 本地估算（仅供参考）。
            # 空串 = 老数据。前端想标「估算值」就靠它。
            "costSource": task.cost_source or "",
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
            # 当前用的是哪个 provider / 模型，以及可选项——换供应商时不用猜
            "llm": provider_info(),
            "defaultSkill": DEFAULT_SKILL,
            "skills": [s.slug for s in registry.list()] + list(EXTRA_SKILLS),
            "promptFingerprints": fingerprints,
            # 手机号授权能不能用。注意它**不再影响使用次数**（统一 10 次），
            # 保留这个探针只是为了别的场景要用手机号时能提前知道能不能调。
            "phoneAuthReady": wechat.phone_ready(),
            "phoneAuthMode": "cloudcall" if settings.WX_OPENAPI_ENABLED else "token",
            # 每人每天的使用次数上限（按 openid 计，北京时间 0 点重置）
            "quotaLimit": quota_service.daily_limit(),
        }
    )


@api("GET")
def debug_headers(request):
    """诊断：回显云托管注入的身份头。默认关闭（``DEAI_DEBUG_HEADERS``）。

    **为什么需要它**：``X-WX-OPENID`` 这类头是**平台在服务端注入**的，客户端
    根本不发送，所以微信开发者工具的 Network 面板里永远看不到。要确认
    「到底有没有注入、值是什么」，只能从容器侧看——这个接口就是那扇窗。

    它**故意不做身份校验**：身份解析失败时（比如头根本没注入）更要把现场
    打出来，这时候要是先被 401 拦住就什么都看不到了。

    ⚠️ 它会回显 openid，属于敏感信息。只在排查时临时开，查完立刻关。
    """
    if not settings.DEAI_DEBUG_HEADERS:
        # 关着的时候按「接口不存在」处理，不对外暴露它的存在
        return fail("NOT_FOUND", "接口不存在", 404)

    known = (
        "X-WX-OPENID",
        "X-WX-FROM-OPENID",
        "X-WX-APPID",
        "X-WX-FROM-APPID",
        "X-WX-UNIONID",
        "X-WX-FROM-UNIONID",
        "X-WX-ENV",
        "X-WX-SOURCE",
        "X-Original-Forwarded-For",
        "X-Forwarded-For",
        "User-Agent",
    )
    headers = {}
    for name in known:
        value = request.headers.get(name)
        if value is not None:
            headers[name] = value

    # 有些框架/中间件会把 header 转成下划线形式，一并列出来
    # 便于排查「平台明明注入了，Django 却读不到」
    meta = {
        k: v
        for k, v in request.META.items()
        if k.startswith(("HTTP_X_WX", "HTTP_X_ORIGINAL", "HTTP_X_FORWARDED"))
    }

    try:
        identity = get_identity(request)
    except AuthError as exc:
        identity = f"<解析失败：{exc}>"

    return ok(
        {
            # 业务侧最终认到的身份，是这几个头综合出来的结果
            "identity": identity,
            "hasOpenid": "X-WX-OPENID" in headers,
            "hasFromOpenid": "X-WX-FROM-OPENID" in headers,
            "hasSource": "X-WX-SOURCE" in headers,
            "headers": headers,
            "metaKeys": sorted(meta.keys()),
            "allowAnonymous": settings.DEAI_ALLOW_ANONYMOUS,
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

    两道用量闸，**先频率后总量**：

    1. ``RATE_LIMITED``（429）—— ``DEAI_REWRITE_RATE_LIMIT`` 次 /
       ``DEAI_REWRITE_RATE_WINDOW_SECONDS`` 秒的滑动窗口，按 openid 计；
    2. ``QUOTA_EXCEEDED``（429）—— 每天的总次数。

    顺序是刻意的：被频率限制挡下的请求**不扣**每日次数。
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

    # 先看频率再看总量：被限流的请求不该白扣一次每日次数。
    # 放在参数校验之后，所以填错参数重试不会消耗限流预算。
    try:
        quota_service.check_rewrite_rate(identity)
    except quota_service.RateLimited as exc:
        return fail("RATE_LIMITED", str(exc), 429)

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
    """查询今日剩余次数。

    返回 ``used`` / ``limit`` / ``remaining`` / ``resetsAt``，外加用户状态
    ``verified`` / ``phoneMasked``。**次数按 openid 计，每人每天固定 N 次**
    （``DEAI_DAILY_QUOTA``，默认 10），不分档、与手机号授权无关。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err
    return ok(quota_service.get_quota(identity))


@api("POST")
def login(request):
    """手机号授权。

    ⚠️ **它不再影响使用次数**。次数已经统一成「拿到 openid 就每天 10 次」，
    手机号授权现在只是记录一下用户手机号，供后续功能使用。

    保留这个接口的原因：手机号是唯一一个「用户主动做过、且微信背书」的
    身份动作（openid 是云托管无条件注入的，``wx.login()`` 谁都能触发、
    微信也不校验）。以后要做真正的账号体系、跨端识别、或者防刷时，
    它还是那个可用的抓手，所以没删。

    前端怎么调
    ----------
    按钮上加 ``open-type="getPhoneNumber"``，拿到回调里的
    ``e.detail.code``（新版手机号快速验证是 code，不再是加密数据），
    POST 给这个接口::

        { "phoneCode": "e.detail.code" }

    安全要点
    --------
    1. ``getuserphonenumber`` 回包里**没有 openid**，所以身份只能取请求头里的
       ``X-WX-OPENID``（云托管注入，前端改不了）。code 本身是一次性、几分钟
       过期的，且由当前小程序会话下发，无法跨用户盗用。
    2. 校验回包中的 ``watermark.appid`` 必须是本小程序（见 ``wechat.py``）。
    3. 云调用模式下请求不带 access_token，不存在 token 泄露面。

    兼容
    ----
    仍接受老的 ``{"code": "<wx.login 的 code>"}``（code2Session 路径），
    但它只是弱验证、且开启「开放接口服务」后会失败，仅用于平滑过渡。
    """
    identity, err = _identity_or_error(request)
    if err:
        return err
    data, err = _parse_body(request)
    if err:
        return err

    phone_code = str(data.get("phoneCode") or "").strip()
    legacy_code = str(data.get("code") or "").strip()

    if phone_code:
        return _login_by_phone(identity, phone_code)
    if legacy_code:
        return _login_by_code2session(identity, legacy_code)
    return fail("CODE_EMPTY", "缺少手机号授权凭证 phoneCode", 400)


def _login_by_phone(identity: str, phone_code: str):
    """手机号授权路径。只记录手机号，不影响使用次数。"""
    if not wechat.phone_ready():
        return fail(
            "WX_NOT_CONFIGURED",
            "服务端还没配置微信 AppID / AppSecret，暂时无法授权手机号",
            503,
        )

    try:
        info = wechat.get_phone_number(phone_code)
    except wechat.WeChatError as exc:
        logger.warning("手机号授权失败：%s", exc)
        return fail("WX_PHONE_FAILED", str(exc), 400)

    if identity == ANONYMOUS:
        # 本地开发没有云托管注入的身份头，链路本身走不通（无法确定手机号记给谁）。
        # 生产环境不存在这种情况。
        logger.warning("匿名模式下授权手机号，无法归属用户：%s", info["masked"])
        return fail("NO_IDENTITY", "当前环境拿不到用户身份，无法记录手机号", 400)

    quota_service.mark_verified(
        identity,
        phone_masked=info["masked"],
        phone_hash=info["fingerprint"],
        method="phone",
    )
    logger.info("用户 %s 手机号授权成功（%s）", identity, info["masked"])
    return ok(
        {
            "verified": True,
            "phoneMasked": info["masked"],
            "quota": quota_service.get_quota(identity),
        }
    )


def _login_by_code2session(identity: str, code: str):
    """[兼容] 老的 ``wx.login()`` 登录路径。

    code2Session 换回来的 openid 必须与请求头里的 ``X-WX-OPENID`` 一致——
    否则任何人都能拿别人的 code 往自己名下写记录。
    """
    logger.warning("使用了已废弃的 code2Session 登录路径，建议前端改为手机号授权")
    if not wechat.is_configured():
        return fail("WX_NOT_CONFIGURED", "服务端还没配置微信 AppID / AppSecret", 503)

    try:
        session = wechat.code2session(code)
    except wechat.WeChatError as exc:
        return fail("WX_LOGIN_FAILED", str(exc), 400)

    openid = session["openid"]
    if identity == ANONYMOUS:
        # 本地开发没有云托管注入的身份头，只能拿 code2Session 的结果当身份。
        # 注意：后续请求的 identity 仍是 "anonymous"——这是开发模式的固有限制，
        # 生产环境不存在。
        logger.warning("匿名模式下登录，用 code2Session 的 openid 标记：%s", openid)
    elif openid != identity:
        logger.warning(
            "登录 openid 不匹配：header=%s code2session=%s", identity, openid
        )
        return fail("OPENID_MISMATCH", "登录信息与当前用户不一致，请重新登录", 400)
    else:
        openid = identity

    quota_service.mark_verified(
        openid, session.get("session_key") or "", method="code2session"
    )
    logger.info("用户 %s 通过 code2Session 记录成功", openid)
    return ok({"verified": True, "quota": quota_service.get_quota(openid)})


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
