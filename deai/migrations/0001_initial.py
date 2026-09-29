"""初始迁移。

手写而非 makemigrations 生成（开发环境无法安装 Django），格式与 Django 生成的
完全一致。若你本地能跑 Django，用 ``python manage.py makemigrations deai``
重新生成也可以，结果等价。
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name="RewriteTask",
            fields=[
                ("id", models.CharField(max_length=40, primary_key=True, serialize=False)),
                ("openid", models.CharField(db_index=True, max_length=64)),
                ("status", models.CharField(db_index=True, default="pending", max_length=16)),
                ("mode", models.CharField(default="general", max_length=16)),
                ("source_text", models.TextField()),
                ("rules_text", models.TextField(blank=True, default="")),
                ("llm_text", models.TextField(blank=True, default="")),
                ("error", models.TextField(blank=True, default="")),
                ("model_name", models.CharField(blank=True, default="", max_length=64)),
                ("warnings", models.TextField(blank=True, default="")),
                ("elapsed_ms", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "deai_rewrite_task"},
        ),
        migrations.CreateModel(
            name="QuotaUsage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("openid", models.CharField(max_length=64)),
                ("day", models.DateField()),
                ("used", models.IntegerField(default=0)),
            ],
            options={
                "db_table": "deai_quota_usage",
                "unique_together": {("openid", "day")},
            },
        ),
        migrations.AddIndex(
            model_name="rewritetask",
            index=models.Index(fields=["openid", "created_at"], name="deai_task_openid_created_idx"),
        ),
    ]
