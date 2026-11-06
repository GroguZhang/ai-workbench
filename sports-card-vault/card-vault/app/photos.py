"""实拍图自动处理模块。

处理链路：EXIF 转正 -> 卡片检测 -> 透视矫正裁切 -> 二级收紧 -> 缩放到长边上限
          -> JPEG 压缩。

**只做几何矫正，绝不自动旋转画面方向**（见下面 FORCE_PORTRAIT 的留档说明）。

三条设计原则（都很重要）：
1. **原图永远保留**。每张上传的图都会另存一份 `_orig`，处理结果不理想时在界面上
   一键「重新处理 / 保留原图」即可，全程没有任何不可逆操作。
2. **认不出来就不动**。卡片检测失败时退回「仅转正 + 压缩」，绝不乱裁。
   实测手机实拍里，错误地"裁掉卡片"比"不裁"糟糕得多。
3. **不美化颜色**。只做几何与体积处理，不做白平衡/增艳——
   这张卡以后要拿去送评，颜色必须真实。
"""
import io
import os

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
    HAS_CV2 = True
except ImportError:          # 没装 opencv 也能跑，只是不做自动裁切
    cv2 = None
    HAS_CV2 = False


# ---------------- 处理档位 ----------------
MODE_AUTO = "auto"              # 自动裁切到卡片 + 转正 + 压缩（推荐）
MODE_STRAIGHTEN = "straighten"  # 仅 EXIF 转正 + 压缩（保留完整画面）
MODE_ORIGINAL = "original"      # 完全不处理
MODES = (MODE_AUTO, MODE_STRAIGHTEN, MODE_ORIGINAL)
MODE_LABELS = {
    MODE_AUTO: "自动裁切到卡片",
    MODE_STRAIGHTEN: "仅转正+压缩",
    MODE_ORIGINAL: "保留原图",
}

# ---------------- 可调参数 ----------------
MAX_EDGE = 1400          # 处理图长边上限（够看清卡面细节，又不至于太大）
JPEG_Q = 85              # 处理后 JPEG 质量
MAX_INPUT_PIXELS = 50_000_000   # 超大图先进件缩小，避免 NAS 内存吃紧

MARGIN = 0.012           # 裁切向外留 1.2%，避免贴边切到卡片边缘
MIN_AREA = 0.05
MAX_AREA = 0.88          # 超过 88% 基本是「把整幅/桌垫抓下来」，否掉
MIN_ASPECT = 0.35        # 放宽长宽比下限，兼顾强烈透视俯仰拍摄
MAX_ASPECT = 1.00
MIN_ANGLE_COS = 0.65     # 放宽正交容差到 50°~130°，避免斜拍角点被直接抛弃
CARD_ASPECT = 2.5 / 3.5  # 标准球星卡 0.714
ASPECT_TOL = 0.28

FORCE_PORTRAIT = False


# ============================ 基础工具 ============================

def _to_bgr(im: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def _encode(bgr: np.ndarray, max_edge: int = MAX_EDGE, quality: int = JPEG_Q):
    h, w = bgr.shape[:2]
    scale = max_edge / float(max(h, w))
    if scale < 1:
        bgr = cv2.resize(bgr, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality,
                                         int(cv2.IMWRITE_JPEG_OPTIMIZE), 1])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes(), bgr.shape[1], bgr.shape[0]


def _encode_pil(im: Image.Image, max_edge: int = MAX_EDGE, quality: int = JPEG_Q):
    """没有 opencv 时的降级路径：只用 Pillow 缩放 + 压缩。"""
    w, h = im.size
    scale = max_edge / float(max(w, h))
    if scale < 1:
        im = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), im.width, im.height


# ============================ 卡片检测 ============================

def _aspect_prior(aspect: float) -> float:
    """越接近标准卡比例得分越高，避免把「卡片 + 一圈背景」当成一张更宽的卡。"""
    return max(0.0, 1.0 - abs(aspect - CARD_ASPECT) / ASPECT_TOL)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]                    # 左上
    rect[2] = pts[np.argmax(s)]                    # 右下
    d = np.diff(pts, axis=1).ravel()               # y - x
    rect[1] = pts[np.argmin(d)]                    # 右上
    rect[3] = pts[np.argmax(d)]                    # 左下
    return rect


