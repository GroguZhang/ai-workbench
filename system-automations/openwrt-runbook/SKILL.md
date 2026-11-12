---
name: openwrt-kwrt-remote-ops
description: 远程运维 OpenWrt / Kwrt（openwrt.ai 系）路由器——SSH 通道搭建、固件升级后抢修（全屋断网/DNS 黑洞/OpenClash 消失）、插件增删、新 opkg 不支持本地 ipk 时自建本地源离线安装、OpenClash 内核与启用故障、软件源报错定位、overlay 空间治理与 U 盘 extroot 建议。当用户给出路由器 IP/账号要求登录排障、说"升级固件后连不上网/WiFi 上不去、电脑没网"、提到 OpenClash/PassWall/SSR Plus 启用失败、opkg 软件源报错、装不上插件、overlay 空间不足、Kwrt/openwrt.ai 固件时触发。
agent_created: true
---

# OpenWrt / Kwrt 路由器远程运维

## 何时用

- 用户给 IP + 账号（如 `192.168.1.1`，root/<password>）要求 SSH 登录排障
- OpenClash「内核无法更新」「无法启用」、PassWall/SSR Plus 卸载、软件源错误、overlay 空间不足
- 固件是第三方魔改版（Kwrt / openwrt.ai / kiddin9 / iStoreOS）

---

## 0. Windows 侧 SSH 通道（先搭好再干活）

**坑 1：Git Bash 的 `MSYS_NO_PATHCONV`**
Git Bash 会把命令行参数里的 `/tmp/xxx` 自动改写成 `C:/Users/.../Temp/xxx`，
导致 paramiko SFTP / `cat > /tmp/x` 报 `ENOENT`。
**所有带远端绝对路径的命令前必须加 `export MSYS_NO_PATHCONV=1`。**

**坑 2：很多 OpenWrt 没开 SFTP 子系统**
dropbear 默认无 sftp-server。不要依赖 SFTP，用 `exec_command("cat > 远端路径")` + `stdin.write()` 流式上传。
仓库内 `_work/` 下的三个助手脚本（可复制到任意项目）：
- `rssh.py`：`python rssh.py "cmd1" "cmd2"` 逐条执行；`-f script.sh` 推脚本执行；`-F` 带 `sh -x`
- `rput.py`：`python rput.py <local> <remote> [...]`，exec+stdin 上传并校验远端字节数
- `rget.py`：`python rget.py <remote> <local>` 拉回文件

依赖：托管 venv 内 `paramiko`（`<venv>\Scripts\python.exe`）。

**坑 3：单条命令超时**
长任务（opkg 下载、OpenClash 启动）会拖死整个 bash 调用。用 `ROUTER_CMD_TIMEOUT` 环境变量控制，
或把多步逻辑写进本地 `.sh` 用 `rssh.py -f` 一次执行。

**坑 4：写脚本时避免 `grep` 正则 `{}`**：busybox grep 不支持 `.{0,200}`。
远端是 busybox：`sort -h`/`timeout`/`lsusb` 可能不存在；`nc` 无 `-z`。

---

## 0.5 固件升级后的抢修清单（用户报"全屋断网/连不上 WiFi"时先看这条）

**症状**：升级固件后，SSH 还能连上路由器、WiFi 也在广播、WAN 也拿到了 IP，
但所有终端"能连上却上不了网"。**90% 是 DNS 黑洞，不是网络故障。**

**根因**：OpenClash 的 `enable_redirect_dns=1` 会把 dnsmasq 改成
```
dhcp.cfg01411c.noresolv='1'
dhcp.cfg01411c.server='127.0.0.1#7874'   # 7874 是 OpenClash 的 DNS
```
固件升级会**删掉 OpenClash 程序**（但保留 `/etc/openclash` 数据与 `/etc/config/openclash`），
于是 7874 没人监听 → dnsmasq 转发全超时 → 全 LAN 域名解析死。

