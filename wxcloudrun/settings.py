"""Django 配置 —— 微信云托管 + 去 AI 味。

基于官方模板（WeixinCloud/wxcloudrun-django）改造，**保留原有的计数器示例与主页**，
新增 ``deai`` 应用提供去 AI 味接口。

模板原有的四个坑在这里修掉了：

1. ``os.environ.get("MYSQL_ADDRESS").split(':')`` —— 环境变量缺失时直接
   ``AttributeError``，本地根本起不来。现在缺失则回落到 SQLite，本地开箱即用。
2. ``SECRET_KEY`` 硬编码、``DEBUG = True`` —— 改为环境变量驱动，默认生产安全值。
3. 日志写 ``logs/*.log`` 文件 —— 云托管容器重启即丢，而平台默认采集 stdout，
   控制台里根本看不到。现在统一打 stdout。
4. ``TIME_ZONE = 'UTC'`` —— 容器时区已设为 Asia/Shanghai，这里对齐，
   否则日志时间差 8 小时。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# --- 基础 -------------------------------------------------------------------

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or "dev-only-insecure-key-please-change"
DEBUG = env_bool("DJANGO_DEBUG", False)

# DEBUG=False 时不能为空，否则所有请求 400。云托管网关的 Host 不固定，默认放开。
ALLOWED_HOSTS = [
    h.strip() for h in (os.environ.get("DJANGO_ALLOWED_HOSTS") or "*").split(",") if h.strip()
]

INSTALLED_APPS = [
    # 模板原有
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "wxcloudrun",
    # 新增：去 AI 味
    "deai",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # 保持模板的做法：小程序 callContainer 不带 cookie，纯 API 场景不需要 CSRF；
    # 注意本项目没有对外暴露 admin 的 URL，所以关掉它不会引入后台被 CSRF 的风险。
    # 'django.middleware.csrf.CsrfViewMiddleware',
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "wxcloudrun.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "wxcloudrun.wsgi.application"

# 接口路径不带尾斜杠，关掉自动补斜杠的 301，免得 callContainer 遇到重定向行为难排查
APPEND_SLASH = False

# --- 数据库 -----------------------------------------------------------------
# 云托管会注入 MYSQL_ADDRESS（形如 "10.0.0.1:3306"）。
# 没配就回落到 SQLite —— 本地开发不用先装 MySQL。

MYSQL_ADDRESS = (os.environ.get("MYSQL_ADDRESS") or "").strip()

if MYSQL_ADDRESS:
    _host, _, _port = MYSQL_ADDRESS.partition(":")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE") or "django_demo",
            # 模板用 MYSQL_USERNAME，这里兼容两种命名
            "USER": os.environ.get("MYSQL_USERNAME") or os.environ.get("MYSQL_USER") or "root",
            "PASSWORD": os.environ.get("MYSQL_PASSWORD") or "",
            "HOST": _host,
            "PORT": _port or "3306",
            "OPTIONS": {"charset": "utf8mb4"},
            "CONN_MAX_AGE": 60,
        }
    }
else:
    _sqlite_path = Path(os.environ.get("SQLITE_PATH") or (BASE_DIR / "data" / "deai.sqlite3"))
    _sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(_sqlite_path),
            "OPTIONS": {"timeout": 20},
        }
    }

# --- 认证 -------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- 国际化 -----------------------------------------------------------------

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = False  # 与模板一致；容器时区已在 Dockerfile 里设为 Asia/Shanghai

# --- 静态文件 ---------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- 反向代理 ---------------------------------------------------------------

# HTTPS 在云托管网关上终止，容器内是 HTTP，要告诉 Django 原始协议
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 模板遗留变量，保留以免外部脚本引用时报错
LOGS_DIR = "/data/logs/"

# --- 去 AI 味业务配置 -------------------------------------------------------

# 单次输入上限（字）
DEAI_MAX_INPUT_CHARS = env_int("DEAI_MAX_INPUT_CHARS", 5000)
# 每个用户每天可用的「AI 深度改写」次数，分两档：
# 未登录（匿名）与已登录。规则层体检/改写不限次、不消耗额度。
# 注意：匿名身份用的是云托管自动注入的 X-WX-OPENID，不是前端本地存储——
# 存本地的话用户清一次缓存就重置了，等于没有限制。
DEAI_DAILY_LIMIT_ANONYMOUS = env_int("DEAI_DAILY_LIMIT_ANONYMOUS", 5)
DEAI_DAILY_LIMIT_VERIFIED = env_int("DEAI_DAILY_LIMIT_VERIFIED", 10)
# 旧变量，仅作为两个新变量的兜底（老部署只配了它时仍能跑）
DEAI_DAILY_LIMIT = env_int("DEAI_DAILY_LIMIT", 0)

# --- 微信小程序身份验证（手机号授权 -> getPhoneNumber）----------------------
# 用于「授权手机号后提升额度」。两种调用方式，见 deai/wechat.py 的模块说明：
#
#   1. 云调用（默认，推荐）：容器内用 HTTP 直接请求 api.weixin.qq.com，不带
#      access_token，由旁加载的「开放接口服务」自动注入 cloudbase_access_token。
#      需要控制台-云调用打开开关、把路径加进白名单，**并在打开开关后重新构建版本**。
#      这种模式下连 AppSecret 都不需要。
#   2. 自管 access_token（兜底）：WX_OPENAPI_ENABLED=False 时启用，
#      用 AppID + AppSecret 换 token，token 存在数据库里给多副本共享。
#
# WX_APPID 无论哪种模式都建议配上：手机号回包里有 watermark.appid，
# 配上才能校验「这个手机号确实属于本小程序」。不配则跳过校验（会打 warning）。
WX_APPID = (os.environ.get("WX_APPID") or "").strip()
# 仅「自管 access_token」和旧的 code2Session 需要
WX_SECRET = (os.environ.get("WX_SECRET") or "").strip()
WX_LOGIN_TIMEOUT = env_int("WX_LOGIN_TIMEOUT", 10)
# 是否启用「开放接口服务」（云调用）。默认开，因为本项目就跑在云托管上。
WX_OPENAPI_ENABLED = env_bool("WX_OPENAPI_ENABLED", True)
# 云调用必须走 HTTP：容器内 api.weixin.qq.com 会解析到 Docker 内部地址，
# 由开放接口服务中转。走 HTTPS 需要额外信任 /app/cert/certificate.crt，
# 官方也明确建议用 HTTP 以获得更好性能。
WX_OPENAPI_BASE = (
    os.environ.get("WX_OPENAPI_BASE") or "http://api.weixin.qq.com"
).rstrip("/")
# 超过这个秒数还停在 pending/running 的任务判为失败（容器重启/扩缩容会杀后台线程）
DEAI_TASK_TIMEOUT_SECONDS = env_int("DEAI_TASK_TIMEOUT_SECONDS", 120)
# 是否允许没有 openid 的调用（本地开发用；生产必须保持 False）
DEAI_ALLOW_ANONYMOUS = env_bool("DEAI_ALLOW_ANONYMOUS", DEBUG)
# 诊断接口 /api/debug/headers：回显云托管注入的身份头。
# 默认只在 DEBUG 下开。线上排查「X-WX-OPENID 到底有没有被注入」时可以临时设 true，
# **查完立刻关掉**——它会回显 openid，属于敏感信息。
DEAI_DEBUG_HEADERS = env_bool("DEAI_DEBUG_HEADERS", DEBUG)
# 后台改写线程池并发数
DEAI_WORKERS = env_int("DEAI_WORKERS", 4)
# 「混合模式」：创建任务后同步等待多久，超时才转成前端轮询。
# 实测一次改写只要 0.5-2.3 秒，12 秒足以覆盖绝大多数请求，
# 同时给 callContainer 的 15 秒硬上限留 3 秒余量。设 0 退回纯异步。
DEAI_SYNC_WAIT_SECONDS = env_float("DEAI_SYNC_WAIT_SECONDS", 12.0)

# --- 日志：一律打 stdout，云托管默认采集 stdout ------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] [%(levelname)s] [%(module)s:%(funcName)s] %(message)s"
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {
        # 模板里业务代码用的是 logging.getLogger('log')，保留这个名字以免改动原有调用
        "log": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "deai": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