def _score_quad(quad: np.ndarray, w: int, h: int):
    area = cv2.contourArea(quad.astype(np.float32))
    frac = area / float(w * h)
    if not (MIN_AREA <= frac <= MAX_AREA):
        return False, {"reason": "area", "frac": round(frac, 4)}
    if not cv2.isContourConvex(quad.astype(np.int32)):
        return False, {"reason": "not-convex", "frac": round(frac, 4)}

    q = _order_quad(quad.astype(np.float32))
    qw = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2.0
    qh = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2.0
    if qw < 8 or qh < 8:
        return False, {"reason": "tiny", "frac": round(frac, 4)}
    aspect = min(qw, qh) / max(qw, qh)
    if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
        return False, {"reason": "aspect", "aspect": round(aspect, 3), "frac": round(frac, 4)}

    worst = 0.0
    for i in range(4):
        p, c, n = q[(i - 1) % 4], q[i], q[(i + 1) % 4]
        v1, v2 = p - c, n - c
        n1, n2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
        if n1 < 1e-6 or n2 < 1e-6:
            return False, {"reason": "degenerate", "frac": round(frac, 4)}
        worst = max(worst, abs(float(np.dot(v1, v2) / (n1 * n2))))
    if worst > MIN_ANGLE_COS:
        return False, {"reason": "skew", "skew": round(worst, 3), "frac": round(frac, 4)}

    # 评分加权：避免大面积桌垫垄断，奖励适中面幅 (20% ~ 70%) 与标准长宽比
    if frac > 0.80:
        area_fit = max(0.1, 1.0 - (frac - 0.80) * 8.0)
    elif frac < 0.15:
        area_fit = max(0.2, frac / 0.15)
    else:
        area_fit = 1.0 - abs(frac - 0.45) * 0.7

    aspect_fit = _aspect_prior(aspect)
    skew_fit = max(0.1, 1.0 - worst * 1.3)
    fit = aspect_fit * 0.45 + area_fit * 0.35 + skew_fit * 0.20
    return True, {"frac": round(frac, 4), "aspect": round(aspect, 3),
                  "skew": round(worst, 3), "fit": round(fit, 4)}