**三行判定**（在路由器上跑）
```sh
nslookup www.baidu.com 223.5.5.5      # 通 → WAN/ISP 没问题
nslookup www.baidu.com 192.168.1.1  # 超时 → 就是它
ping -c2 223.5.5.5 && ping -c2 192.168.1.1   # IP 层全通 → 排除链路
```
**紧急恢复**（立刻全网复活，先做这个再谈别的）
```sh
uci -q delete dhcp.@dnsmasq[0].server
uci -q delete dhcp.@dnsmasq[0].noresolv
uci commit dhcp && /etc/init.d/dnsmasq restart
```
> 之后 OpenClash 一旦重新启动，它会把这两项自动写回来——**这是正常的，别去删**。

**升级后恢复 OpenClash 的正确姿势**
固件升级只删程序本体，`/etc/openclash/`（含 `core/clash_meta`、`config/<订阅>.yaml`、GeoIP/GeoSite）
和 `/etc/config/openclash`（含订阅地址）**都还在**。所以只需重装 `luci-app-openclash`，
核心与订阅无需重下，装完直接 `enable=1` 启动即可。

**升级还会清掉**：`/etc/hotplug.d/online/40-pin-feeds`、`/etc/opkg-restore-auto`、
distfeeds 软链、`quickstart.main.disable_update_check` → **每次升级后都要重做一遍**（见 §2、§4）。

---

## 1. 首轮侦察（必做，别跳过）

```sh
cat /etc/openwrt_release        # 发行版/版本/target/arch
cat /proc/mtd; ubinfo -a        # NAND 分区 + UBI 卷 + available LEBs
df -h /; free -m; uptime
du -sk /overlay/upper/*         # overlay 真实占用（UBIFS 有压缩，du 比 df 更接近真相）
opkg list-installed | wc -l
```

**关键判断：`ubinfo -a` 的 `Amount of available logical eraseblocks: 0`**
→ overlay 是**硬上限**，无法扩容，只能靠卸载腾地方。这点必须先告诉用户。

**UBIFS 压缩注意**：删掉 11.5MB 的二进制，df 只会多出约 4.9MB。别按文件原始大小估算能腾多少空间。

---

## 2. 软件源报错：Kwrt 的两个真凶

### 真凶 A：固件内置的「一键换源」会把官方源改坏

Kwrt 的 `/admin/quickstart`（快速设置首页）加载时会自动跑一次「国内镜像加速」，
把 `/etc/opkg/distfeeds.conf` 里的 `dl.openwrt.ai` 替换成
`mirrors.{aliyun,ustc,tuna,sustech,cernet,cloud.tencent}.com/openwrt`。

**而这些公共镜像只同步 OpenWrt 官方发行版。OpenWrt 25.12 是 rc/快照，公共镜像根本没有
`releases/25.12/...` 路径 → 必然 404 → 用户看到"一直提示软件源错误"，
而且每次打开快速设置页都会被重新改坏一次。**

写入者：`/usr/sbin/quickstart` 与 `/usr/sbin/startdhns`（Go 二进制，字符串表里能看到镜像列表 + `distfeeds.conf`）。
触发证据：`logread | grep "login on /admin/quickstart"`，时间戳与 distfeeds mtime 对得上。

**定位手法（可复用）**
```sh
grep -rl 'distfeeds' /usr/lib/lua /usr/sbin /etc/init.d 2>/dev/null      # 找写入者
grep -aE 'https?://[a-z0-9./-]*mirror[a-z0-9./-]*' /usr/sbin/quickstart   # 挖二进制字符串表
grep -aoE '/api/[a-zA-Z0-9/_-]+' /usr/sbin/<bin>                          # 挖路由
logread | grep -iE 'quickstart|opkg|distfeed|mirror'
```

