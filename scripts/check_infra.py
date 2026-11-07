#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基础设施不变式检查（docker-compose 层）。

这不是「风格检查」，而是把踩过的坑固化成断言：

1. **同一文件内宿主机端口不得重复绑**
   真实事故：scrapling 服务原本映射 8090:8090，而宿主机 8090 已被另一个
   常驻服务占用，容器起不来却看不出原因。写死端口的地方最容易撞。

2. **service 必须声明 restart 策略**
   个人自建服务没有运维值班，重启策略是唯一的自愈手段。

3. **compose 里不得出现字面量密钥**
   compose 是「示例配置」，一旦有人把真 Key 粘进 environment 再提交，
   就会绕过 .env 约定。历史上有两次真实 Key 就是这样差点进了仓库。

4. **env_file 指向的文件必须真实存在**
   指向不存在的路径会让 `docker compose up` 报一个与根因无关的错误。

用法：
    python scripts/check_infra.py            # 检查整个仓库
    python scripts/check_infra.py path/...   # 只检查指定目录
"""
import os
import re
import sys

# Windows 控制台默认 GBK，输出 ✓/✗ 会直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import yaml
except ImportError:
    print("需要 pyyaml：pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

SECRET_PATTERNS = [
    ("DeepSeek/OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    ("Google/Gemini Key", re.compile(r"AQ\.[A-Za-z0-9_\-]{30,}|AIza[0-9A-Za-z_\-]{30,}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{25,}")),
    ("企业微信 Webhook", re.compile(r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9\-]{10,}")),
    ("Bark 推送地址", re.compile(r"https://api\.day\.app/[A-Za-z0-9]{10,}")),
]
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def find_compose_files(roots):
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__"}]
            for name in filenames:
                if name in COMPOSE_NAMES:
                    found.append(os.path.join(dirpath, name))
    return sorted(found)


def iter_env_items(env):
    """environment 既可能是 dict 也可能是 ["K=V"] 列表。"""
    if isinstance(env, dict):
        for k, v in env.items():
            yield str(k), str(v)
    elif isinstance(env, list):
        for item in env:
            text = str(item)
            if "=" in text:
                k, v = text.split("=", 1)
                yield k, v


def check_file(path, problems, notices):
    rel = os.path.relpath(path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:
        problems.append("%s: YAML 解析失败 — %s" % (rel, exc))
        return

    if not isinstance(doc, dict) or "services" not in doc:
        problems.append("%s: 没有 services 段，可能不是 compose 文件" % rel)
        return

    base = os.path.dirname(path)
    host_ports = {}

    for name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue

        if not svc.get("restart"):
            problems.append("%s: service %r 缺少 restart 策略" % (rel, name))

        for key, value in iter_env_items(svc.get("environment")):
            for label, rx in SECRET_PATTERNS:
                if rx.search(value):
                    problems.append("%s: service %r 的 %s 含字面量密钥（%s），请改用环境变量"
                                    % (rel, name, key, label))

        env_file = svc.get("env_file")
        for ef in (env_file if isinstance(env_file, list) else [env_file] if env_file else []):
            target = os.path.join(base, str(ef))
            if os.path.exists(target):
                continue
            # .env 被 .gitignore 屏蔽是**设计如此**，仓库里只有 .env.example。
            # 新克隆的人尚未复制时，这不是配置错误，只需提示一句怎么补。
            example = target + ".example"
            if os.path.basename(str(ef)).startswith(".env") and os.path.exists(example):
                notices.append("%s: service %r 的 env_file %s 尚未创建，"
                               "请先 cp %s %s"
                               % (rel, name, ef, os.path.relpath(example), os.path.relpath(target)))
            else:
                problems.append("%s: service %r 的 env_file 不存在且无同名 .example 可参照 — %s"
                                % (rel, name, ef))

        for port in svc.get("ports") or []:
            text = str(port)
            host = text.split(":")[0].strip()
            if not host.isdigit():
                continue
            if host in host_ports:
                problems.append("%s: 宿主机端口 %s 被 %r 与 %r 重复绑定"
                                % (rel, host, host_ports[host], name))
            else:
                host_ports[host] = name


def main(argv):
    roots = argv[1:] or ["."]
    files = find_compose_files(roots)
    if not files:
        print("未找到任何 compose 文件")
        return 0

    problems = []
    notices = []
    for path in files:
        check_file(path, problems, notices)

    print("检查了 %d 个 compose 文件：" % len(files))
    for path in files:
        print("  - %s" % os.path.relpath(path))

    if notices:
        print("\n提示 %d 条（不阻塞）：" % len(notices))
        for n in notices:
            print("  · %s" % n)

    if problems:
        print("\n发现 %d 个问题：" % len(problems))
        for p in problems:
            print("  ✗ %s" % p)
        return 1

    print("\n全部通过：端口无冲突 / restart 策略齐全 / 无字面量密钥 / env_file 引用有效")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
