#!/bin/sh
# 容器启动：先迁移，再起服务。
#
# 为什么需要这个脚本而不是把命令堆在 CMD 里
# ------------------------------------------
# 原来 CMD 是 `migrate && gunicorn`。migrate 一失败，gunicorn 就不启动，
# 80 端口没人监听，云托管只会报：
#
#     Readiness probe failed: dial tcp 10.x.x.x:80: connect: connection refused
#
# **真实原因完全看不到**——是数据库连不上？是版本不兼容？是密码错？
# 排查时只能靠猜。这里把每一步都打出来，并在失败时给出最常见的几种原因。
set -e

echo "[start] ================ 启动检查 ================"
echo "[start] LLM_PROVIDER    = ${LLM_PROVIDER:-<未设，默认 deepseek>}"
echo "[start] MYSQL_ADDRESS   = ${MYSQL_ADDRESS:-<未设，将使用容器内 SQLite>}"
echo "[start] MYSQL_DATABASE  = ${MYSQL_DATABASE:-<未设，默认 django_demo>}"
echo "[start] MYSQL_USERNAME  = ${MYSQL_USERNAME:-${MYSQL_USER:-<未设>}}"
echo "[start] MYSQL_ALLOW_57  = ${MYSQL_ALLOW_57:-<未设>}"
echo "[start] DJANGO_DEBUG    = ${DJANGO_DEBUG:-<未设，默认 false>}"
echo "[start] =========================================="

# 打开这个开关等于关掉 Django 的版本保护，所以显式提醒一次
if [ "${MYSQL_ALLOW_57}" = "true" ] && [ -n "${MYSQL_ADDRESS}" ]; then
  echo "[start] 注意：已开启 MYSQL_ALLOW_57，将跳过 MySQL 版本检查"
fi

echo "[start] 执行数据库迁移……"
if ! python manage.py migrate --noinput --fake-initial; then
  echo "[start] =========================================="
  echo "[start] 数据库迁移失败，容器不会启动（这是刻意的 fail-fast）。"
  echo "[start] 上面有 migrate 的原始报错，常见原因对照："
  echo "[start]"
  echo "[start] 1) Can't connect to MySQL / timed out / Name or service not known"
  echo "[start]    → 数据库地址不可达。云托管容器【默认没有公网出口】，"
  echo "[start]      MYSQL_ADDRESS 必须填数据库的【内网地址】，不能用公网地址。"
  echo "[start]    → 也检查数据库是否放行了云托管的出口 IP。"
  echo "[start]"
  echo "[start] 2) MySQL 8 or later is required (found 5.7.x)"
  echo "[start]    → Django 4.2 不支持 MySQL 5.7。升级实例到 8.0，"
  echo "[start]      或临时设 MYSQL_ALLOW_57=true（不推荐长期开）。"
  echo "[start]"
  echo "[start] 3) Access denied for user"
  echo "[start]    → MYSQL_USERNAME / MYSQL_PASSWORD 不对。"
  echo "[start]"
  echo "[start] 4) Unknown database"
  echo "[start]    → 库不存在。先跑 scripts/init_db.py 建库建表，"
  echo "[start]      或把 MYSQL_DATABASE 改成实际库名。"
  echo "[start]"
  echo "[start] 5) Commands out of sync / 连接被中断"
  echo "[start]    → 内网地址填成了公网，或网络策略拦截，同上第 1 条。"
  echo "[start] =========================================="
  exit 1
fi
echo "[start] 迁移完成"

echo "[start] 启动 gunicorn（监听 0.0.0.0:80）……"
exec gunicorn wxcloudrun.wsgi:application \
  -b 0.0.0.0:80 \
  -w 2 -k gthread --threads 4 \
  --timeout 55 --graceful-timeout 30 --keep-alive 5 \
  --access-logfile - --error-logfile -