**修复（双层，缺一不可）**
```sh
# 1) 关掉快速设置页的自动检查（main.htm 里读取的官方开关）
uci set quickstart.main.disable_update_check='1'; uci commit quickstart

# 2) 物理上钉死：改成指向只读 /rom 的软链，任何写入都拿到 EROFS
cp -f /rom/etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf   # 先确认原厂内容
rm -f /etc/opkg/distfeeds.conf
ln -s /rom/etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf
echo x >> /etc/opkg/distfeeds.conf   # 应报 "Read-only file system"
```
原厂源永远还在只读层：`/rom/etc/opkg/distfeeds.conf` —— **排查任何被改坏的配置，第一站都是去 /rom 比对。**

### 真凶 B：官方源 dl.openwrt.ai 直连不稳（时通时断，不是全不通）

实测（同一台路由器、无代理）：连发 3 次同一 URL → `200 / 2.3s`、`000 / 30s 超时`、`000 / 30s 超时`。
**不是"墙掉"，是链路质量差**。表现就是 opkg 报 `wget returned 4`，而且**大小包都会被随机命中**
（7.8KB 的 ruby-pstore 连挂两次，8.17MB 的 openclash 反而下得动）。
→ 结论：**别赌远程源，大包一律走 §4.5 本地源安装。**

**顺序铁律：先让 OpenClash 跑起来 → router_self_proxy → dl.openwrt.ai 明显变稳 → opkg 才更新得动。
任何"停 OpenClash 再 opkg install 联网包"的操作都会失败。**

### 真凶 C：用户口中的"软件源错误"是 quickstart 首页卡片，不是 opkg

**先分清两个"软件源错误"：**
1. LuCI 软件包页/命令行 opkg 报的错 → 真凶 A / B
2. **首页（iStoreOS 风格 /admin/quickstart）里那张"软件源错误"卡片** → 由 quickstart Go 后端的状态机给出

字符串出处：`/www/luci-static/quickstart/i18n/en.json` 里
`"软件源错误":"Feeds Inaccessible"`。真正的前端判断条件在 `index.js`（UTF-8 以 `\uXXXX` 存储）：
```sh
grep -o -E '.{400}\\u8F6F\\u4EF6\\u6E90\\u9519\\u8BEF.{400}' /www/luci-static/quickstart/index.js
# → e(m).networkInfo=="netSuccess" / "dnsFailed" / "softSourceFailed"(软件源错误) / 其它(未联网)
```
所以判断依据是 **`networkInfo` 字段**，只有 4 个取值。

**istore 后端接口（Go，监听 127.0.0.1:3038 + unix:/var/run/quickstart/local.sock）**
- 直连 3038 / unix socket 一律返回 `{"success":-1001}`（缺内部代理注入的鉴权），**必须经 nginx**：
```sh
CK=/tmp/ck; curl -s -c $CK -o /dev/null -d 'luci_username=root&luci_password=root' http://127.0.0.1/cgi-bin/luci/
curl -s -b $CK http://127.0.0.1/cgi-bin/luci/istore/network/status/        # networkInfo / dnsList / gateway
curl -s -b $CK http://127.0.0.1/cgi-bin/luci/istore/guide/soft-source/     # 当前源 identity/name/url
curl -s -b $CK http://127.0.0.1/cgi-bin/luci/istore/guide/soft-source/list/ # 八家候选镜像
curl -s -b $CK http://127.0.0.1/cgi-bin/luci/istore/system/version/        # 固件/内核/quickstart 版本
```
（前缀规律：`/cgi-bin/luci/istore/<group>/...`，前端 baseURL 就是 `/cgi-bin/luci/istore`）

**实测结论（Kwrt 25.12.0-rc2 / quickstart 0.11.13）**
- 修好源后 `guide/soft-source/` 正确回报 `{"identity":"https://dl.openwrt.ai/"}` ✅
- `network/status/` 长期停在 `"networkInfo":"netDetecting"`（检测状态机不会自己收尾；
  `quickstart uciChange|ifaceEvent|dhcpValid`、重启 quickstart 服务、`ifup wan` 都**推不动**它）
