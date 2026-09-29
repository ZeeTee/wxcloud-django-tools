"""数据模型。

任务必须落库，原因有两个：
1. ``wx.cloud.callContainer`` 单次请求上限 **15 秒**，而 LLM 改写要 10-60 秒，
   所以只能「创建任务 → 轮询结果」；
2. 轮询请求可能落到不同副本，任务状态不能只放在进程内存里。
"""

from __future__ import annotations

from django.db import models


class RewriteTask(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    id = models.CharField(primary_key=True, max_length=40)
    openid = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=16, default=STATUS_PENDING, db_index=True)
    mode = models.CharField(max_length=16, default="general")
    source_text = models.TextField()
    rules_text = models.TextField(blank=True, default="")
    llm_text = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    model_name = models.CharField(max_length=64, blank=True, default="")
    warnings = models.TextField(blank=True, default="")  # JSON 数组字符串
    elapsed_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deai_rewrite_task"
        # 显式命名索引：不写 name 时 Django 会生成带哈希的自动名，
        # 与手写迁移对不上，makemigrations --check 会报「需要重命名」。
        indexes = [
            models.Index(fields=["openid", "created_at"], name="deai_task_openid_created_idx")
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<RewriteTask {self.id} {self.status}>"


class QuotaUsage(models.Model):
    """按天统计的免费额度。规则层不计次，只有 AI 深度改写才消耗。"""

    openid = models.CharField(max_length=64)
    day = models.DateField()
    used = models.IntegerField(default=0)

    class Meta:
        db_table = "deai_quota_usage"
        unique_together = ("openid", "day")

    def __str__(self) -> str:  # pragma: no cover
        return f"<QuotaUsage {self.openid} {self.day} {self.used}>"
