from __future__ import annotations

from django.apps import AppConfig


class DeaiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "deai"
    verbose_name = "去 AI 味"
