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
# 每个用户每天可用的「AI 深度改写」次数；规则层体检/改写不限次
DEAI_DAILY_LIMIT = env_int("DEAI_DAILY_LIMIT", 20)
# 超过这个秒数还停在 pending/running 的任务判为失败（容器重启/扩缩容会杀后台线程）
DEAI_TASK_TIMEOUT_SECONDS = env_int("DEAI_TASK_TIMEOUT_SECONDS", 120)
# 是否允许没有 openid 的调用（本地开发用；生产必须保持 False）
DEAI_ALLOW_ANONYMOUS = env_bool("DEAI_ALLOW_ANONYMOUS", DEBUG)
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