def _candidates(bgr: np.ndarray):
    """多种互补策略的候选轮廓生成：
    A. 亮卡压暗底 (全局 Otsu)
    B. 纹理去噪后的边缘检测 (双边滤波 + Canny)
    C. 深色卡在浅色底上 (反向 Otsu)
    D. 自适应局部阈值 (应对阴影/渐变光照)
    E. HSV 饱和度通道 (印刷油墨 vs 无彩色反光/底板)
    F. Lab 色彩通道与抗反光光斑抑制
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    out = []

    # A. 全局 Otsu
    _, th = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8), iterations=1)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out.extend(("A", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])

    # B. 双边滤波去桌面木纹 + Canny
    try:
        smooth = cv2.bilateralFilter(gray, 7, 50, 50)
        edges = cv2.Canny(smooth, 30, 120)
        edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8), iterations=3)
        cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out.extend(("B", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])
    except Exception:
        pass

    # C. 反向 Otsu (深色卡片)
    _, thi = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    thi = cv2.morphologyEx(thi, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(thi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out.extend(("C", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])

    # D. 自适应局部阈值 (应对阴影/渐变光照)
    try:
        ad = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
        ad = cv2.morphologyEx(ad, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(ad, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out.extend(("D", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])
    except Exception:
        pass

    # E. 饱和度通道 (印刷油墨 vs 无彩色反光/底板)
    try:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        sat = cv2.GaussianBlur(hsv[:, :, 1], (5, 5), 0)
        _, th_sat = cv2.threshold(sat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th_sat = cv2.morphologyEx(th_sat, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(th_sat, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out.extend(("E", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])
    except Exception:
        pass

    # F. Lab 色彩通道与抗反光光斑抑制
    try:
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2Lab)
        a_diff = lab[:, :, 1].astype(np.float32) - 128.0
        b_diff = lab[:, :, 2].astype(np.float32) - 128.0
        chroma = np.sqrt(a_diff * a_diff + b_diff * b_diff).astype(np.uint8)
        chroma = cv2.GaussianBlur(chroma, (7, 7), 0)
        _, th_chroma = cv2.threshold(chroma, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th_chroma = cv2.morphologyEx(th_chroma, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(th_chroma, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out.extend(("F", c) for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5])
    except Exception:
        pass

    return out


def _contour_quad(contour, w: int, h: int):
    """优先 approxPolyDP 出真四边形（能做透视矫正），否则退回 minAreaRect（只矫正旋转）。"""
    if cv2.contourArea(contour) < 0.02 * w * h:
        return None, None
    peri = cv2.arcLength(contour, True)
    for eps in (0.02, 0.03, 0.04, 0.05, 0.06):
        ap = cv2.approxPolyDP(contour, eps * peri, True)
        if len(ap) == 4:
            return ap.reshape(4, 2).astype(np.float32), "poly4"
    return cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32), "minrect"


def detect_card(bgr: np.ndarray, return_candidates: bool = False):
    """返回 (quad_原图坐标, 方式, 指标[, top_candidates])。检不出返回 (None, None, 指标[, []])。"""
    H, W = bgr.shape[:2]
    scale = 900.0 / max(H, W)
    if scale < 1:
        small = cv2.resize(bgr, (max(1, int(W * scale)), max(1, int(H * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = bgr
        scale = 1.0
    sH, sW = small.shape[:2]

    valid = []
    tried = []
    for tag, cnt in _candidates(small):
        quad, how = _contour_quad(cnt, sW, sH)
        if quad is None:
            continue
        ok, info = _score_quad(quad, sW, sH)
        tried.append("%s/%s:%s" % (tag, how, "ok" if ok else info.get("reason")))
        if not ok:
            continue
        valid.append((info["fit"], quad, "%s/%s" % (tag, how), info))

    if not valid:
        if return_candidates:
            return None, None, {"rejected": tried[:8] or ["no-contour"]}, []
        return None, None, {"rejected": tried[:8] or ["no-contour"]}

    valid.sort(key=lambda x: x[0], reverse=True)

    # 提取至多 4 组差异化优质候选框
    top_candidates = []
    for fit, q, how, info in valid:
        orig_q = q / scale
        c = orig_q.mean(axis=0)
        padded_q = c + (orig_q - c) * (1.0 + MARGIN)
        padded_q[:, 0] = np.clip(padded_q[:, 0], 0, W - 1)
        padded_q[:, 1] = np.clip(padded_q[:, 1], 0, H - 1)
        ordered_q = _order_quad(padded_q)

        # 候选去重 (若中心距离小于 5% 且面积接近，视为重复)
        area_curr = cv2.contourArea(ordered_q)
        is_dup = False
        for prev in top_candidates:
            prev_q = np.array(prev["corners"], dtype=np.float32)
            dist_c = np.linalg.norm(ordered_q.mean(axis=0) - prev_q.mean(axis=0))
            area_prev = cv2.contourArea(prev_q)
            if dist_c < max(W, H) * 0.05 and abs(area_curr - area_prev) / max(1.0, area_prev) < 0.12:
                is_dup = True
                break
        if not is_dup:
            top_candidates.append({
                "corners": ordered_q.tolist(),
                "how": how,
                "fit": round(fit, 3)
            })
            if len(top_candidates) >= 4:
                break

    best_corners = top_candidates[0]["corners"]
    best_how = top_candidates[0]["how"]
    best_quad = np.array(best_corners, dtype=np.float32)

    if return_candidates:
        return best_quad, best_how, valid[0][3], top_candidates
    return best_quad, best_how, valid[0][3]


def _warp(bgr: np.ndarray, quad: np.ndarray):
    q = _order_quad(quad.astype(np.float32))
    w = int(round(max(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3]))))
    h = int(round(max(np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1]))))
    if w < 16 or h < 16:
        return None
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(bgr, M, (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _aspect_of(bgr: np.ndarray) -> float:
    """短边/长边，标准球星卡约 0.714。"""
    h, w = bgr.shape[:2]
    return min(h, w) / float(max(h, w))


def _tighten(bgr: np.ndarray):
    """二级收紧：矫正后卡片已经摆正，再找一次边界，去掉带进来的壳边 / 桌面 / 挂绳。"""
    h, w = bgr.shape[:2]
    if h < 64 or w < 64:
        return bgr, False
    orig_asp = _aspect_of(bgr)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return bgr, False
    c = max(cnts, key=cv2.contourArea)
    x, y, cw, ch = cv2.boundingRect(c)
    frac = (cw * ch) / float(w * h)
    if not (0.75 <= frac <= 0.985):
        return bgr, False

    new_asp = min(cw, ch) / float(max(cw, ch))
    if abs(new_asp - CARD_ASPECT) >= abs(orig_asp - CARD_ASPECT):
        return bgr, False

    pad_x = int(round(cw * MARGIN))
    pad_y = int(round(ch * MARGIN))
    x0 = max(0, x - pad_x)
    y0 = max(0, y - pad_y)
    x1 = min(w, x + cw + pad_x)
    y1 = min(h, y + ch + pad_y)
    return bgr[y0:y1, x0:x1], True


# ============================ 顶层接口 ============================

def process_photo(raw: bytes, mode: str = MODE_AUTO, rotate: int = 0,
                  max_edge: int = MAX_EDGE, quality: int = JPEG_Q) -> dict:
    """处理单张卡片照片。"""
    if mode not in MODES:
        mode = MODE_AUTO
    rotate = int(rotate or 0) % 360
    if rotate not in (0, 90, 180, 270):
        rotate = 0

    if mode == MODE_ORIGINAL:
        return {"data": raw, "mode": mode, "width": 0, "height": 0,
                "cropped": False, "tightened": False, "rotated": False,
                "exif_fixed": False, "how": "-", "aspect": 0.0,
                "detect": {}, "note": "原图未处理"}

    src_im = Image.open(io.BytesIO(raw))
    src_size = src_im.size
    im = ImageOps.exif_transpose(src_im).convert("RGB")
    exif_fixed = im.size != src_size
    if im.width * im.height > MAX_INPUT_PIXELS:
        scale = (MAX_INPUT_PIXELS / float(im.width * im.height)) ** 0.5
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                       Image.LANCZOS)

    if not HAS_CV2:
        if rotate:
            im = im.rotate(-rotate, expand=True)
        data, w, h = _encode_pil(im, max_edge, quality)
        return {"data": data, "mode": mode, "width": w, "height": h,
                "cropped": False, "tightened": False, "rotated": bool(rotate),
                "exif_fixed": exif_fixed, "how": "-",
                "aspect": round(min(w, h) / float(max(w, h)), 3), "detect": {},
                "note": "未安装 opencv，仅做转正+压缩"}

    bgr = _to_bgr(im)
    cropped = tightened = auto_rotated = False
    how = "-"
    detail = {}

    if mode == MODE_AUTO:
        quad, how, detail = detect_card(bgr, return_candidates=False)
        if quad is not None:
            warped = _warp(bgr, quad)
            if warped is not None:
                bgr = warped
                cropped = True

    auto_rotated = False

    if mode == MODE_AUTO and cropped:
        bgr, tightened = _tighten(bgr)

    if rotate:
        bgr = cv2.rotate(bgr, {90: cv2.ROTATE_90_CLOCKWISE,
                               180: cv2.ROTATE_180,
                               270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate])

    data, w, h = _encode(bgr, max_edge, quality)
    bits = []
    if exif_fixed: bits.append("EXIF 转正")
    if cropped: bits.append("透视裁切")
    if tightened: bits.append("二级收紧")
    if rotate: bits.append("手动旋转 %d°" % rotate)
    if not bits: bits.append("无需处理，仅压缩")

    return {"data": data, "mode": mode, "width": w, "height": h,
            "cropped": cropped, "tightened": tightened,
            "rotated": bool(rotate) or auto_rotated,
            "auto_rotated": auto_rotated,
            "exif_fixed": exif_fixed, "how": how,
            "aspect": round(min(w, h) / float(max(w, h)), 3),
            "detect": detail, "note": "；".join(bits)}


# ============================ 交互式拉正与居中度测量 ============================

def detect_corners(raw: bytes) -> dict:
    """自动检测卡片 4 个角点坐标及多预设档位，供前端画布一键切换与交互。"""
    src_im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(src_im).convert("RGB")
    W, H = im.size

    # 计算画面中央默认的标准 2.5:3.5 竖版球星卡框 (避免铺满全屏)
    # 无论是竖拍还是横拍，体育卡默认绝大多数是竖版卡
    if H >= W:
        box_h = int(round(H * 0.76))
        box_w = int(round(box_h * (2.5 / 3.5)))
        if box_w > int(W * 0.88):
            box_w = int(round(W * 0.88))
            box_h = int(round(box_w * (3.5 / 2.5)))
    else:
        # 横构图下居中的竖版卡框
        box_h = int(round(H * 0.80))
        box_w = int(round(box_h * (2.5 / 3.5)))
        if box_w > int(W * 0.80):
            box_w = int(round(W * 0.80))
            box_h = int(round(box_w * (3.5 / 2.5)))

    cx, cy = W // 2, H // 2
    x0, y0 = cx - box_w // 2, cy - box_h // 2
    x1, y1 = x0 + box_w, y0 + box_h
    std_box = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    presets = {"std": std_box, "candidates": []}

    if not HAS_CV2:
        return {
            "detected": False,
            "corners": std_box,
            "presets": presets,
            "candidates_count": 0,
            "width": W, "height": H
        }

    bgr = _to_bgr(im)
    quad, how, detail, candidates = detect_card(bgr, return_candidates=True)

    if candidates:
        presets["candidates"] = [c["corners"] for c in candidates]

    if quad is not None:
        q = _order_quad(quad)
        auto_corners = q.tolist()

        # 紧凑卡芯框 (内缩 2.5%，剔除塑料卡砖反光边)
        center = q.mean(axis=0)
        tight_q = center + (q - center) * 0.975
        tight_corners = _order_quad(tight_q).tolist()

        # 完整外壳框 (外扩 3.5%，包含完整卡砖/卡夹)
        outer_q = center + (q - center) * 1.035
        outer_q[:, 0] = np.clip(outer_q[:, 0], 0, W - 1)
        outer_q[:, 1] = np.clip(outer_q[:, 1], 0, H - 1)
        outer_corners = _order_quad(outer_q).tolist()

        presets["auto"] = auto_corners
        presets["tight"] = tight_corners
        presets["outer"] = outer_corners

        return {
            "detected": True,
            "corners": auto_corners,
            "presets": presets,
            "candidates_count": len(candidates),
            "width": W, "height": H, "how": how
        }

    return {
        "detected": False,
        "corners": std_box,
        "presets": presets,
        "candidates_count": 0,
        "width": W, "height": H
    }


def warp_quad_manual(raw: bytes, corners: list, rotate: int = 0,
                     max_edge: int = MAX_EDGE, quality: int = JPEG_Q) -> dict:
    """根据前端交互传入的 4 个角点执行精准透视矫正。"""
    src_im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(src_im).convert("RGB")
    if not HAS_CV2:
        raise RuntimeError("OpenCV 未安装，无法执行透视拉正")
    bgr = _to_bgr(im)
    pts = np.array(corners, dtype=np.float32)
    warped = _warp(bgr, pts)
    if warped is None:
        raise ValueError("拉正计算失败，角点构成的几何多边形过小或退化")

    rotate = int(rotate or 0) % 360
    if rotate in (90, 180, 270):
        bgr = cv2.rotate(warped, {90: cv2.ROTATE_90_CLOCKWISE,
                                  180: cv2.ROTATE_180,
                                  270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate])
    else:
        bgr = warped

    data, w, h = _encode(bgr, max_edge, quality)
    return {"data": data, "width": w, "height": h}
