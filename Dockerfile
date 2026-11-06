# syntax=docker/dockerfile:1
#
# 订单客服 Agent —— 运行镜像
#
# 一个镜像跑两种东西：
#   docker run ... python -m web.app --host 0.0.0.0     商家后台（默认）
#   docker run -it ... python -m agent.main --as u_1001  顾客端 CLI
#
# 两者共用同一份服务层与同一份 data 目录，所以容器里看到的订单状态
# 和宿主机（如果共用数据卷）永远一致。
FROM python:3.13-slim

# PYTHONUNBUFFERED: 日志实时输出，否则 docker logs 看不到 uvicorn 的信息
# PYTHONDONTWRITEBYTECODE: 不生成 __pycache__，保持容器内文件干净
# PIP_NO_CACHE_DIR: 不留 pip 缓存，镜像小一点
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    ORDER_DATA_DIR=/app/data \
    APP_HOME=/app

# tzdata 让 datetime.now() 走东八区；「今日已通过」「本周」这类统计依赖它，
# 否则容器默认 UTC，跨零点时统计会错一天。
# curl 只给 HEALTHCHECK 和容器内排查用。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR $APP_HOME

# 依赖单独一层：只要 requirements.txt 不变，改代码不会触发重装（省几分钟）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 代码本体。.dockerignore 已排除 .env / .venv / .git 等不该进镜像的东西
COPY . .

# 非 root 运行。data 目录需要在构建时就交给 appuser，
# 否则命名卷首次挂载时所有者是 root，容器内写不进去。
RUN useradd --create-home --uid 10001 --shell /bin/bash appuser \
 && mkdir -p $APP_HOME/data \
 && chmod +x $APP_HOME/docker/entrypoint.sh \
 && chown -R appuser:appuser $APP_HOME

USER appuser

EXPOSE 8000

# 探活打在登录页：不依赖登录态，能反映「进程活着 + 模板能渲染」两件事
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/login -o /dev/null || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "web.app", "--host", "0.0.0.0", "--port", "8000"]
