# 把 DSH（DeepSeek Harness）部署到群晖，24 小时在线

> 目标：电脑关机后，家里的手机 / 平板 / 其他电脑仍然能用 DSH 网页界面。

## 结论

**可以。** DSH 的网页界面本质上是一个纯 Node.js 服务（`@deepseek-ai/dsh` 这个 npm CLI 就能启动，不需要 Electron 桌面程序）。在群晖上用 Docker（Container Manager）跑它最省事，NAS 重启自动拉起、数据持久化。

本文件夹就是现成的部署包：

```
dsh-nas/
├── Dockerfile          # 基于 node:22 构建 DSH 无头服务镜像
├── entrypoint.sh       # 启动参数（回环监听 + 可选 trusted-host）
├── docker-compose.yml  # 容器编排（API Key、数据卷、自启动）
├── run.sh              # 一键构建+启动+看日志
└── README.zh.md        # 本文档
```

---

## 一、准备工作（约 5 分钟）

1. **确认型号支持**：NAS 是 x86_64（Intel/AMD）或 arm64；DSM 7.2 及以上。
2. **安装 Container Manager**：套件中心 → 搜索「Container Manager」→ 安装（DSM 7.2 自带 Docker 改名就是这个）。
3. **开启 SSH**：控制面板 → 终端机和 SNMP → 勾选「启动 SSH 功能」（部署需要 SSH 登录，默认端口 22）。
4. **把本文件夹传到 NAS**，任选一种：
   - File Station：在 `/volume1/docker` 下新建文件夹 `dsh`，把 `Dockerfile`、`entrypoint.sh`、`docker-compose.yml`、`run.sh` 四个文件传进去；
   - 或在你电脑的终端里：`scp -r dsh-nas/* admin@<NAS的IP>:/volume1/docker/dsh/`（文件夹要先建好）。
5. **填 API Key**：用文本编辑器打开 `docker-compose.yml`，把 `DEEPSEEK_API_KEY` 的值换成你的 DeepSeek Key。
   - 忘了 Key？在你电脑的 `~/.dsh/.credentials.yaml` 里找；
   - 也可以先不填——启动后打开网页，在 **设置 → 模型** 页面里粘贴保存，效果一样（数据会写进 NAS 的数据卷）。

## 二、部署（一条命令）

SSH 登录群晖（电脑终端或手机 Termius 都行）：

```
ssh admin@<NAS的IP>
cd /volume1/docker/dsh
sh run.sh
```

第一次要拉镜像 + 装 npm 包，等 1~5 分钟。日志出现下面这行就成功了：

```
dsh web: http://127.0.0.1:58838
```

容器已设置 `restart: unless-stopped`，**NAS 重启后会自动拉起**。

## 三、从别的设备访问

DSH 安全设计上**不允许 `--host 0.0.0.0`**（防止把远程代码执行能力直接暴露到网络），只监听 `127.0.0.1`。所以对外访问走下面两种方式之一：

### 方式 A：SSH 隧道（今晚就能用，零额外配置）

在你想用的那台设备上执行（手机装 Termius / JuiceSSH 同理）：

```
ssh -L 58838:127.0.0.1:58838 admin@<NAS的IP>
```

然后浏览器打开 **http://127.0.0.1:58838** 即可。电脑关不关机都不影响。

### 方式 B：群晖反向代理（长期推荐，打开网址就能用）

1. 群晖 **控制面板 → 登录门户 → 高级 → 反向代理服务器 → 新增**：
   - 来源：协议 `https`，主机名 `<NAS的IP 或 DDNS 域名>`，端口 `8443`（如有 WebSocket 选项请勾选）
   - 目的地：协议 `http`，主机名 `127.0.0.1`，端口 `58838`
2. 编辑 `docker-compose.yml`，取消 `DSH_TRUSTED_HOST` 的注释，填「来源」的 主机:端口（必须与浏览器地址栏完全一致，含端口）：
   ```
   DSH_TRUSTED_HOST: "192.168.1.5:8443"
   ```
   用 DDNS 域名就填 `myds.synology.me:8443`；多个用空格分隔。
3. 重新生效：
   ```
   cd /volume1/docker/dsh && docker compose up -d
   ```
4. 之后任何设备打开 `https://<NAS的IP>:8443` 就是 DSH 界面。配合群晖 QuickConnect / DDNS，外网也能访问。

> 为什么不能直接开放端口？`--host 0.0.0.0` 是 DSH 主动拒绝的（见 `dsh-web-app` 源码里的安全校验），反向代理 / SSH 隧道就是官方认可的对外方式。`--trusted-host` 是给浏览器信任围栏放行的：浏览器请求的 Host 头不在「回环地址 ∪ trusted-host」里，`/api` 会被拒。

## 四、数据与日常维护

| 操作 | 命令（在 `/volume1/docker/dsh` 下） |
| --- | --- |
| 看日志 | `docker compose logs -f` |
| 停止 | `docker compose down` |
| 启动 | `docker compose up -d` |
| 升级 | `docker compose build --pull && docker compose up -d` |

- 所有会话、凭据、设置都存在 **`/volume1/docker/dsh/dsh-data/`**（= 容器内 `$DSH_HOME`），删容器、升级都不丢。
- 建议给 dsh-data 目录开个快照或 Hyper Backup 备份任务。

## 五、常见问题

- **构建很慢 / 失败**：需要 NAS 能访问 `registry.npmjs.org`。国内网络慢的话，在 Container Manager → 注册表设置里配一个 Docker Hub / npm 镜像加速。
- **报 node-gyp / 编译错误**：取消 `Dockerfile` 里 build 工具那行的注释（`python3 make g++`），重新 `sh run.sh`。
- **反向代理打开是 502**：检查 ① 反向代理勾选了 WebSocket；② 目的地是 `127.0.0.1:58838`；③ `DSH_TRUSTED_HOST` 与「来源」完全一致（含端口）。
- **提示没有 API Key**：网页 设置 → 模型 里粘贴保存，或在 compose 里配 `DEEPSEEK_API_KEY`。
- **端口被占**：把 `entrypoint.sh`、`docker-compose.yml`（没有映射就不用改）、反向代理目的地的 58838 一起改成别的端口。
- **内存**：容器大约占 300–600MB，群晖建议 ≥2GB 内存。

## 六、安全提醒

- compose 用的是 `network_mode: host` + dsh 只监听 `127.0.0.1`，**没有**把 58838 暴露到公网。
- 反向代理如果暴露到公网，建议在群晖的访问控制里做登录保护，或只在内网使用。
- 容器里的 DSH 是 **Linux 环境**：它执行的是 bash（不是 PowerShell），文件系统只能看到挂载的卷。
- 权限预设默认比电脑上保守，需要时可登录后在 设置→权限 里调整。

---

*说明：部署包里的版本锁定为 `@deepseek-ai/dsh@0.1.0-rc.6`（与当前桌面版一致）。DSH 迭代很快，升级时把 Dockerfile 里的版本号改成新版即可。*
