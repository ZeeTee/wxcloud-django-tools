"""项目包。

Django 默认用 mysqlclient 连 MySQL，而镜像里装的是纯 Python 的 PyMySQL，
这里做一次驱动替换。没装 PyMySQL（纯 SQLite 本地开发）时静默跳过。
"""

from __future__ import annotations

try:  # pragma: no cover - 取决于部署形态
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover
    pass
