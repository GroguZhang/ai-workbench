#!/bin/sh
set -e

# DSH 出于安全设计拒绝 --host 0.0.0.0（防止把远程代码执行暴露到网络），
# 只监听本机回环地址。对外访问请用「群晖反向代理」或「SSH 隧道」（见 README）。
ARGS="--profile web --host 127.0.0.1 --port 58838"

# 可选：用群晖反向代理访问时，把「来源」的主机:端口填进 DSH_TRUSTED_HOST
# （多个用空格分隔），例如 DSH_TRUSTED_HOST="192.168.1.5:8443 myds.synology.me:8443"
# 浏览器请求的 Host 会经过浏览器信任围栏校验，必须与这里完全一致（含端口）。
if [ -n "$DSH_TRUSTED_HOST" ]; then
  for h in $DSH_TRUSTED_HOST; do
    ARGS="$ARGS --trusted-host $h"
  done
fi

exec dsh $ARGS
