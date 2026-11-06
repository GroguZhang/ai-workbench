# DSH (DeepSeek Harness) 无头 Web 服务镜像 —— 用于群晖 24 小时运行
# 镜像说明：DSH 的 Web 界面是一个纯 Node.js 服务（@deepseek-ai/dsh CLI），
# 不需要 Electron 桌面程序。此处用 Debian slim（glibc），原生模块
# （landlock / require-builtin / node-pty / sharp）都有 Linux 预编译二进制。

FROM node:22-bookworm-slim

# 一般不需要编译工具链；若 `npm install` 报 node-gyp / 编译类错误，
# 取消下面一行的注释后重新构建：
# RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
#     && rm -rf /var/lib/apt/lists/*

RUN npm install -g @deepseek-ai/dsh@0.1.0-rc.6 \
    && npm cache clean --force

COPY entrypoint.sh /usr/local/bin/dsh-entrypoint.sh
RUN chmod +x /usr/local/bin/dsh-entrypoint.sh

ENV DSH_HOME=/root/.dsh
# 容器使用 host 网络，dsh 直接监听群晖的 127.0.0.1:58838
EXPOSE 58838

ENTRYPOINT ["/usr/local/bin/dsh-entrypoint.sh"]
