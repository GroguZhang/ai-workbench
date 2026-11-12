# -*- coding: utf-8 -*-
"""按 volumes_data.py 生成：分单元 Markdown + 各册曲目与课时索引 + 总册次索引"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volumes_data import VOLUMES, FUTURE_UNITS

WD = r"D:\WorkBuddy\初中音乐教学资源库\_work"
OUT = os.path.join(WD, "out")
UNIT_LABEL = ["第一单元", "第二单元", "第三单元", "第四单元", "第五单元", "第六单元"]


def load_pages(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    pages, cur = {}, 0
    for line in raw.split("\n"):
        m = re.match(r"^===== PAGE (\d+) =====$", line.strip())
        if m:
            cur = int(m.group(1)); pages[cur] = []
        elif cur:
            pages[cur].append(line)
    return {k: [x for x in v if x.strip()] for k, v in pages.items() if v}


def clean_lines(lines, book):
    drop = {"扫描全能王", "创建", "目录", "目录l", "目录1", "3亿人都在用的扫描App", book}
    out = []
    for ln in lines:
        s = ln.strip()
        if s in drop:
            continue
        s = re.sub(r"^[义义夯救育自格科实务美务养新科书务教育教科书]+[·・]音乐　?教师用书", "", s).strip()
        s = re.sub(r"^义务教育教科书艺术[·・]音乐教师用书", "", s).strip()
        if s and not re.fullmatch(r"[0-9IVX\s]{1,4}", s):
            out.append(s)
    return out


def find_pdf_pages(pages, kw, skip_until, limit=2):
    hits = [p for p in sorted(pages) if p > skip_until and any(kw in ln for ln in pages[p])]
    if not hits:
        return []
    segs, a, b = [], hits[0], hits[0]
    for x in hits[1:]:
        if x - b <= 2:
            b = x
        else:
            segs.append((a, b)); a = b = x
    segs.append((a, b))
    return segs[:limit]


def fmt(segs):
    return "、".join(f"{a}–{b}" if b > a else str(a) for a, b in segs) or "—"


os.makedirs(OUT, exist_ok=True)
summary_rows = []

for key, V in VOLUMES.items():
    pages = load_pages(os.path.join(WD, V["stem"] + "_全文.txt"))
    off, book, toc_end = V["offset"], V["book"], V["toc_pdf"][1]
    units = list(V["units"])
    if V.get("appendix"):
        a = V["appendix"]
        units.append(("附录", a[0], a[1], a[2]))

    # ---- 分单元 Markdown ----
    udir = os.path.join(OUT, "分单元", V["stem"])
    os.makedirs(udir, exist_ok=True)
    for label, title, bp0, bp1 in units:
        p0, p1 = min(bp0 + off, V["pdf_pages"]), min(bp1 + off, V["pdf_pages"])
        head = f"# 人音版{book} · {label} {title}" if title != "附录" else f"# 人音版{book} · 附录"
        buf = [head, "",
               f"> 来源：《义务教育教科书·艺术·音乐 教师用书（简线通用）》{book}",
               f"> 书内第 {bp0}–{bp1} 页　PDF 第 {p0}–{p1} 页　共 {p1-p0+1} 页", "",
               "> OCR 全文，正文可用；**谱例（五线谱/简谱）、节奏型与复杂表格会失真，须以 PDF 原页为准**。", "",
               "---", ""]
        for p in range(p0, p1 + 1):
            buf += [f"## 第 {p} 页（书内 {p-off}）", ""] + clean_lines(pages.get(p, []), book) + [""]
        fn = f"人音版_{key}_{label}_{title}_教师参考用书.md" if title != "附录" else f"人音版_{key}_附录_教师参考用书.md"
        with open(os.path.join(udir, fn), "w", encoding="utf-8") as f:
            f.write("\n".join(buf))

    # ---- 各册曲目索引 ----
    L = [f"# 人音版 {book} · 曲目与相关知识索引", "",
         "> 依据《义务教育教科书·艺术·音乐 教师用书（简线通用）》目录原页逐条核对整理。",
         f"> 「书内页」= 教参自印页码；「PDF 页」= 成品 PDF 实际页码，换算关系 **PDF 页 = 书内页 + {off}**。", "",
         "## 一、单元与曲目", "",
         "| 单元 | 环节 | 曲目 | 书内页 | PDF 页 |", "|---|---|---|---|---|"]
    for unit, kind, title, bp in V["works"]:
        L.append(f"| {unit} | {kind} | 《{title}》 | {bp} | {bp + off} |")

    if V.get("knowledge"):
        L += ["", "## 二、相关知识条目", "", "| 单元 | 相关知识 | 书内页 |", "|---|---|---|"]
        for unit, k, bp in V["knowledge"]:
            L.append(f"| {unit} | {k} | {bp} |")

    L += ["", "## 三、单元结构（教参统一体例）", "",
          "`单元说明` → `教学目标` → `教材分析` → `相关知识` → `教学建议`（课时分配 + 教学实施） → `评价建议`", "",
          "| 单元 | 书内页 | PDF 页 |", "|---|---|---|"]
    for label, title, bp0, bp1 in units:
        p0, p1 = min(bp0 + off, V["pdf_pages"]), min(bp1 + off, V["pdf_pages"])
        nm = f"{label} {title}" if title != "附录" else "附录"
        L.append(f"| {nm} | {bp0}–{bp1} | {p0}–{p1} |")

    idx_name = f"人音版_{book}_曲目与相关知识索引.md"
    with open(os.path.join(OUT, idx_name), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    n_unit_pages = V["pdf_pages"]
    summary_rows.append((book, key, n_unit_pages, len(V["works"]),
                         len(V.get("knowledge") or []), idx_name))
    print(f"[{key}] {book}: {len(V['works'])} 曲目, {len(V.get('knowledge') or [])} 知识条目, "
          f"分单元 {len(units)} 个文件 -> {idx_name}")

# ---- 总册次索引 ----
done = {k: v for k, v in VOLUMES.items()}
files = {}
for fn in os.listdir(OUT):
    if fn.endswith(".md"):
        files[fn] = fn

L = ["# 人音版初中音乐 · 册次与资源总索引", "",
     "> 人音版《义务教育教科书·艺术·音乐》（2024 修订版，主编 赵季平、杜永寿）教师用书，**简线通用**。",
     "> 初中段共 6 册。单元名以七上教参序言（杜永寿《\"人音版\"义务教育音乐新教材解读》）为准，",
     "> 曲目名与页码逐条对照各册目录原页核对。", "",
     "## 一、入库完成度", "",
     "| 册次 | 教师用书 | PDF 页数 | 曲目数 | 知识条目 | 曲目索引 |",
     "|---|---|---|---|---|---|"]
order = ["七上", "七下", "八上", "八下", "九上", "九下"]
book_name = {"七上": "七年级上册", "七下": "七年级下册", "八上": "八年级上册",
             "八下": "八年级下册", "九上": "九年级上册", "九下": "九年级下册"}
for k in order:
    bn = book_name[k]
    if k in done:
        V = done[k]
        L.append(f"| **{bn}** | ✅ {V['pdf_pages']} 页 | {V['pdf_pages']} | {len(V['works'])} | "
                 f"{len(V.get('knowledge') or [])} | ✅ |")
    else:
        L.append(f"| {bn} | ⬜ 待补 | — | — | — | ⬜ |")

L += ["", "## 二、各册单元一览", "",
      "| 册次 | 第一单元 | 第二单元 | 第三单元 | 第四单元 | 第五单元 | 第六单元 |",
      "|---|---|---|---|---|---|---|"]
for k in order:
    bn = book_name[k]
    if k in done:
        t = [u[1] for u in done[k]["units"]]
    else:
        t = FUTURE_UNITS.get(k, ["—"] * 6)
    L.append(f"| {bn} | " + " | ".join(t) + " |")

L += ["", "## 三、页码换算", "",
      "| 册次 | 教参书内页 → 成品 PDF 页 | 说明 |", "|---|---|---|"]
for k in order:
    if k in done:
        L.append(f"| {book_name[k]} | PDF 页 = 书内页 + {done[k]['offset']} | 书前含扉页/序言/目录共 {done[k]['offset']} 页 |")
    else:
        L.append(f"| {book_name[k]} | — | 待入库后测定 |")

L += ["", "## 四、教学框架（2022 版艺术课标）", "",
      "四个艺术实践：**欣赏 · 表现 · 创造 · 联系**　｜　四大核心素养：**审美感知 · 艺术表现 · 创意实践 · 文化理解**", "",
      "教参每单元固定体例：`单元说明` → `教学目标` → `教材分析` → `相关知识` → `教学建议` → `评价建议`"]

with open(os.path.join(OUT, "人音版初中音乐_册次索引.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(L) + "\n")
print("\n[总索引] 人音版初中音乐_册次索引.md 已生成")
