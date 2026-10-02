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
    # 改写强度：light（保守）/ medium（默认）/ heavy（彻底）
    intensity = models.CharField(max_length=16, default="medium")
    # 用哪个 skill 跑的（humanizer / legacy …）。记下来才能做事后归因：
    # 同一段文本换 skill 效果差多少、哪个 skill 的失败率高。
    skill = models.CharField(max_length=32, default="humanizer")
    skill_version = models.CharField(max_length=16, blank=True, default="")
    # 编译后 prompt 的短哈希。version 是手写的、可能忘记 bump，指纹不会。
    # 配合 Feedback 表就能回答「改了 prompt 之后好评率变了没」。
    prompt_fingerprint = models.CharField(max_length=16, blank=True, default="")
    source_text = models.TextField()
    rules_text = models.TextField(blank=True, default="")
    llm_text = models.TextField(blank=True, default="")
    # skill 模式要求模型额外输出检测报告，存下来可以在结果页展示
    llm_report = models.TextField(blank=True, default="")
    # 模型是否遵守了 <REPORT>/<REWRITTEN> 输出协议（容错解析成功时为 False）
    protocol_ok = models.BooleanField(default=True)
    # 模型自报「补充了哪些原文没有的内容」。存下来是因为这是本功能最大的信任风险：
    # 去 AI 味要求「具体化」，但硬约束是「不新增事实」，需要留给用户核对。
    llm_added_facts = models.TextField(blank=True, default="")
    # token 用量与估算费用：取自响应的 usage 字段——精确、随响应返回、零额外请求。
    # 不用「调用前后查两次余额算差值」：实测余额只有 2 位小数且更新滞后，
    # 单次调用（约 0.0007 元）的差值恒为 0。
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    cache_hit_tokens = models.IntegerField(default=0)
    cost_cny = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    error = models.TextField(blank=True, default="")
    model_name = models.CharField(max_length=64, blank=True, default="")
    # 实际调用的是哪个 provider（deepseek / openrouter）。换供应商时效果和费用
    # 都会变，落库才能在事后归因——尤其配合 Feedback 表看「换 provider 后好评率」
    provider = models.CharField(max_length=16, blank=True, default="")
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


class Feedback(models.Model):
    """用户对一次改写结果的评价。

    为什么重要：在没有自动评测的情况下，这是**唯一**能知道「改得好不好」的信号。
    没有它，改 prompt 只能靠手感，而且永远不知道线上真实效果。

    只存 task_id 不冗余 skill/scene/intensity——统计时 join 任务表即可，
    避免两处数据不一致。
    """

    RATING_GOOD = "good"
    RATING_BAD = "bad"
    RATING_CHOICES = ((RATING_GOOD, "满意"), (RATING_BAD, "不满意"))

    # 负面评价的原因标签。枚举而不是自由文本，是为了能统计出
    # 「哪类问题最多」——那才是改进 prompt 的依据。
    REASON_CHOICES = (
        "added_facts",  # 加了原文没有的内容
        "lost_info",  # 丢了原文的信息
        "not_natural",  # 还是很像 AI
        "changed_meaning",  # 意思被改了
        "too_casual",  # 改得太随意/口语
        "too_formal",  # 改得太正式
        "too_long",  # 变啰嗦了
        "too_short",  # 变短了、信息变少
        "other",
    )

    task_id = models.CharField(max_length=40, db_index=True)
    openid = models.CharField(max_length=64, db_index=True)
    rating = models.CharField(max_length=8, choices=RATING_CHOICES)
    reason = models.CharField(max_length=32, blank=True, default="")
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deai_feedback"
        # 一个用户对一个任务只保留一条：重复提交视为「改主意」，
        # 用 update_or_create 覆盖，而不是堆出多条自相矛盾的记录。
        unique_together = ("task_id", "openid")

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<Feedback {self.task_id} {self.rating}>"


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