- 也就是说：**这张卡片的显示与 opkg 是否可用完全解耦**。opkg update 0 错误时，卡片仍可能显示旧状态。
  → 交付时要明确告诉用户："看到的是首页的联网自检卡片，与软件源本身无关；刷新页面/重启路由后自行消除"，
  不要为了让卡片变绿去动 distfeeds。

**彩蛋（同源坑）**：`ttyd` 日志里会出现 `lws_sort_dns: 198.18.1.47 has no route out` →
fake-ip 段自流量无路由；`ifup wan` 之后会变成 `CPD result OK`。快速自检失败多半是它。

### 附加加固 1：用 hotplug 兜底重钉软链

软链虽能挡 `open()` 写入，但挡不住"先 `os.Remove` 再 `os.Create`"的写法。加一条幂等 hotplug：
```sh
cat > /etc/hotplug.d/online/40-pin-feeds <<'EOF'
#!/bin/sh
[ -L /etc/opkg/distfeeds.conf ] && exit 0
rm -f /etc/opkg/distfeeds.conf
ln -sf /rom/etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf
logger -t feeds-pin "distfeeds.conf re-pinned to /rom"
EOF
chmod +x /etc/hotplug.d/online/40-pin-feeds
```
放 `online/` 且编号 40（先于 50-opkg-restore / 51-opkg-upgrade）。
自测：`cp -f /rom/... /etc/opkg/distfeeds.conf; hotplug-call online; ls -l /etc/opkg/distfeeds.conf`。

### 附加加固 2：把 opkg 列表挪出 tmpfs

`/var` 指向 /tmp，重启即丢。改到 overlay 让 LuCI 马上可用：
```sh
sed -i 's|^lists_dir .*|lists_dir ext /etc/opkg-lists|' /etc/opkg.conf
mkdir -p /etc/opkg-lists && cp -f /var/opkg-lists/* /etc/opkg-lists/
```
验证：`opkg update 2>&1 | grep -icE 'failed|error'` 必须为 **0**。

---

## 3. OpenClash「内核无法更新 / 无法启用」

### 内核本来是好的
先跑 `/etc/openclash/core/clash_meta -v`。若打印 `Mihomo Meta ...`，**内核没坏，只是"查不到新版本"**。
`/tmp/clash_last_version` 下载失败（`curl: (35) Recv failure`）= GitHub 被墙，不是内核问题。

### 修复：把 GitHub 前缀换成可达的加速站
`github_address_mod` 是**前缀拼接**语义（`前缀 + https://raw.githubusercontent.com/...`），不是索引号。
`gh-proxy.com` 在国内可达且实测 0.28s：
```sh
uci set openclash.config.github_address_mod='https://gh-proxy.com/'
uci commit openclash
rm -f /tmp/clash_last_version /tmp/openclash_last_version   # 清掉失败缓存，否则不重试
# 用 OpenClash 自己的脚本验证
/usr/share/openclash/clash_version.sh 'https://gh-proxy.com/';     cat /tmp/clash_last_version
/usr/share/openclash/openclash_version.sh 'https://gh-proxy.com/'; head -1 /tmp/openclash_last_version
```
实测可用：`gh-proxy.com`。**不通**：`ghfast.top`、`ghproxy.net`、`github.moeyy.xyz`、`gh.llkk.cc`、`ghp.ci`。

涉及其它 GitHub 下载的开关同理：`urltest_address_mod`。

### 「无法启用」的真相
`/etc/init.d/openclash start` 若回 `Warning: OpenClash Now Disabled, Need Start From Luci Page`
= `openclash.config.enable='0'`。LuCI 点启用时它先做版本检查，GitHub 不通 → 卡死，看起来"启不了"。
修好加速后：
```sh
uci set openclash.config.enable='1'; uci commit openclash
/etc/init.d/openclash start
```
**验收三件套**（不要只看 `ps`）：
```sh
pgrep -f '/etc/openclash/clash'          # 注意：进程名是 /etc/openclash/clash，不是 clash_meta！grep clash_meta 会假阴性
netstat -lntp | grep -E '7890|7891|7893|9090'
cat /tmp/openclash_start.log             # 期望 ##FINISH##
curl -sS -m 15 -o /dev/null -w '%{http_code}' https://www.google.com   # 启用前必超时，启用后应 200
```

