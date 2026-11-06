#!/bin/sh
#
# 容器入口：确保数据目录可用，再执行 CMD。
#
# 数据目录一般挂成卷（docker-compose 里的 order-data）。
# 命名卷首次挂载时 Docker 会把镜像内的 data/ 拷进去，所以通常不需要生成；
# 但绑定挂载到宿主机空目录时目录是空的，这里补一次种子数据，
# 免得服务起来后所有页面都是空的、还看不出是数据问题。
set -e

DATA_DIR="${ORDER_DATA_DIR:-/app/data}"

mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/orders.json" ]; then
    echo "[entrypoint] 数据目录 $DATA_DIR 为空，生成演示数据..."
    if python -m data.seed; then
        echo "[entrypoint] 演示数据已生成"
    else
        # 数据生成失败不该阻止服务启动：宁可起一个空库，也要让日志和健康检查可用
        echo "[entrypoint] 警告：演示数据生成失败，服务将以空数据启动" >&2
    fi
else
    echo "[entrypoint] 复用已有数据：$DATA_DIR"
fi

exec "$@"
