#!/bin/sh
# 在群晖上执行：  sh run.sh
# 作用：构建镜像并启动 DSH 容器，然后打印启动日志。
set -e
cd "$(dirname "$0")"

echo "==> 构建并启动容器 ..."
docker compose up -d --build

echo "==> 启动日志（看到 'dsh web: http://127.0.0.1:58838' 即成功）==>"
docker compose logs --tail=100