### 代理端口 407 / SOCKS5 被拒 ≠ 坏了
机场订阅配置里的 `authentication:` 段会给入站端口加鉴权。此时：
- **显式代理**（客户端填 192.168.1.1:7890）会 407，需用户名密码
- **透明代理照常工作**（这是实际使用方式）
判断透明代理是否生效，用"启用前 vs 启用后"对比 google/youtube 的返回码最可靠。

### 关于给 OpenClash 升级程序本体
- 自更新走 `opkg install /tmp/openclash.ipk`，**必须先把 OpenClash 停掉**，否则内核文件被占用
- 会因 `ETXTBSY`/空间问题失败；大 ipk 在空间不足时表现为 **opkg 段错误（rc=139）**，日志只写 "Installation failed"
- **务必先下载好 ipk，再停 OpenClash，再本地安装**（停服 = 断代理 = 下不动包）
- OpenClash 官方源包（GitHub vernesong）自带 core，装完约 22MB；**32MB overlay 装不下**，需要 U 盘 extroot

---

## 4. 卸载插件（保留依赖的正确姿势）

Kwrt 的 passwall / ssr-plus 主包卸掉后，会留下一堆孤立依赖（xray-core、trojan-plus、
shadowsocks-libev/rust/ssr、dns2socks、microsocks、ipt2socks、chinadns-ng、tcping、v2ray-plugin、simple-obfs）。
**先扫反向依赖再删**：
```sh
for p in xray-core trojan-plus ...; do echo "$p -> $(grep -c "^Depends:.*$p" /usr/lib/opkg/status)"; done
```
全为 0 才删。**绝不要动**：`kmod-nft-tproxy`、`kmod-tun`、`kmod-nft-socket`、`dnsmasq-full`、`bash`、`curl`、`ruby*`、`ip-full` —— OpenClash 依赖它们。

残留不止包管理器：还要清 `/usr/lib/lua/luci/{model/cbi,view}/<插件>`、`/www/luci-static/<插件>`、
`/usr/share/ucitrack/luci-app-*.json`、以及 `wizard` 里指向已删页面的 shortcut：
```sh
uci -q delete wizard.@shortcuts[0]; uci commit wizard
```

### ⚠️ 厂商 App 是「主包 + meta 包 + i18n」三件套，别只删主包

Kwrt/iStoreOS 的厂商 App（FastNet 等）装的是：
`app-meta-<x>`（壳，Depends: libc, <x>, luci-app-<x>, luci-i18n-<x>-zh-cn）
+ 真正的 `<x>` + `luci-app-<x>` + `<x>` 的中文语言包。

只删后三个，`app-meta-<x>` 会变成**断链 meta**（依赖全缺但自身仍在），并在
`/usr/share/linkeasefull/desktop-apps.d/`、`/usr/libexec/istorea/` 留下图标/脚本。
判定手法：
```sh
opkg info app-meta-fastnet            # 看 Depends 里引用的包是否还在
opkg whatdepends app-meta-fastnet     # 反向依赖为空即可删
opkg remove app-meta-fastnet luci-i18n-fastnet-zh-cn
```
**别删 `linkease-app-entry` / `luci-lib-linkeaseauth`** —— 那是易有云/LinkEase 的入口与鉴权库，
用户说"保留 LinkEase"就是指它们。

### ⚠️ 别误删：opkg 里 `grep -i fastnet` 剩下的可能是保留项

`opkg list-installed | grep -i -E 'fastnet|linkease'` 一次列全，逐条判断归属，
不要看到关键字就 `remove`。

