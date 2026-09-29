from django.db import models
from django.utils import timezone


class Counters(models.Model):
    """模板自带的「计数器读写」示例，行为保持不变。

    原来有四处会在系统检查里报警告、甚至运行时直接报错，顺手修掉：

    * ``id = models.AutoField`` 少写了括号（是类而不是实例），Django 会忽略它
      再自动补一个主键。直接删掉，交给 Django 生成，结果一致。
    * ``IntegerField(max_length=11)`` —— ``max_length`` 对整型无效，只会产生
      ``fields.W122`` 警告。
    * ``default=datetime.now()`` —— 在**类定义时**就求值了，所有记录会共用同一个
      时间戳。改成传函数 ``timezone.now``（注意不加括号）。
    * ``__str__`` 引用了不存在的 ``self.title``，一打印就 AttributeError。

    这些改动都只涉及 Python 层，不影响 ``Counters`` 表结构，因此无需迁移。
    """

    count = models.IntegerField(default=0)
    createdAt = models.DateTimeField(default=timezone.now)
    updatedAt = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Counters(id={self.pk}, count={self.count})"

    class Meta:
        db_table = "Counters"  # 数据库表名
