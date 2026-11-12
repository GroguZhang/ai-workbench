# -*- coding: utf-8 -*-
"""扫描教参批量 OCR -> 可搜索 PDF + 分页全文
用法: python ocr_batch.py <job_index...>
lossless=True : 原页原样保留（画质无损），仅加隐藏文字层
lossless=False: 按 dpi/quality 重编码后再加文字层（用于超 100MiB 上限的大件）
"""
import os, sys, time, io
import numpy as np
import pymupdf
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

WD = r"D:\WorkBuddy\初中音乐教学资源库\_work"
OCR_DPI = 200
engine = RapidOCR()

JOBS = [
    dict(src=r"D:\工作\教参\七上教参.pdf", stem="人音版_七上_教师参考用书", lossless=True),
    dict(src=r"D:\工作\教参\八下教参.pdf", stem="人音版_八下_教师参考用书", lossless=True),
    dict(src=r"D:\工作\教参\七下教参.pdf", stem="人音版_七下_教师参考用书", lossless=False,
         dpi=200, quality=80),
]


def ocr_page(page, dpi=OCR_DPI):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    res, _ = engine(img)
    if not res:
        return []
    s = 72.0 / dpi
    out = []
    for box, text, score in res:
        if text and text.strip():
            out.append(([(float(x) * s, float(y) * s) for x, y in box], text.strip()))
    return out


def add_text_layer(dp, lines):
    for pts, text in lines:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        h = max(y1 - y0, 4.0)
        fs = h * 0.82
        try:
            w = pymupdf.get_text_length(text, fontname="china-s", fontsize=fs)
            if w > 0 and w > (x1 - x0) * 1.6:
                fs *= (x1 - x0) * 1.6 / w
        except Exception:
            pass
        try:
            dp.insert_text((x0, y1 - h * 0.15), text, fontname="china-s",
                           fontsize=max(fs, 3.0), render_mode=3)
        except Exception:
            pass


def run(job):
    src = pymupdf.open(job["src"])
    stem, lossless = job["stem"], job["lossless"]
    n = src.page_count
    dst = pymupdf.open()
    txt = []
    t0 = time.time()
    mode = "lossless" if lossless else f"recompress {job['dpi']}dpi q{job['quality']}"
    print(f"[{stem}] {n}p  mode={mode}", flush=True)
    for i in range(n):
        sp = src[i]
        lines = ocr_page(sp)
        if lossless:
            dst.insert_pdf(src, from_page=i, to_page=i)
            dp = dst[-1]
        else:
            pix = sp.get_pixmap(dpi=job["dpi"], colorspace=pymupdf.csRGB, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=job["quality"], optimize=True)
            dp = dst.new_page(width=sp.rect.width, height=sp.rect.height)
            dp.insert_image(dp.rect, stream=buf.getvalue())
        add_text_layer(dp, lines)
        txt.append(f"\n\n===== PAGE {i+1} =====\n" + "".join(t + "\n" for _, t in lines))
        if i % 10 == 0 or i == n - 1:
            el = time.time() - t0
            print(f"  p{i+1}/{n}  {el:.0f}s  eta {el/(i+1)*(n-i-1):.0f}s", flush=True)

    op = os.path.join(WD, f"{stem}_可搜索版.pdf")
    dst.subset_fonts()
    # garbage=4 必须：insert_pdf 会拆散源文件中被多页共享的图像对象，
    # 造成体积翻倍；garbage=4 做对象级去重，把它还原（实测 139.9MB -> 70.4MB）
    dst.save(op, garbage=4, deflate=True)
    dst.close(); src.close()
    with open(os.path.join(WD, f"{stem}_全文.txt"), "w", encoding="utf-8") as f:
        f.write("".join(txt))
    print(f"[DONE] {stem}  ->  {round(os.path.getsize(op)/1048576,1)}MB  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    idx = [int(a) for a in sys.argv[1:]] or list(range(len(JOBS)))
    for k in idx:
        run(JOBS[k])
