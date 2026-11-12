# -*- coding: utf-8 -*-
"""补生成《八年级上册 教学设计》的分单元 Markdown"""
import os, re

WD = r"D:\WorkBuddy\初中音乐教学资源库\_work"
OUT = os.path.join(WD, "stage", "01_教材与教参（人音版2024新版）", "八年级上册", "分单元", "教学设计")
os.makedirs(OUT, exist_ok=True)

UNITS = [
    ("第一单元", "百年风华", 3, 17, ["信仰之声", "南湖星火", "新中国与“英特纳雄耐尔”"]),
    ("第二单元", "银屏乐声", 18, 32, ["热血中的颂歌", "奋发中的乐章", "时代的呼唤"]),
    ("第三单元", "经典协奏", 33, 44, ["梁山伯与祝英台", "中国经典协奏曲巡礼"]),
    ("第四单元", "非洲鼓乐", 45, 59, ["解码非洲音乐", "跃动的鼓魂", "鼓声"]),
    ("第五单元", "国乐飘香（一）", 60, 81, ["寻韵", "叩境", "绘声"]),
    ("第六单元", "雅集流韵", 82, 98, ["诗词古韵", "琴深意重"]),
]

with open(os.path.join(WD, "人音版_八上_教学设计_全文.txt"), encoding="utf-8") as f:
    raw = f.read()
pages, cur = {}, 0
for line in raw.split("\n"):
    m = re.match(r"^===== PAGE (\d+) =====$", line.strip())
    if m:
        cur = int(m.group(1)); pages[cur] = []
    elif cur:
        pages[cur].append(line)

SKIP = {"扫描全能王", "创建", "目录", "目录1", "八年级上册"}
for label, title, p0, p1, lessons in UNITS:
    buf = [f"# 人音版八年级上册 · {label} {title}　教学设计", "",
           "> 来源：《义务教育教科书·艺术·音乐 教学设计》八年级上册",
           f"> 本单元 PDF 第 {p0}–{p1} 页　共 {p1-p0+1} 页", "",
           f"> 课时构成：{' / '.join(f'第{i}课时 {t}' for i, t in enumerate(lessons, 1))}", "",
           "> OCR 全文，正文可用；**谱例、节奏型与表格会失真，须以 PDF 原页为准**。", "",
           "---", ""]
    for p in range(p0, min(p1, 98) + 1):
        buf += [f"## 第 {p} 页", ""]
        for ln in pages.get(p, []):
            s = ln.strip()
            if s in SKIP or re.fullmatch(r"[0-9IVX\s]{1,4}", s):
                continue
            s = re.sub(r"^[实务美务养育教利容义务教育教科书]+艺术[·・]音乐\s*(数学餐计|教学设计)?", "", s).strip()
            if s:
                buf.append(s)
        buf.append("")
    fn = f"人音版_八上_{label}_{title}_教学设计.md"
    with open(os.path.join(OUT, fn), "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("写出", fn)

print("完成，共", len(UNITS), "个文件")
