# ---------- 微信云托管 · Django（生产可用） ----------
# 官方模板用的是 alpine:3.13 + python3（3.7）+ `manage.py runserver`，
# runserver 是单线程开发服务器，不能上生产；这里换成 python:3.11-slim + gunicorn。

FROM python:3.11-slim

# 云托管容器默认 UTC，日志时间会差 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，最大化利用构建缓存
COPY requirements.txt .
RUN pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple \
    && pip config set global.trusted-host mirrors.cloud.tencent.com \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# 端口必须与控制台「服务设置 / 发布时」填写的端口完全一致，否则 Readiness probe failed
EXPOSE 80

# 单行 CMD：写多行独立 CMD 只有最后一行会执行（官方 FAQ 明确列为常见错误）。
# --fake-initial：模板部署路径下 Counters 表已由 container.config.json 的
#   executeSQLs 建好，Django 需要识别并跳过，否则会报「表已存在」导致启动失败。
# --timeout 55 < 平台 60s 上限；真正的长任务（大模型改写）在后台线程里跑，
#   由 /api/task/<id> 轮询取结果，所以不会撞上这个超时。
CMD ["sh", "-c", "python manage.py migrate --noinput --fake-initial && gunicorn wxcloudrun.wsgi:application -b 0.0.0.0:80 -w 2 -k gthread --threads 4 --timeout 55 --graceful-timeout 30 --keep-alive 5 --access-logfile - --error-logfile -"]
