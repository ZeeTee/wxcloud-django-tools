"""为模板自带的 ``Counters`` 表补上迁移。

原模板把建表 SQL 放在 ``container.config.json`` 的 ``executeSQLs`` 里，而那**只在
「控制台一键模板部署」那一次生效**。于是有两条路径会踩空：自行新建服务上传代码包、
以及本地开发——这两种情况下 ``Counters`` 表都不存在，``/api/count`` 一调就报错。

补上这个迁移后，``python manage.py migrate`` 就能把表建出来。
配合启动命令里的 ``--fake-initial``：如果表已经存在（模板部署路径建的），
Django 会识别出来并直接 fake 掉这个初始迁移，不会报「表已存在」。
"""

from __future__ import annotations

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name="Counters",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("count", models.IntegerField(default=0)),
                ("createdAt", models.DateTimeField(default=django.utils.timezone.now)),
                ("updatedAt", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "Counters"},
        ),
    ]