### ⚠️ 必做的收尾：阻断 `opkg restore` 把删掉的包装回来
Kwrt 的 `/etc/hotplug.d/online/50-opkg-restore` 会在联网后执行 `opkg restore`，
其逻辑是**"把 /rom 镜像里存在但当前未安装的包装回来"** —— 你刚删的插件正好命中。
```sh
touch /etc/opkg-restore-auto      # 一次性恢复任务不再执行；想恢复就删掉它
```

---

## 4.5 ⚠️ 新 opkg 不支持安装本地 ipk —— 必须自建本地源

**踩坑实录**：OpenWrt 新版 opkg（`opkg version 80503d94... (2025-11-05)`，Kwrt 25.12 自带）
**完全忽略 `opkg install /path/x.ipk` 的本地路径**，会按包名去 feed 下载。
现场表现：
```
$ opkg install ./ruby-pstore_3.4.9-r1_aarch64_cortex-a53.ipk
Installing ruby-pstore (3.4.9-r1) to root...
 * opkg_download: Failed to download https://dl.openwrt.ai/.../ruby-pstore_....ipk, wget returned 4.
```
文件明明就在当前目录，opkg 还是去联网下 → 源一抖就装不上。

**为什么必须离线装**：`dl.openwrt.ai` 从国内该网络**直连极不稳定**（实测连发 3 次：1 次 200/2.3s、
2 次 30s 超时）。大包（openclash 8.17MB）几乎必然中途失败。

### 标准流程（本机下载 → 本地源 → opkg 安装）

**① 本机（有稳定网络）下载全部 ipk**
```sh
# 从路由器取精确文件名：opkg info <pkg> | grep -E '^(Version|Filename|Size):'
curl -sSL --retry 3 -o x.ipk "https://dl.openwrt.ai/releases/25.12/packages/aarch64_cortex-a53/packages/<filename>"
```
推送到路由器：`python rput.py <local> /tmp/ipk/<name>`（见 §0 助手脚本）

**② 在路由器上生成 Packages 索引 —— 最大的坑在这里**
```sh
cd /tmp/ipk; mkdir -p /tmp/localfeed; cp -f *.ipk /tmp/localfeed/; cd /tmp/localfeed
: > Packages
for f in *.ipk; do
  rm -rf /tmp/ctl; mkdir -p /tmp/ctl
  tar -xzf "$f" -C /tmp/ctl 2>/dev/null
  [ -f /tmp/ctl/control.tar.gz ] && tar -xzf /tmp/ctl/control.tar.gz -C /tmp/ctl
  CP=/tmp/ctl/control; [ -f /tmp/ctl/./control ] && CP=/tmp/ctl/./control
  awk 'NF' "$CP" >> Packages                        # ★ 关键：压掉所有空行
  printf 'Filename: %s\n' "$f" >> Packages
  printf 'Size: %s\n' "$(stat -c %s "$f")" >> Packages
  printf '\n' >> Packages                           # ★ 只在段落末尾留一个空行
done
gzip -kf Packages
```
> **为什么必须 `awk 'NF'`**：control 文件本身**以空行结尾**。直接 `cat control >> Packages`
> 再追加 `Filename:`，就会在中间插入一个空行 → opkg 的段落解析器在此断开，
> `Filename` 落到"没有 Package 字段的新段落"里被丢弃 →
> 报 **`Package xxx does not have a valid filename field`**。
> 症状极易误判为"索引没生成"，其实 `grep Filename Packages.gz` 是看得到的。**必须有且只有一个段落分隔空行。**

**③ 用 nginx 暴露（`file://` 未验证，走 http 最稳）**
```sh
ln -s /tmp/localfeed /www/localfeed
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/localfeed/Packages.gz   # 期望 200
```

