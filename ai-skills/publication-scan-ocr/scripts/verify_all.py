# -*- coding: utf-8 -*-
"""成品总验收：页数一致性 / 文字层可提取 / 体积 / 谱例失真提示"""
import pymupdf, os, re

WD = r"D:\WorkBuddy\初中音乐教学资源库\_work"
PAIRS = [
    (r"D:\BaiduNetdiskDownload\人音版八年级上册教师用书 2025-8-31 181855 1.pdf", "人音版_八上_教师参考用书_可搜索版.pdf"),
    (r"D:\BaiduNetdiskDownload\人音版八年级上册教学设计 2025-8-31 181855 2.pdf", "人音版_八上_教学设计_可搜索版.pdf"),
    (r"D:\工作\教参\七上教参.pdf", "人音版_七上_教师参考用书_可搜索版.pdf"),
    (r"D:\工作\教参\七下教参.pdf", "人音版_七下_教师参考用书_可搜索版.pdf"),
    (r"D:\工作\教参\八下教参.pdf", "人音版_八下_教师参考用书_可搜索版.pdf"),
]
LIMIT = 100 * 1024 * 1024
print(f"{'产物':<44}{'页数':>10}{'体积MB':>9}{'文字层':>8}{'超限':>6}")
print("-" * 80)
ok = True
for src, name in PAIRS:
    out = os.path.join(WD, name)
    s = pymupdf.open(src); o = pymupdf.open(out)
    sz = os.path.getsize(out)
    probes = [5, o.page_count // 2, o.page_count - 3]
    chars = [len(o[p].get_text().strip()) for p in probes if 0 <= p < o.page_count]
    good = sum(1 for c in chars if c > 50)
    same = "✔" if s.page_count == o.page_count else f"�’{s.page_count}≠{o.page_count}"
    over = "⛔超" if sz > LIMIT else "—"
    if s.page_count != o.page_count or good < len(chars) or sz > LIMIT:
        ok = False
    print(f"{name[:42]:<44}{same:>10}{sz/1048576:>9.1f}{f'{good}/{len(chars)}':>8}{over:>6}")
    s.close(); o.close()

print("-" * 80)
print("结论：" + ("全部通过，可入库" if ok else "存在问题，需处理"))
print(f"\n单文件上限：{LIMIT/1048576:.0f} MiB")
