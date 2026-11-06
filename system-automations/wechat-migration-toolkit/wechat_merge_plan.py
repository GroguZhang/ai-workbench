# -*- coding: utf-8 -*-
# 微信老库 vs 新库 全量比对，生成合并计划。带指纹缓存，可断点续跑。
import os, sys, io, json, hashlib, collections
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH = 16 * 1024           # 首尾各 16KB（针对 13 万个 .dat 小文件优化，精度足够）
CACHE = os.path.join(BASE, 'wechat_fp_cache.json')
PLAN = os.path.join(BASE, 'wechat_merge_plan.json')


def human(n):
    for u in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return '%.1f%s' % (n, u)
        n /= 1024.0
    return '%.1fPB' % n


def fingerprint(path, size):
    """size + 首尾各 16KB 的 sha1；不足 32KB 读全部"""
    h = hashlib.sha1()
    h.update(str(size).encode())
    try:
        with open(path, 'rb') as f:
            if size <= CH * 2:
                h.update(f.read())
            else:
                h.update(f.read(CH))
                f.seek(-CH, os.SEEK_END)
                h.update(f.read(CH))
    except Exception:
        return None
    return h.hexdigest()


old = json.load(io.open(os.path.join(BASE, 'wechat_old.json'), encoding='utf-8'))
new = json.load(io.open(os.path.join(BASE, 'wechat_new.json'), encoding='utf-8'))
print('老库 %d 文件 / %s' % (len(old), human(sum(r[1] for r in old))))
print('新库 %d 文件 / %s' % (len(new), human(sum(r[1] for r in new))), flush=True)

new_by_size = collections.defaultdict(list)
for p, s, e in new:
    if s > 0:
        new_by_size[s].append(p)

needed_sizes = set(s for p, s, e in old if s > 0 and s in new_by_size)
need_paths = []
for s in needed_sizes:
    need_paths.extend(new_by_size[s])
print('需比对新库文件 %d 个（%d 种尺寸）' % (len(need_paths), len(needed_sizes)), flush=True)

# ---------- 指纹缓存（断点续跑）----------
cache = {}
if os.path.exists(CACHE):
    try:
        cache = json.load(io.open(CACHE, encoding='utf-8'))
        print('已加载缓存 %d 条' % len(cache), flush=True)
    except Exception:
        cache = {}

todo = [p for p in need_paths if p not in cache]
print('待计算指纹 %d 个' % len(todo), flush=True)

n = 0
for p in todo:
    try:
        s = os.path.getsize(p)
    except Exception:
        continue
    k = fingerprint(p, s)
    if k:
        cache[p] = k
    n += 1
    if n % 2000 == 0:
        json.dump(cache, io.open(CACHE, 'w', encoding='utf-8'))
        print('  进度 %d/%d' % (n, len(todo)), flush=True)

json.dump(cache, io.open(CACHE, 'w', encoding='utf-8'))
print('指纹完成，共 %d 条' % len(cache), flush=True)

# ---------- 新库指纹反查表 ----------
fp2new = collections.defaultdict(list)
for p in need_paths:
    k = cache.get(p)
    if k:
        fp2new[k].append(p)

# ---------- 逐文件判定老库 ----------
dup = []
uniq = []
for p, s, e in old:
    if s == 0:
        dup.append({'p': p, 's': s, 'f': '', 'm': ''})
        continue
    if s not in new_by_size:
        uniq.append({'p': p, 's': s})
        continue
    k = fingerprint(p, s)
    if k and k in fp2new:
        dup.append({'p': p, 's': s, 'f': k, 'm': fp2new[k][0]})
    else:
        uniq.append({'p': p, 's': s})

dup_bytes = sum(x['s'] for x in dup)
uniq_bytes = sum(x['s'] for x in uniq)
print('\n=== 判定结果 ===')
print('  与新库重复可删：%6d 个 / %s' % (len(dup), human(dup_bytes)))
print('  老库独有需保留：%6d 个 / %s' % (len(uniq), human(uniq_bytes)))

# 按扩展名统计
old_ext = {p: e for p, s, e in old}
for label, rows, key in [('可删', dup, 'p'), ('独有', uniq, 'p')]:
    agg = collections.defaultdict(lambda: [0, 0])
    for x in rows:
        e = old_ext.get(x[key], '') or '(无扩展名)'
        agg[e][0] += 1
        agg[e][1] += x['s']
    print('\n=== %s内容按类型 ===' % label)
    for e, (c, s) in sorted(agg.items(), key=lambda kv: -kv[1][1])[:12]:
        print('  %-14s %7d 个  %10s' % (e, c, human(s)))

print('\n=== 老库独有大文件 Top 20 ===')
for x in sorted(uniq, key=lambda y: -y['s'])[:20]:
    print('  %10s  %s' % (human(x['s']), x['p']))

out = {
    'old_root': os.environ.get('WECHAT_OLD', r'D:\WeChat Files\<your_wxid>'),
    'new_root': os.environ.get('WECHAT_NEW', r'D:\xwechat_files\<your_wxid>_bc06'),
    'dup_bytes': dup_bytes,
    'uniq_bytes': uniq_bytes,
    'dup': dup,
    'uniq': uniq,
}
json.dump(out, io.open(PLAN, 'w', encoding='utf-8'), ensure_ascii=False)
print('\n-> wechat_merge_plan.json  (%d 可删 / %d 独有)' % (len(dup), len(uniq)))
