"""项目包。

两件事在这里做，**顺序不能颠倒**：

1. **驱动替换**：Django 默认用 mysqlclient 连 MySQL，而镜像里装的是纯 Python 的
   PyMySQL。没装 PyMySQL（纯 SQLite 本地开发）时静默跳过。
2. **可选的 MySQL 5.7 兼容开关**：Django 4.2 起官方不再支持 MySQL 5.7（只支持
   8.0+），连库时会直接抛 ``NotSupportedError``。腾讯云 CynosDB 等托管服务仍可能
   给 5.7 实例，这时可以用 ``MYSQL_ALLOW_57=true`` 绕过版本检查。

第 2 步必须在第 1 步之后：导入 mysql backend 会触发 ``import MySQLdb``，
而那时驱动还没替换好。
"""

from __future__ import annotations

import os

try:  # pragma: no cover - 取决于部署形态
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover
    pass


def _env_bool(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# Django 4.2 起移除 MySQL 5.7 支持（见 ticket #33718）。本项目实测在 5.7.18 上：
# 连接、中文与 emoji 往返、以及全部建表 DDL 都正常——模型只用了 varchar/longtext/
# integer/bigint/datetime(6)/numeric/date 这些基础类型，没有 8.0 专有语法。
#
# 但这**仍是官方未支持的组合**：Django 不会为 5.7 做兼容测试，未来用到窗口函数、
# 表达式默认值之类的特性时会踩坑。所以做成显式开关而不是默认绕过，
# 并且建议尽快把实例升到 MySQL 8。
if _env_bool("MYSQL_ALLOW_57"):  # pragma: no cover - 取决于部署形态
    try:
        from django.db.backends.mysql.base import DatabaseWrapper

        DatabaseWrapper.check_database_version_supported = lambda self: None
    except ImportError:  # pragma: no cover - 没装 mysql 依赖时无从绕过
        pass
