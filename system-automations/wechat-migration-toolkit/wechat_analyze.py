# -*- coding: utf-8 -*-
# 微信老库/新库 结构对比与重叠规模分析（只读）
import os, sys, io, json, collections
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 改成你自己的路径，或用环境变量覆盖：
#   export WECHAT_OLD="D:\WeChat Files\<your_wxid>"
#   export WECHAT_NEW="D:\xwechat_files\<your_wxid>_bc06"
OLD = os.environ.get('WECHAT_OLD', r'D:\WeChat Files\<your_wxid>')
NEW = os.environ.get('WECHAT_NEW', r'D:\xwechat_files\<your_wxid>_bc06')


def human(n):
    for u in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return '%.1f%s' % (n, u)
        n /= 1024.0
    return '%.1fPB' % n


def collect(root):
    rows = []
    for dp, dns, fns in os.walk(root):
        for f in fns:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p, follow_symlinks=False)
                rows.append((p, st.st_size, os.path.splitext(f)[1].lower()))
            except Exception:
                pass
    return rows


print('枚举老库...')
old = collect(OLD)
print('  老库 %d 个文件 / %s' % (len(old), human(sum(r[1] for r in old))))
print('枚举新库...')
new = collect(NEW)
print('  新库 %d 个文件 / %s' % (len(new), human(sum(r[1] for r in new))))

print('\n=== 老库扩展名分布（按体积）===')
oe = collections.defaultdict(lambda: [0, 0])
for p, s, e in old:
    oe[e or '(无扩展名)'][0] += 1
    oe[e or '(无扩展名)'][1] += s
for e, (c, s) in sorted(oe.items(), key=lambda kv: -kv[1][1])[:18]:
    print('  %-14s %7d 个  %10s' % (e, c, human(s)))

print('\n=== 新库扩展名分布（按体积）===')
ne = collections.defaultdict(lambda: [0, 0])
for p, s, e in new:
    ne[e or '(无扩展名)'][0] += 1
    ne[e or '(无扩展名)'][1] += s
for e, (c, s) in sorted(ne.items(), key=lambda kv: -kv[1][1])[:18]:
    print('  %-14s %7d 个  %10s' % (e, c, human(s)))

# size 层面重叠
new_sizes = collections.Counter()
for p, s, e in new:
    if s > 0:
        new_sizes[s] += 1
old_sizes = collections.Counter()
for p, s, e in old:
    if s > 0:
        old_sizes[s] += 1

hit_files = 0
hit_bytes = 0
miss_files = 0
miss_bytes = 0
miss_rows = []
for p, s, e in old:
    if s == 0:
        continue
    if s in new_sizes:
        hit_files += 1
        hit_bytes += s
    else:
        miss_files += 1
        miss_bytes += s
        miss_rows.append((p, s))

print('\n=== size 层面比对（老库视角）===')
print('  新库存在同尺寸的文件 : %6d 个  %10s  (%.1f%%)' % (
    hit_files, human(hit_bytes), hit_bytes * 100.0 / max(1, sum(r[1] for r in old))))
print('  新库无同尺寸（必为独有）: %6d 个  %10s  (%.1f%%)' % (
    miss_files, human(miss_bytes), miss_bytes * 100.0 / max(1, sum(r[1] for r in old))))

print('\n=== 老库中「新库无同尺寸」的大文件 Top 30（确定独有）===')
for p, s in sorted(miss_rows, key=lambda x: -x[1])[:30]:
    print('  %10s  %s' % (human(s), p))

with io.open(os.path.join(BASE, 'wechat_old.json'), 'w', encoding='utf-8') as f:
    json.dump(old, f, ensure_ascii=False)
with io.open(os.path.join(BASE, 'wechat_new.json'), 'w', encoding='utf-8') as f:
    json.dump(new, f, ensure_ascii=False)
print('\n-> wechat_old.json / wechat_new.json')