**④ 临时把源切到本地（原文件备好以便还原）**
```sh
cp -f /etc/opkg/compatfeeds.conf /tmp/compatfeeds.conf.bak
echo '# temporarily disabled' > /etc/opkg/compatfeeds.conf      # 否则 istore_compat 也会联网拖慢
rm -f /etc/opkg/distfeeds.conf                                  # 软链挡住写入，必须先删
printf 'src/gz localfeed http://127.0.0.1/localfeed\n' > /etc/opkg/distfeeds.conf
rm -f /var/lock/opkg.lock; killall -9 opkg 2>/dev/null          # 清掉上一步卡死留下的锁
opkg update && opkg install luci-app-openclash
```

**⑤ 收尾（必做，别忘）**
```sh
rm -f /www/localfeed                       # ★ 不删等于把 /tmp 挂在 web 上
rm -f /etc/opkg/distfeeds.conf
ln -sf /rom/etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf
cp -f /tmp/compatfeeds.conf.bak /etc/opkg/compatfeeds.conf
rm -rf /tmp/ipk /tmp/localfeed /tmp/ctl
```

**opkg 卡死排查**：报 `Could not lock /var/lock/opkg.lock: Resource temporarily unavailable`
= 上一个 opkg 还挂在下载重试里 → `killall -9 opkg; rm -f /var/lock/opkg.lock`。

### luci-app-openclash 的依赖账（便于提前估空间）
`Depends: libc, dnsmasq-full, bash, curl, ca-bundle, ip-full, ruby, ruby-yaml, kmod-tun, unzip`
其中 ruby 链会自动拉 `libruby3.4 / ruby-digest / ruby-enc / ruby-pstore / ruby-psych /
ruby-yaml / ruby-stringio / ruby-date / ruby-bigdecimal`。
`ruby-pstore`、`ruby-yaml` 这两个小包（7.8KB/5.5KB）**最容易被源抖动卡住**，务必纳入本地源。
安装体量：openclash 本体 ≈22.1MB + ruby 链 ≈6MB + unzip 0.2MB ≈ **28MB**。

---

## 5. 空间治理与「要不要加 U 盘」

**先把账算清楚再说结论**（RAX3000M 类机型，两代固件实测）：

| 卷 | 老固件 25.12.0-rc2 | 新固件 25.12-SNAPSHOT 09.25 |
|---|---|---|
| ubi（总） | 114M | **110.4MiB = 912 LEB**（LEB 124KiB） |
| kernel | 5.8M | 4.6MiB（38 LEB） |
| rootfs（只读 squashfs） | 69.3M | **41.1MiB（340 LEB）** |
| rootfs_data（唯一可写） | 32.4M | **61.6MiB（509 LEB）→ UBIFS 60MiB，可用 55.1M** |
| **available LEBs** | **0** | **0** |

**结论口径（用户问"55M 能不能调大一点"时的标准回答）**
- 用户看到的"55M"= `df -h /overlay` 的 rootfs_data。厂商在新固件里**精简了 rootfs（69.3M→41.1M）**，
  把可写区从 32MB 提到了 55MB —— **这已经是厂商侧的极限**。
- `ubinfo -a` 的 `Amount of available logical eraseblocks: 0` → **三卷已把 NAND 分完，物理上无法再扩**。
  想靠"缩 rootfs 卷把 LEB 让给 rootfs_data"需要从 u-boot/临时系统重建在跑的 rootfs 卷，**自毁级风险，不做**。
- 真正的扩容方案只有一个：**U 盘 + extroot**。
  RAX3000M 的 Kwrt 已内置全套驱动与工具，插上就能用：
  `kmod-usb3 / kmod-usb-xhci-mtk / kmod-usb-storage / kmod-usb-storage-uas / block-mount / kmod-fs-ext4`，
  且默认装有 `luci-app-diskman`、`luci-app-partexp`、`luci-app-istorex` → LuCI 里可直接做外置 overlay / 沙箱。

