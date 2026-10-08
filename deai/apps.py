from __future__ import annotations

from django.apps import AppConfig


class DeaiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "deai"
    verbose_name = "去 AI 味"

    def ready(self) -> None:
        # 旧环境变量 DEAI_DAILY_LIMIT 会**静默**覆盖两档额度，启动时就喊出来。
        # 放在 ready() 而不是等第一次请求：容器一起来日志里就有，
        # 不用等用户发现限额不对再回头查。
        from . import quota

        quota.warn_legacy_override()
