#!/usr/bin/env bash
# 本地起服务：加载 .env → 建表 → runserver
#
#   ./scripts/dev.sh                       # http://127.0.0.1:8080
#
# 不配 MYSQL_ADDRESS 时会自动用 SQLite（<项目根>/data/deai.sqlite3），
# 所以本地开发不需要先装 MySQL。
#
# 注意：本地没有云托管注入的身份头，所以这里强制打开 DEAI_ALLOW_ANONYMOUS，
# 否则所有接口都会 401。生产环境必须保持它关闭。
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
else
  echo "提示：没有 .env，将使用默认配置（且没有 LLM_API_KEY，AI 改写会返回 503）" >&2
fi

export DJANGO_DEBUG="${DJANGO_DEBUG:-true}"
export DEAI_ALLOW_ANONYMOUS="${DEAI_ALLOW_ANONYMOUS:-true}"

# --fake-initial：如果库里已经有模板部署时建的 Counters 表，识别并跳过而不是报错
python3 manage.py migrate --noinput --fake-initial
exec python3 manage.py runserver "${BIND:-0.0.0.0:8080}"