**"够不够"的判断基准**
| 场景 | 结论 |
|---|---|
| 只放订阅/规则/配置 | 不用 U 盘，30MB 余量绰绰有余 |
| 装 OpenClash（≈28MB 含 ruby 链） | 55M 版装完余 **≈28M**；32M 老版**装不下**，必须 U 盘 |
| 想装 Docker、多套代理、多语言包 | **必须 U 盘 extroot** |

内存侧：512MB 机型 `free -m` total ≈ 478~489MB，OpenClash 跑起来后 available ≈ 200MB，**够用**；
想再留余量就开固件自带的 `/etc/init.d/zram`，不必为内存插盘。

---

## 6. 交付/汇报口径

多步任务按 **任务编号 → 根因 → 动作 → 验证证据** 汇报，每条给出可复现命令。
用户常会中途贴报错回来，**先复现再解释**，不要凭猜。
涉及删包/改网络配置/改密码，先给推荐项让用户拍板；纯清理动作直接做完再报。

---

## 7. 本次实战数据（可作为基线）

**机型**：CMCC RAX3000M (NAND) / MediaTek MT7981 Filogic / aarch64_cortex-a53 / 512MB RAM / 128MB NAND

| 项目 | 25.12.0-rc2 (01.10.2026) | 25.12-SNAPSHOT (09.25.2026) |
|---|---|---|
| 内核 | 6.12.63 | 6.12.108 |
| 已装包数 | 377+ | 377 |
| overlay 可用 | 9.5M（治理后） | **28.1M**（装完 OpenClash 后） |
| OpenClash | 0.47.028-r30（镜像自带） | **0.47.156-r8**（需另装，≈22.1MB） |
| quickstart | 0.11.13 | 0.12.10-r1 |
| CPU 待机 | 63-64°C | — |

**OpenClash 跑通后的验收基线**（可直接对照）
```
enable=1 / rc.d/S99openclash 自启 / PIDs: clash + openclash_watchdog
监听: 7890 7891 7892 7893 7895 7874 9090
/tmp/openclash_start.log 末尾 = [Tip] OpenClash Start Successful!
dnsmasq: noresolv=1 + server=127.0.0.1#7874 → nslookup 返回 198.18.x.x（fake-ip）即为接管成功
出口: google 204 / youtube 200 / github 200 / baidu 200 in 0.12s（Direct）
```

**公共镜像 404 实证（真凶 A 的铁证，可直接复述给用户）**
```
dl.openwrt.ai/releases/25.12/targets/mediatek/filogic/6.12.63/Packages.gz   → 200  0.69s
mirrors.aliyun.com/openwrt/...                                              → 404  0.16s
mirrors.ustc.edu.cn/openwrt/...                                              → 404  0.17s
mirrors.tuna.tsinghua.edu.cn/openwrt/...                                     → 403  0.25s
downloads.openwrt.org/openwrt/...                                            → 404  0.85s
```

**本机固件自带的 8 家候选镜像（`guide/soft-source/list/` 返回）**
OpenWRT(HTTP) / OpenWRT(HTTPS) / 高校联合镜像站(CERNET) / 南方科技大学 / 清华大学 /
中国科学技术大学 / 阿里云 / 腾讯云 —— 全部是官方发行版镜像，**没有一家有 25.12**。
所以"自动选择最优镜像"在 Kwrt 25.12 上必然是负优化。

**"内核无法更新" ≠ "内核旧"**：`/etc/openclash/core/clash_meta` 本身可以是新版
（本例 Mihomo Meta `alpha-ge183c58`, 2026-08-10，`-v` 可验），所谓"无法更新"只是版本查询被墙。

### 交付物清单（本项目 `_work/`）
`rssh.py`（批量执行/推脚本）· `rput.py`（exec+stdin 上传，校验远端字节数）· `rget.py`（拉回文件）
每次任务前用 `survey_*.sh` 侦察、`diag_*.sh` 定位、`step_*.sh` 动手、`verify_*.sh` 验收 —— 四段式脚本命名，便于复用与回溯。

