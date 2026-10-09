#!/usr/bin/env python3
"""建库 + 建表 + 校验，一条命令搞定。

为什么需要这个脚本
------------------
Django 的 ``migrate`` **只建表、不建库**：数据库不存在时，连接阶段就失败了，
根本轮不到 migrate 报错。云托管开通 MySQL 时会自动建库，但这三种场景需要自己来：

* 本地要连一个空的远程 MySQL 调试；
* 把服务迁到新实例，库还没建；
* 想先看一眼要执行哪些 SQL（交给 DBA 审核）。

脚本把三步合成一条命令，且**幂等**，可以反复执行：

1. 用独立连接（不指定库名）执行 ``CREATE DATABASE IF NOT EXISTS``
2. 调用 ``manage.py migrate`` 建表
3. 校验业务表是否齐全

用法::

    python scripts/init_db.py                 # 建库 + 建表 + 校验
    python scripts/init_db.py --dry-run       # 只打印将要做的事
    python scripts/init_db.py --print-sql     # 只打印迁移 SQL，不执行
    python scripts/init_db.py --skip-db       # 跳过建库（库已存在或无权限时）
    python scripts/init_db.py --check-only    # 只校验，不改动任何东西

本地开发（没配 MYSQL_ADDRESS）时会走 SQLite：建库这步自动跳过，
migrate 会直接把 sqlite 文件建出来。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wxcloudrun.settings")

# 业务表（Django 自带的 django_* 表不在这里列，它们随 migrate 一起建）
BUSINESS_TABLES: dict[str, str] = {
    "deai_rewrite_task": "改写任务（轮询、用量、反馈都要靠它）",
    "deai_quota_usage": "每日使用次数（不开数据库就会丢，用户可无限白嫖）",
    "deai_user_profile": "手机号授权记录（不影响使用次数）",
    "deai_wx_access_token": "微信 access_token 缓存（仅自管 token 模式用；云调用模式是空表）",
    "deai_feedback": "用户评价",
    "Counters": "模板原有的计数器示例",
}

# 需要跑的 app 迁移（按依赖顺序，Django 自己也会排）
MIGRATE_APPS = ("deai", "wxcloudrun")


def log(msg: str) -> None:
    print(f"[init_db] {msg}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="建库 + 建表 + 校验（幂等，可反复执行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不改动数据库")
    group.add_argument("--print-sql", action="store_true", help="只打印迁移 SQL 后退出")
    group.add_argument("--check-only", action="store_true", help="只校验表是否齐全")
    parser.add_argument("--skip-db", action="store_true", help="跳过建库步骤")
    parser.add_argument(
        "--fix-tables",
        action="store_true",
        help="把库内已有表也转成 utf8mb4（大表会锁表，谨慎）",
    )
    return parser.parse_args()


def db_settings() -> dict:
    """从 Django settings 里取数据库配置，避免两处各写一份。"""
    from django.conf import settings

    conf = settings.DATABASES["default"]
    return {
        "engine": conf["ENGINE"],
        "name": str(conf["NAME"]),
        "host": str(conf.get("HOST") or ""),
        "port": str(conf.get("PORT") or "3306"),
        "user": str(conf.get("USER") or ""),
        "password": str(conf.get("PASSWORD") or ""),
    }


def ensure_database(conf: dict, dry_run: bool, fix_tables: bool) -> bool:
    """确保库存在且字符集是 utf8mb4。返回是否真的改动了数据库。

    这里有两件容易被忽略的事：

    1. **必须用独立连接且不指定库名**——Django 的连接一上来就要选库，
       库不存在时连不上，所以我们绕开 ORM 直接用驱动。
    2. **库已存在不代表字符集对**。腾讯云 CynosDB 开通时默认给的库是
       ``utf8``（即 utf8mb3，最多 3 字节），**存 emoji 会静默变成 ``?``**。
       实测：``CONVERT('😀' USING utf8)`` → ``'?'``。用户输入一个 emoji
       就丢字符，而且不报错——这种坑必须在这里堵住。
    """
    if not conf["engine"].endswith("mysql"):
        log(f"非 MySQL（{conf['engine'].split('.')[-1]}），跳过建库；migrate 会自动建文件")
        return False

    name = conf["name"]
    create_sql = (
        f"CREATE DATABASE IF NOT EXISTS `{name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    alter_sql = (
        f"ALTER DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )

    if dry_run:
        log(f"[dry-run] 将要执行：{create_sql}")
        log(f"[dry-run] 若库已存在但字符集不是 utf8mb4，还会执行：{alter_sql}")
        return False

    try:
        import pymysql
    except ImportError:
        log("缺少 PyMySQL，无法建库。请先 pip install -r requirements.txt，或用 --skip-db")
        return False

    log(f"连接 MySQL {conf['host']}:{conf['port']}（不指定库名）")
    try:
        conn = pymysql.connect(
            host=conf["host"],
            port=int(conf["port"]),
            user=conf["user"],
            password=conf["password"],
            charset="utf8mb4",
            connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - 连接失败要给用户看得懂的话
        log(f"连接失败：{exc}")
        log("检查 MYSQL_ADDRESS / MYSQL_USERNAME / MYSQL_PASSWORD 是否正确")
        raise SystemExit(1)

    changed = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME=%s",
                (name,),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(create_sql)
                log(f"数据库 `{name}` 已创建（utf8mb4）")
                changed = True
            elif str(row[0]).lower() != "utf8mb4":
                log(f"数据库 `{name}` 已存在，但字符集是 {row[0]} —— 改成 utf8mb4")
                cur.execute(alter_sql)
                changed = True
            else:
                log(f"数据库 `{name}` 已就绪（utf8mb4）")

            if fix_tables:
                changed = _convert_tables(cur, name) or changed
            else:
                stale = _tables_not_utf8mb4(cur, name)
                if stale:
                    log(f"注意：以下表仍不是 utf8mb4：{', '.join(stale)}")
                    log("      如果这些表是空的或很小，可以加 --fix-tables 转换")
        conn.commit()
    finally:
        conn.close()
    return changed


def _tables_not_utf8mb4(cur, schema: str) -> list[str]:
    cur.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_COLLATION NOT LIKE 'utf8mb4%%'",
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def _convert_tables(cur, schema: str) -> bool:
    """把库内已有表转成 utf8mb4。注意 ALTER TABLE 会锁表，大表慎用。"""
    stale = _tables_not_utf8mb4(cur, schema)
    if not stale:
        return False
    for table in stale:
        log(f"  转换表 {table} → utf8mb4")
        cur.execute(
            f"ALTER TABLE `{schema}`.`{table}` "
            "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    log(f"已转换 {len(stale)} 张表")
    return True


def run_migrations(dry_run: bool) -> None:
    import django

    django.setup()
    from django.core.management import call_command

    if dry_run:
        log("[dry-run] 将要执行：manage.py migrate")
        return

    log("执行 migrate 建表……")
    call_command("migrate", interactive=False, verbosity=1, fake_initial=True)


def print_migration_sql() -> None:
    """打印所有迁移会执行的 SQL，不落库。适合交给 DBA 审核或纯手工建表。"""
    import django

    django.setup()
    from django.core.management import call_command
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    printed_any = False
    for app in MIGRATE_APPS:
        names = sorted(name for (app_label, name) in loader.disk_migrations if app_label == app)
        for name in names:
            print(f"\n-- ===== {app}.{name} =====")
            call_command("sqlmigrate", app, name, verbosity=0)
            printed_any = True
    if not printed_any:
        log("没有找到任何迁移")
    else:
        log("以上是按顺序拼接的 SQL（Django 自带 app 的迁移未包含在内）")


def verify(conf: dict) -> bool:
    """校验业务表是否齐全。返回是否全部就绪。

    **大小写不敏感**：MySQL 的 ``lower_case_table_names=1``（Linux 默认）会把表名
    统一存成小写，于是模型的 ``Counters`` 实际建出来叫 ``counters``。
    直接做集合比对会把建好的表误报成缺失——实测在腾讯云 CynosDB 上就踩到了。
    Django 自己查询不受影响（MySQL 侧会做大小写折叠），所以这里也照做。
    """
    import django

    django.setup()
    from django.db import connection

    with connection.cursor() as cur:
        existing = {t.lower() for t in connection.introspection.table_names(cur)}

    missing = [t for t in BUSINESS_TABLES if t.lower() not in existing]
    log(f"数据库：{conf['name']}（{conf['engine'].split('.')[-1]}）")
    for table, desc in BUSINESS_TABLES.items():
        mark = "✓" if table.lower() in existing else "✗ 缺失"
        log(f"  {mark} {table:22s} {desc}")

    if missing:
        log(f"缺少 {len(missing)} 张表：{', '.join(missing)}")
        return False
    log("全部业务表就绪")
    return True


def main() -> int:
    args = parse_args()

    if args.print_sql:
        print_migration_sql()
        return 0

    # 取配置前先 setup，settings 里的 DATABASES 才会生效
    import django

    django.setup()
    conf = db_settings()

    if args.check_only:
        return 0 if verify(conf) else 1

    log(f"目标：{conf['engine'].split('.')[-1]} / {conf['name']}")
    if not args.skip_db:
        ensure_database(conf, args.dry_run, args.fix_tables)
    else:
        log("按参数要求跳过建库")

    run_migrations(args.dry_run)
    if args.dry_run:
        log("[dry-run] 结束，未改动任何数据")
        return 0

    return 0 if verify(conf) else 1


if __name__ == "__main__":
    raise SystemExit(main())
