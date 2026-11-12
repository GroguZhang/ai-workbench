# -*- coding: utf-8 -*-
"""逐曲目输出「教参正文中对该曲目的定性描述」证据句，供人工判读课型。
用法: python dump_evidence.py 七上
"""
import os, re, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volumes_data import VOLUMES

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_RE = re.compile(r"=====\s*PAGE\s+(\d+)\s*=====")


def load_pages(p):
    raw = open(p, encoding="utf-8").read()
    parts = PAGE_RE.split(raw)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def main(key):
    v = VOLUMES[key]
    pages = load_pages(os.path.join(HERE, f"{v['stem']}_全文.txt"))
    off = v["offset"]
    works = sorted(v["works"], key=lambda w: w[3])
    for i, (unit, kind, title, bp) in enumerate(works):
        nxt = works[i + 1][3] if i + 1 < len(works) else bp + 6
        seg = "\n".join(pages.get(p, "") for p in range(bp + off, min(nxt, bp + 6) + off + 1))
        seg = re.sub(r"扫描全能王|3亿人都在用的扫描App", "", seg)
        seg = re.sub(r"[ \t]+", "", seg)
        flat = re.sub(r"\s+", "", seg)
        head = title.split("——")[0].split("（")[0]
        head = head[:max(3, len(head) - 1)]
        found = []
        for m in re.finditer(r"《" + re.escape(head[:4]), flat):
            found.append(flat[max(0, m.start() - 10):m.start() + 90])
            if len(found) >= 3:
                break
        print(f"\n### [{kind}] {title}  (教参p{bp}/PDF p{bp + off})  单元:{unit}")
        for f in found:
            print("   ▸", f)
        if not found:
            print("   ▸ (无《》命中) 首页片段:", flat[:120])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "七上")
