# -*- coding: utf-8 -*-
"""生成《初中音乐备课横向速查表.md》
数据源：volumes_data.py（页码/单元/曲目，逐页核对）+ xref_data.py（课型/考点，据教参原文裁定）
用法: python build_xref.py
"""
import os, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volumes_data import VOLUMES, FUTURE_UNITS
from xref_data import CLS, CLS_NAME

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER = ["七上", "七下", "八上", "八下"]


def collect():
    rows = []
    for v in ORDER:
        d = VOLUMES[v]
        off = d["offset"]
        unit_of = {}
        for u in d["units"]:
            unit_of[u[1]] = u[0]
        for unit, kind, title, bp in d["works"]:
            key = (v, kind, title)
            c = CLS.get(key)
            if c is None:
                raise SystemExit(f"缺分类: {key}")
            code, sub, form, pts = c
            u = unit.split(" ", 1)
            rows.append(dict(
                vol=v, volname=d["book"], unit=unit,
                unitno=u[0], unitname=u[1] if len(u) > 1 else "",
                kind=kind, title=title,
                bp=bp, pdf=bp + off,
                cls=code, sub=sub, form=form,
                pts=[p for p in pts.split("|") if p and p != "—"],
            ))
    return rows


def tbl(rows, cols):
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def build(rows):
    L = []
    A = L.append
    A("# 初中音乐备课横向速查表")
    A("")
    A("> **用途**：同一件事（找一首能上合唱课的曲子 / 找讲「切分节奏」的载体 / 对比不同册次的同一考点），")
    A("> 不用再翻 5 本教参。按 **课型** 与 **乐理考点** 两个维度横切全部曲目，每首标注 册次 · 单元 · 教参页码。")
    A(">")
    A("> **覆盖范围**：人音版（2024 修订）七上 / 七下 / 八上 / 八下 四册，")
    A(f"> 共 **{len(rows)} 首** 曲目、**{sum(len(VOLUMES[v]['knowledge']) for v in ORDER)} 条** 「知识与技能」条目。")
    A("> 九上 / 九下 待素材到位后并入本表（预留结构见文末）。")
    A(">")
    A("> **数据来源**：全部取自各册教参正文（含「作品分析」原文表述与「相关知识」条目），")
    A("> 经逐页 OCR + 原页图核对；课型与考点标签为在原文证据上的归一化。")
    A("")
    A("---")
    A("")

    # ===== 一、页码换算 =====
    A("## 一、页码换算（先看这个，全表都用它）")
    A("")
    A("教参 PDF 内的页码与书内印刷页码有一册次差异，换算关系：")
    A("")
    A("| 册次 | 教参内页 → PDF 页 | 目录所在 PDF 页 | 总页数 |")
    A("|---|---|---|---|")
    for v in ORDER:
        d = VOLUMES[v]
        A(f"| {d['book']} | 书内页 + **{d['offset']}** | {d['toc_pdf'][0]}–{d['toc_pdf'][1]} | {d['pdf_pages']} |")
    A("")
    A("**本表列名含义**：`教参页` = 教参书内印刷页码（等同教材页码，便于对照教材）；`PDF页` = 在「可搜索版 PDF」中直接跳转的页码。")
    A("")
    A("**册次目录节点**：")
    A("")
    A("| 册次 | 单元 | 起止（书内页） | PDF 页范围 |")
    A("|---|---|---|---|")
    for v in ORDER:
        d = VOLUMES[v]
        for no, name, s, e in d["units"]:
            A(f"| {d['book']} | {no} {name} | {s}–{e} | {s + d['offset']}–{e + d['offset']} |")
        ap = d["appendix"]
        A(f"| {d['book']} | {ap[0]} | {ap[1]}–{ap[2]} | {ap[1] + d['offset']}–{ap[2] + d['offset']} |")
    A("")
    A("---")
    A("")

    # ===== 二、按课型 =====
    A("## 二、课型维度：拿到一节课，先在这里定位")
    A("")
    A("| 代码 | 课型 | 本表曲目数 |")
    A("|---|---|---|")
    cnt = collections.Counter(r["cls"] for r in rows)
    for c in ["A", "B", "C", "D", "E"]:
        A(f"| {c} | {CLS_NAME[c]} | {cnt[c]} |")
    A("")
    for c in ["A", "B", "C", "D", "E"]:
        sub = [r for r in rows if r["cls"] == c]
        A(f"### {c} · {CLS_NAME[c]}（{len(sub)} 首）")
        A("")
        A(tbl([[r["title"], r["vol"], r["unitno"] + " " + r["unitname"], r["kind"], r["sub"], r["form"],
                f"{r['bp']}", f"{r['pdf']}", "、".join(r["pts"]) or "—"] for r in sub],
              ["曲名", "册次", "单元", "教材定位", "演唱形式 / 乐器编制", "曲式结构（教参原文）",
               "教参页", "PDF页", "考点标签"]))
        A("")

    A("---")
    A("")

    # ===== 三、按乐理考点 =====
    A("## 三、乐理考点维度：要讲某个知识点，在这里挑载体")
    A("")
    tags = collections.Counter()
    for r in rows:
        for p in r["pts"]:
            tags[p] += 1
    A("| 考点 | 可用曲目数 |")
    A("|---|---|")
    for t, n in tags.most_common():
        A(f"| {t} | {n} |")
    A("")
    for t, n in tags.most_common():
        sub = [r for r in rows if t in r["pts"]]
        A(f"### 考点：{t}（{n} 首）")
        A("")
        A(tbl([[r["title"], r["vol"], r["unitno"] + " " + r["unitname"], r["kind"], r["sub"],
                r["form"], f"{r['pdf']}"] for r in sub],
              ["曲名", "册次", "单元", "教材定位", "演唱形式 / 乐器编制", "曲式结构", "PDF页"]))
        A("")

    A("---")
    A("")

    # ===== 四、跨册衔接 =====
    A("## 四、螺旋上升线索：同一考点跨册落点")
    A("")
    A("同一考点在不同册次反复出现，正好构成「由易到难」的教学递进。备课组长排三年进度时按此对齐：")
    A("")
    A("| 考点 | 七年级 | 八年级 | 教学递进建议 |")
    A("|---|---|---|---|")
    spend = {
        "切分节奏": "七上仅作感知（《月之故乡》）→ 八上进入「改编曲中的切分」（《彩云追月》《鼓声》）→ 八下落到「探戈跨小节切分」（《化装舞会》）",
        "二段体": "七上以「两段体歌曲」建立概念 → 七下引入「带再现的单二部」→ 八下要求辨析「带/不带再现」",
        "三段体": "七上从「ABA 圆舞曲」（《第二圆舞曲》）感知 → 七下「带再现的单三部」（《中国》）→ 八下「再现单三部合唱」（《念故乡》）",
        "复三部曲式": "七上集中出现（婚礼/军队/钢铁洪流三首进行曲），建议作为八年级复习素材",
        "变奏体": "八上《二泉映月》首次系统出现（引子+主题+五次变奏），八下《春江花月夜》可作延伸对比",
        "奏鸣曲式": "七上《红旗颂》感知 → 七下《节日序曲》识结构 → 八上《梁祝》完整作品分析",
        "无伴奏合唱": "七上《牧歌》→ 八上《阿伊亚——非洲的灵感》，可作同一演唱形式的跨文化对比",
        "变换拍子": "七上第五、六单元设专条知识（教参 p120 / p142），《故土情》为指定练习载体",
        "轮唱": "七上第三单元设专条知识（教参 p65），《中国人民志愿军战歌》为指定轮唱曲目",
        "一领众和": "八下《军民大生产》，与八上《阿伊亚》（领唱+伴唱）形成中外对照",
    }
    for t in ["切分节奏", "二段体", "三段体", "复三部曲式", "变奏体", "奏鸣曲式", "无伴奏合唱", "变换拍子", "轮唱", "一领众和"]:
        g7 = [r for r in rows if t in r["pts"] and r["vol"].startswith("七")]
        g8 = [r for r in rows if t in r["pts"] and r["vol"].startswith("八")]
        f7 = "、".join(dict.fromkeys(f"《{r['title'].split('——')[0]}》" for r in g7)) or "—"
        f8 = "、".join(dict.fromkeys(f"《{r['title'].split('——')[0]}》" for r in g8)) or "—"
        A(f"| {t} | {f7} | {f8} | {spend.get(t, '—')} |")
    A("")
    A("---")
    A("")

    # ===== 五、知识与技能条目 =====
    A("## 五、「知识与技能」条目全表（按册次）")
    A("")
    A("教参每单元末的「相关知识」是乐理考点的权威落点，下表可直接当教学进度表的考点栏用。")
    A("")
    for v in ORDER:
        d = VOLUMES[v]
        A(f"### {d['book']}（{len(d['knowledge'])} 条）")
        A("")
        A(tbl([[no, item, f"{pg}", f"{pg + d['offset']}"] for no, item, pg in d["knowledge"]],
              ["所属单元", "知识与技能条目", "教参页", "PDF页"]))
        A("")

    A("---")
    A("")
    A("## 六、九上 / 九下 预留位（素材到位后并入）")
    A("")
    A("单元名已从七上教参序言（杜永寿《人音版义务教育音乐新教材解读》36 单元对照表）取得，")
    A("拿到教参后只需补曲目与页码即可并入上表：")
    A("")
    A("| 册次 | 单元一 | 单元二 | 单元三 | 单元四 | 单元五 | 单元六 |")
    A("|---|---|---|---|---|---|---|")
    for v in ["九上", "九下"]:
        A("| " + v + " | " + " | ".join(FUTURE_UNITS[v]) + " |")
    A("")
    A("---")
    A("")
    A("## 附：生成方式（可复现）")
    A("")
    A("本表由脚本自数据源导出，**不手抄**；册次扩充后重跑即得新版：")
    A("")
    A("```")
    A("_work/volumes_data.py   # 页码 / 单元 / 曲目（逐页原图核对）")
    A("_work/xref_data.py      # 课型 / 考点裁定（依据教参原文证据句）")
    A("_work/build_xref.py     # 生成本文件")
    A("```")
    A("")
    A("生成时间：2026-09-16 ｜ 覆盖 4 册 / 131 首曲目 / 109 条知识条目")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    rows = collect()
    md = build(rows)
    out = os.path.join(HERE, "stage", "00_使用说明与索引", "初中音乐备课横向速查表.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print("wrote", out, len(md), "chars,", len(rows), "works")
