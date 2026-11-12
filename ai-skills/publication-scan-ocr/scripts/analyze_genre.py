# -*- coding: utf-8 -*-
"""为每首曲目定位「体裁定性」：抓取 《曲名》 前后 8 字的上下文，统计高频体裁词。
输出 _work/out/work_genre.tsv
"""
import os, re, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volumes_data import VOLUMES

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_RE = re.compile(r"=====\s*PAGE\s+(\d+)\s*=====")

def load_pages(p):
    raw = open(p, encoding="utf-8").read()
    parts = PAGE_RE.split(raw)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}

# 体裁关键词（按优先级判定）
GENRE_ORDER = [
    ("器乐·协奏", ["协奏曲"]),
    ("器乐·交响/管弦", ["交响曲", "交响诗", "交响组曲", "管弦乐", "管弦乐曲", "交响乐"]),
    ("器乐·民族管弦", ["民族管弦乐", "民乐合奏", "丝竹", "吹打", "吹歌", "广东音乐", "绛州鼓乐", "鼓乐"]),
    ("器乐·独奏", ["独奏曲", "二胡曲", "琵琶曲", "古筝曲", "笛子曲", "唢呐曲", "钢琴曲", "双排键", "马头琴曲"]),
    ("器乐·小型合奏", ["管乐合奏", "管乐", "合奏曲", "重奏", "四手联弹", "鼓乐合奏"]),
    ("器乐·钢琴", ["钢琴曲", "钢琴独奏", "钢琴作品"]),
    ("器乐·舞曲/序曲", ["圆舞曲", "波尔卡", "进行曲", "序曲", "舞曲"]),
    ("声乐·合唱", ["无伴奏合唱", "童声合唱", "混声合唱", "女声合唱", "男声合唱", "合唱曲", "合唱"]),
    ("声乐·独唱", ["女高音独唱", "男高音独唱", "独唱", "女声独唱", "男声独唱"]),
    ("戏曲", ["唱段", "选段", "京剧", "昆曲", "豫剧", "川剧"]),
    ("声乐·歌曲", ["歌曲", "少儿歌曲", "群众歌曲", "艺术歌曲", "颂歌", "民歌"]),
]


def classify(ctxs):
    votes = collections.Counter()
    evid = {}
    for c in ctxs:
        for name, kws in GENRE_ORDER:
            for kw in kws:
                if kw in c:
                    votes[name] += 1
                    evid.setdefault(name, set()).add(kw)
    if not votes:
        return "?", "", 0
    # 按 GENRE_ORDER 顺序取第一个有票的（优先级）
    order = [n for n, _ in GENRE_ORDER]
    for n in order:
        if votes[n]:
            return n, "/".join(sorted(evid[n])), votes[n]
    return "?", "", 0


def main():
    rows = []
    for key in ["七上", "七下", "八上", "八下"]:
        v = VOLUMES[key]
        pages = load_pages(os.path.join(HERE, f"{v['stem']}_全文.txt"))
        off = v["offset"]
        works = sorted(v["works"], key=lambda w: w[3])
        for i, (unit, kind, title, bp) in enumerate(works):
            nxt = works[i + 1][3] if i + 1 < len(works) else bp + 6
            seg = "\n".join(pages.get(p, "") for p in range(bp + off - 1, min(nxt, bp + 6) + off + 1))
            seg = re.sub(r"\s+", "", seg)
            # 抓 《title》 上下文（容错：截断标题取前 4 字匹配）
            stem4 = title.split("——")[0].split("（")[0][:4]
            ctxs = []
            for m in re.finditer(r"《" + re.escape(stem4) + r"[^》]{0,20}》", seg):
                s = max(0, m.start() - 8)
                ctxs.append(seg[s:m.end() + 6])
            # 曲名不带书名号出现时，取前 6 字上下文
            for m in re.finditer(re.escape(stem4), seg):
                s = max(0, m.start() - 6)
                ctxs.append(seg[s:m.start() + len(stem4) + 4])
            g, e, n = classify(ctxs)
            rows.append((v["book"], unit, kind, title, bp, bp + off, g, e, n, " | ".join(ctxs[:4])))
    out = os.path.join(HERE, "out", "work_genre.tsv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("vol\tunit\tkind\ttitle\tbook_page\tpdf_page\tgenre\tevidence\tvotes\tctx\n")
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")
    print("wrote", out)
    cnt = collections.Counter(r[6] for r in rows)
    for k, n in cnt.most_common():
        print(f"{n:4d}  {k}")


if __name__ == "__main__":
    main()
