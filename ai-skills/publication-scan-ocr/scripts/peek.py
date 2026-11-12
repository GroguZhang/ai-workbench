# -*- coding: utf-8 -*-
"""定点查看某册某 PDF 页的教参原文（去噪、保留结构）。"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volumes_data import VOLUMES

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_RE = re.compile(r"=====\s*PAGE\s+(\d+)\s*=====")
STEM = {"七上": "人音版_七上_教师参考用书", "七下": "人音版_七下_教师参考用书",
        "八上": "人音版_八上_教师参考用书", "八上设计": "人音版_八上_教学设计",
        "八下": "人音版_八下_教师参考用书"}


def pages_of(stem):
    raw = open(os.path.join(HERE, f"{stem}_全文.txt"), encoding="utf-8").read()
    p = PAGE_RE.split(raw)
    return {int(p[i]): p[i + 1] for i in range(1, len(p) - 1, 2)}


if __name__ == "__main__":
    vol, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    pg = pages_of(STEM[vol])
    for n in range(a, b + 1):
        t = pg.get(n, "(缺页)")
        t = re.sub(r"扫描全能王|3亿人都在用的扫描App", "", t)
        t = re.sub(r"\n{2,}", "\n", t)
        print(f"\n=========== {vol} PDF p{n} ===========")
        print(t.strip()[:2600])
