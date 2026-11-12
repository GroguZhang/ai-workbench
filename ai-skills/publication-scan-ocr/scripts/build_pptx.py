# -*- coding: utf-8 -*-
"""生成《初中音乐课件母版（15页骨架）.pptx》
雅致艺术风 · 16:9 · 大字投屏友好 · 含音频占位框与乐谱灰色贴图框
用法: python build_pptx.py
"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn
import copy

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "stage", "02_课件与教案", "初中音乐课件母版（15页骨架）.pptx")

# ---------- 色板（雅致艺术风） ----------
BG      = RGBColor(0xFB, 0xF9, 0xF5)   # 暖白底
PANEL   = RGBColor(0xF1, 0xEE, 0xE8)   # 面板浅灰
INK     = RGBColor(0x1B, 0x2A, 0x41)   # 墨蓝（主文字）
INK2    = RGBColor(0x3C, 0x4A, 0x60)   # 次级墨蓝
GRAY    = RGBColor(0x8B, 0x85, 0x78)   # 辅助灰
ACCENT  = RGBColor(0xB4, 0x88, 0x4D)   # 赭金
ACCENT2 = RGBColor(0x8C, 0x6A, 0x3A)   # 深赭金
LINE    = RGBColor(0xC9, 0xC3, 0xB8)   # 虚线框
WHITE   = RGBColor(0xFF, 0xFF, 0xFF)

FONT = "微软雅黑"
FONT_T = "思源宋体"   # 标题衬线（无该字体时自动回退）

W, H = Inches(13.333), Inches(7.5)
M = Inches(0.72)          # 左右页边距
CONTENT_TOP = Inches(1.72)
CONTENT_BOT = Inches(6.62)


def _set_ea(run, name):
    """同时设置中西文字体"""
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", name)


def box(slide, x, y, w, h, fill=None, line=None, dash=False, radius=None,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE, line_w=Pt(1.25)):
    sh = slide.shapes.add_shape(shape, x, y, w, h)
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid(); sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line; sh.line.width = line_w
        if dash:
            sh.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    sh.shadow.inherit = False
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sh.adjustments[0] = radius
    if sh.has_text_frame:
        sh.text_frame.text = ""
    return sh


def text(slide, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         line_spacing=1.25, space_after=4, wrap=True):
    """runs: list of (字符串, 字号, 加粗, 颜色) 或 list[list[...]] 表示多段"""
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    paras = runs if runs and isinstance(runs[0], list) else [runs]
    for i, pr in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line_spacing
        p.space_after = Pt(space_after)
        for (s, sz, bold, col) in pr:
            r = p.add_run(); r.text = s
            r.font.size = Pt(sz); r.font.bold = bold
            r.font.color.rgb = col; r.font.name = FONT
            _set_ea(r, FONT)
    return tb


def slide_new(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])   # 空白版式
    box(s, 0, 0, W, H, fill=BG, shape=MSO_SHAPE.RECTANGLE)
    return s


def header(s, title, eyebrow=None, page=None, sub=None):
    box(s, M, Inches(0.56), Inches(0.075), Inches(0.66), fill=ACCENT,
        shape=MSO_SHAPE.RECTANGLE)
    text(s, M + Inches(0.20), Inches(0.50), Inches(10.4), Inches(0.78),
         [(title, 34, True, INK)], anchor=MSO_ANCHOR.MIDDLE)
    if eyebrow:
        text(s, W - M - Inches(4.6), Inches(0.60), Inches(4.6), Inches(0.4),
             [(eyebrow, 14, False, ACCENT2)], align=PP_ALIGN.RIGHT)
    if sub:
        text(s, W - M - Inches(6.0), Inches(1.16), Inches(6.0), Inches(0.4),
             [(sub, 15, False, GRAY)], align=PP_ALIGN.RIGHT)
    # 页脚
    box(s, M, Inches(6.80), W - M * 2, Emu(6350), fill=LINE, shape=MSO_SHAPE.RECTANGLE)
    text(s, M, Inches(6.92), Inches(8.0), Inches(0.36),
         [("人音版（2024修订）初中音乐 · 教研组课件母版", 11, False, GRAY)])
    if page:
        text(s, W - M - Inches(1.2), Inches(6.92), Inches(1.2), Inches(0.36),
             [(str(page), 12, True, ACCENT2)], align=PP_ALIGN.RIGHT)


def audio_box(s, x, y, w, h, label="音频占位", hint="上课时替换为本课音响", n=1):
    """音频占位框：虚线 + 播放三角 + 说明"""
    box(s, x, y, w, h, fill=PANEL, line=LINE, dash=True, radius=0.06)
    cx = x + Inches(0.42)
    cy = y + h / 2
    tri = s.shapes.add_shape(MSO_SHAPE.ISOSCELES_TRIANGLE, cx, cy - Inches(0.22),
                             Inches(0.38), Inches(0.44))
    tri.rotation = 90
    tri.fill.solid(); tri.fill.fore_color.rgb = ACCENT
    tri.line.fill.background(); tri.shadow.inherit = False
    text(s, cx + Inches(0.60), cy - Inches(0.40), w - Inches(1.3), Inches(0.8),
         [[(f"{label} {n}", 20, True, INK2)],
          [(hint, 13, False, GRAY)]],
         anchor=MSO_ANCHOR.MIDDLE, line_spacing=1.2, space_after=2)


def score_box(s, x, y, w, h, label="乐谱贴图区", hint="插入教材谱例截图 / 自行截图后替换本框"):
    """乐谱灰色占位框"""
    box(s, x, y, w, h, fill=RGBColor(0xEC, 0xE9, 0xE3), line=LINE, dash=True, radius=0.03)
    text(s, x + Inches(0.2), y + h / 2 - Inches(0.5), w - Inches(0.4), Inches(1.0),
         [[(label, 19, True, GRAY)], [(hint, 12, False, RGBColor(0xA8, 0xA2, 0x96))]],
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, line_spacing=1.2, space_after=3)


def note(s, txt):
    s.notes_slide.notes_text_frame.text = txt


def bullets(s, x, y, w, h, items, size=22, gap=8, color=INK2, bullet_color=ACCENT):
    """带方形项目符号的要点列表"""
    paras = []
    for it in items:
        if isinstance(it, tuple):
            paras.append([("■  ", size - 4, True, bullet_color), (it[0], size, True, INK)][:1]
                         + [(it[0], size, True, INK)])
            paras[-1].append((it[1], size - 6, False, GRAY))
        else:
            paras.append([("■  ", size - 6, True, bullet_color), (it, size, False, color)])
    text(s, x, y, w, h, paras, line_spacing=1.35, space_after=gap)


def table_grid(s, x, y, w, h, rows, cols, header_row=True,
               headers=None, col_w=None, font_size=16):
    """轻量表格：用矩形拼（避免 PPT 表格样式污染）"""
    rh = h / rows
    cw = col_w or [w / cols] * cols
    cx = x
    for j in range(cols):
        cy = y
        for i in range(rows):
            fill = ACCENT if (header_row and i == 0) else (PANEL if i % 2 else WHITE)
            box(s, cx, cy, cw[j], rh, fill=fill, line=LINE, line_w=Pt(0.75),
                shape=MSO_SHAPE.RECTANGLE)
            if header_row and i == 0 and headers:
                text(s, cx + Inches(0.10), cy, cw[j] - Inches(0.20), rh,
                     [(headers[j], font_size, True, WHITE)],
                     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, wrap=True)
            cy += rh
        cx += cw[j]


# ==================== 主构建 ====================
def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # ---------- P1 封面 ----------
    s = slide_new(prs)
    box(s, 0, 0, W, Inches(2.35), fill=INK, shape=MSO_SHAPE.RECTANGLE)
    box(s, 0, Inches(2.35), W, Inches(0.05), fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)
    text(s, M, Inches(0.62), Inches(9.6), Inches(0.4),
         [("人音版（2024修订）义务教育教科书 · 艺术·音乐", 15, False, RGBColor(0xC8, 0xB9, 0x9A))])
    text(s, M, Inches(1.02), Inches(11.0), Inches(1.1),
         [("《　　　　　　》", 46, True, WHITE)])
    text(s, W - M - Inches(3.4), Inches(1.62), Inches(3.4), Inches(0.5),
         [("课件课题占位", 13, False, RGBColor(0x9A, 0xA6, 0xB5))], align=PP_ALIGN.RIGHT)

    text(s, M, Inches(2.78), Inches(5.4), Inches(2.4),
         [[("册次 / 单元", 14, True, ACCENT2)],
          [("八年级上册　第五单元　国乐飘香（一）", 21, False, INK2)],
          [("", 10, False, INK2)],
          [("课　　型", 14, True, ACCENT2)],
          [("欣赏课（纯器乐欣赏）", 21, False, INK2)]],
         line_spacing=1.15, space_after=6)
    text(s, M + Inches(5.8), Inches(2.78), Inches(5.6), Inches(2.4),
         [[("授 课 教 师", 14, True, ACCENT2)],
          [("　　　　　　　　　", 21, False, INK2)],
          [("", 10, False, INK2)],
          [("授 课 日 期", 14, True, ACCENT2)],
          [("　　　年　　月　　日", 21, False, INK2)]],
         line_spacing=1.15, space_after=6)
    box(s, M, Inches(5.42), W - M * 2, Emu(6350), fill=LINE, shape=MSO_SHAPE.RECTANGLE)
    text(s, M, Inches(5.62), Inches(11.9), Inches(0.9),
         [("使用提示：本页为封面骨架。深色标题区请保持纯色，不要叠加图片，以保证投影清晰度。", 13, False, GRAY)])
    note(s, "【母版使用说明】\n"
            "1. 全册 15 页为骨架，按课型删减：歌唱课重点用 P1–P8、P11、P14–P15；欣赏课重点用 P1–P6、P8–P10、P13–P15。\n"
            "2. 所有虚线框均为占位：音频框替换为「插入→音频」，乐谱框替换为「插入→图片」。\n"
            "3. 色板：暖白底 #FBF9F5／墨蓝 #1B2A41／赭金 #B4884D。正文不小于 20pt，标题 34–40pt。\n"
            "4. 每页只放 1 个核心信息；投影教室后排可视距离有限，宁少勿多。")

    # ---------- P2 学习目标 ----------
    s = slide_new(prs)
    header(s, "学习目标", eyebrow="Learning Goals", page=2,
           sub="按 2022 版艺术课标四维素养拆解")
    items = [("审美感知", "能听辨本课作品的主题与音色特征，\n说出 2 处以上的音乐要素变化"),
             ("艺术表现", "能用正确的姿势与气息参与演唱/模唱，\n准确表现指定节奏型"),
             ("创意实践", "能运用本课节奏或音型，\n编创 4–8 小节节奏短句"),
             ("文化理解", "能结合创作背景，\n说出作品与时代、地域文化的关联")]
    cw = (W - M * 2 - Inches(0.30) * 3) / 4
    for i, (t, d) in enumerate(items):
        x = M + i * (cw + Inches(0.30))
        box(s, x, CONTENT_TOP, cw, Inches(4.3), fill=WHITE, line=LINE, radius=0.05)
        box(s, x, CONTENT_TOP, cw, Inches(0.72), fill=ACCENT, radius=0.05)
        box(s, x, CONTENT_TOP + Inches(0.42), cw, Inches(0.30), fill=ACCENT,
            shape=MSO_SHAPE.RECTANGLE)
        text(s, x, CONTENT_TOP + Inches(0.04), cw, Inches(0.64), [(t, 22, True, WHITE)],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + Inches(0.22), CONTENT_TOP + Inches(1.0), cw - Inches(0.44), Inches(3.1),
             [(d, 17, False, INK2)], line_spacing=1.5, space_after=0)
    note(s, "四维素养为课标固定框架，建议每课只保留 1–2 条可观测的行为目标，删除其余，避免目标虚化。")

    # ---------- P3 情境导入 ----------
    s = slide_new(prs)
    header(s, "情境导入", eyebrow="Lead-in", page=3, sub="建议 5 分钟以内，直接切入音乐")
    audio_box(s, M, CONTENT_TOP, Inches(7.1), Inches(2.05), "导入音频占位",
              "可选：音效 / 歌曲片段 / 器乐音色片段", 1)
    box(s, M, CONTENT_TOP + Inches(2.30), Inches(7.1), Inches(2.30), fill=WHITE,
        line=LINE, radius=0.04)
    text(s, M + Inches(0.32), CONTENT_TOP + Inches(2.52), Inches(6.5), Inches(0.5),
         [("驱动问题（板书）", 19, True, ACCENT2)])
    text(s, M + Inches(0.32), CONTENT_TOP + Inches(3.06), Inches(6.5), Inches(1.4),
         [[("1. 你听到的是什么音色？它让你想到了什么画面？", 20, False, INK2)],
          [("2. 这段音乐的情绪是统一的，还是变化的？", 20, False, INK2)]],
         line_spacing=1.4, space_after=10)
    box(s, M + Inches(7.45), CONTENT_TOP, W - M * 2 - Inches(7.45), Inches(4.60),
        fill=PANEL, line=LINE, dash=True, radius=0.04)
    text(s, M + Inches(7.65), CONTENT_TOP + Inches(1.9), W - M * 2 - Inches(7.85), Inches(0.8),
         [[("情境图 / 视频占位", 18, True, GRAY)],
          [("插入图片、封面截图或短视频", 12, False, RGBColor(0xA8, 0xA2, 0x96))]],
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, space_after=3)
    note(s, "导入忌长篇铺垫。推荐三种高效导入：① 音色盲听猜乐器；② 一句旋律模唱接龙；③ 一张图片对应一段音乐。")

    # ---------- P4 初听全曲 ----------
    s = slide_new(prs)
    header(s, "初听全曲", eyebrow="First Listening", page=4,
           sub="第一次聆听：只听情绪与画面，不做分析")
    audio_box(s, M, CONTENT_TOP, Inches(6.0), Inches(4.55), "完整音频占位",
              "全曲播放（建议完整听一遍，中途不打断）", 1)
    box(s, M + Inches(6.35), CONTENT_TOP, W - M * 2 - Inches(6.35), Inches(4.55),
        fill=WHITE, line=LINE, radius=0.04)
    text(s, M + Inches(6.65), CONTENT_TOP + Inches(0.30), Inches(5.0), Inches(0.5),
         [("聆听任务单（第 1 次）", 21, True, ACCENT2)])
    bullets(s, M + Inches(6.65), CONTENT_TOP + Inches(1.02), Inches(5.0), Inches(3.2),
            ["音乐的情绪基调是什么？（欢快 / 抒情 / 庄严 / 悲壮）",
             "速度与力度大致如何？",
             "听到哪些你熟悉的乐器音色？",
             "音乐有没有让你想到某个场景？用一句话写下。"], size=19, gap=14)
    note(s, "第 1 次聆听必须留白：不要边听边讲。听前给出问题，听后立刻让学生用一句话或一个词回答，教师只做汇总不评价。")

    # ---------- P5 作品与作者 ----------
    s = slide_new(prs)
    header(s, "作品与作者", eyebrow="Background", page=5)
    box(s, M, CONTENT_TOP, Inches(5.4), Inches(4.55), fill=PANEL, line=LINE, dash=True, radius=0.04)
    text(s, M + Inches(0.2), CONTENT_TOP + Inches(1.9), Inches(5.0), Inches(0.8),
         [[("作曲家肖像 / 手稿图占位", 17, True, GRAY)],
          [("插入图片后替换本框", 12, False, RGBColor(0xA8, 0xA2, 0x96))]],
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, space_after=3)
    bullets(s, M + Inches(5.75), CONTENT_TOP, Inches(6.1), Inches(4.5),
            [("作曲家", "生卒年 · 国籍 · 代表作品 2–3 部"),
             ("体裁与编制", "例：小提琴协奏曲｜二胡独奏曲｜混声四部合唱"),
             ("创作背景", "创作年代、缘起、与时代事件的关系（2–3 句）"),
             ("作品地位", "获奖、流传度、在音乐史中的位置（1 句）")],
            size=20, gap=18)
    note(s, "背景介绍控制在 3 分钟内。凡教参未写明、你自己也不确定的生平细节，一律不讲——课堂上的错误信息比不讲更难纠正。")

    # ---------- P6 乐谱精读 ----------
    s = slide_new(prs)
    header(s, "乐谱精读", eyebrow="Score Reading", page=6,
           sub="对照谱例，找出音乐要素的关键变化")
    score_box(s, M, CONTENT_TOP, Inches(8.6), Inches(3.05))
    bullets(s, M, CONTENT_TOP + Inches(3.28), Inches(8.6), Inches(1.3),
            ["看拍号：有没有变换拍子？在哪里变？",
             "看节奏：有没有切分、附点、跨小节连线？",
             "看旋律：主题出现几次？每次有什么变化？"], size=18, gap=6)
    box(s, M + Inches(8.95), CONTENT_TOP, W - M * 2 - Inches(8.95), Inches(4.55),
        fill=WHITE, line=LINE, radius=0.04)
    text(s, M + Inches(9.15), CONTENT_TOP + Inches(0.28), Inches(3.0), Inches(0.5),
         [("谱例标注要点", 18, True, ACCENT2)])
    text(s, M + Inches(9.15), CONTENT_TOP + Inches(0.92), Inches(3.0), Inches(3.3),
         [[("① 用红框圈出主题句", 15, False, INK2)],
          [("② 用箭头标出旋律走向", 15, False, INK2)],
          [("③ 用小节号标出结构分界", 15, False, INK2)],
          [("④ 用色块区分不同乐段", 15, False, INK2)]],
         line_spacing=1.5, space_after=12)
    note(s, "乐谱截图务必截清小节号与拍号，投影后放大仍可辨认。若谱例太小，宁可拆成两页。")

    # ---------- P7 主题模唱 ----------
    s = slide_new(prs)
    header(s, "音乐主题模唱", eyebrow="Theme Singing", page=7,
           sub="用唱名或\"啦\"哼唱，先会唱再谈分析")
    score_box(s, M, CONTENT_TOP, Inches(5.5), Inches(2.6), "主题谱例占位",
              "只截取主题句，去掉多余乐句")
    audio_box(s, M, CONTENT_TOP + Inches(2.85), Inches(5.5), Inches(1.70),
              "主题音频占位", "慢速示范 + 原速各一遍", 1)
    bullets(s, M + Inches(5.90), CONTENT_TOP, Inches(6.0), Inches(4.5),
            [("唱名", "用 1 2 3 5 6 逐句哼唱，先慢后快"),
             ("节奏", "先拍节奏再唱音高，切分处单独练 4 遍"),
             ("手势", "用柯尔文手势或划拍，标出旋律上下行"),
             ("模进位置", "主题第二遍出现时移高/移低了几度？")],
            size=20, gap=18)
    note(s, "主题模唱是欣赏课的核心环节。标准：全班能用\"啦\"整齐哼出主题首句，且能指出主题在曲中出现的次数。")

    # ---------- P8 曲式结构图 ----------
    s = slide_new(prs)
    header(s, "曲式结构", eyebrow="Form", page=8)
    box(s, M, CONTENT_TOP, W - M * 2, Inches(2.85), fill=WHITE, line=LINE, radius=0.04)
    segs = [("引子", 1.35), ("A 段", 2.05), ("B 段", 2.05), ("A′ 段", 2.05), ("尾声", 1.35)]
    total = sum(w for _, w in segs)
    avail = (W - M * 2 - Inches(0.7))
    x = M + Inches(0.35)
    for i, (nm, w) in enumerate(segs):
        bw = avail * w / total - Inches(0.12)
        col = ACCENT if i % 2 == 0 else INK2
        box(s, x, CONTENT_TOP + Inches(0.75), bw, Inches(1.1), fill=col, radius=0.05)
        text(s, x, CONTENT_TOP + Inches(0.75), bw, Inches(1.1), [(nm, 22, True, WHITE)],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        if i < len(segs) - 1:
            text(s, x + bw, CONTENT_TOP + Inches(0.75), Inches(0.12), Inches(1.1),
                 [("›", 22, True, GRAY)], align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        x += bw + Inches(0.12)
    text(s, M + Inches(0.35), CONTENT_TOP + Inches(0.24), Inches(10.0), Inches(0.4),
         [("结构示意（按小节数比例调整宽度）", 15, False, GRAY)])
    bullets(s, M, CONTENT_TOP + Inches(3.10), W - M * 2, Inches(1.4),
            ["各段的小节范围：________ 至 ________（请对照谱例填写）",
             "速度或力度在哪个节点发生明显变化？",
             "B 段与 A 段是什么关系：对比 / 展开 / 变奏？"], size=19, gap=8)
    note(s, "结构图不要直接给答案，让学生听第 3 遍时自己填。此页在填写前只保留色块，文字全部删掉。")

    # ---------- P9 分段赏析 ----------
    s = slide_new(prs)
    header(s, "分段赏析", eyebrow="Section by Section", page=9,
           sub="第 2 次聆听：按段听，逐段填表")
    half = (W - M * 2 - Inches(0.4)) / 2
    for i, (t, hint) in enumerate([("A 段｜主题呈示", "旋律特点 / 音色 / 力度"),
                                   ("B 段｜对比与发展", "变化了什么 / 情绪走向")]):
        x = M + i * (half + Inches(0.4))
        text(s, x, CONTENT_TOP, half, Inches(0.5), [(t, 22, True, ACCENT2)])
        audio_box(s, x, CONTENT_TOP + Inches(0.62), half, Inches(1.25),
                  f"第 {i + 2} 段音频", hint, 1)
        box(s, x, CONTENT_TOP + Inches(2.05), half, Inches(2.50), fill=WHITE,
            line=LINE, radius=0.04)
        text(s, x + Inches(0.28), CONTENT_TOP + Inches(2.28), half - Inches(0.56), Inches(2.0),
             [[("旋律：", 17, True, INK), ("　　　　　　　　　　　　　", 17, False, GRAY)],
              [("音色：", 17, True, INK), ("　　　　　　　　　　　　　", 17, False, GRAY)],
              [("力度：", 17, True, INK), ("　　　　　　　　　　　　　", 17, False, GRAY)],
              [("情绪：", 17, True, INK), ("　　　　　　　　　　　　　", 17, False, GRAY)]],
             line_spacing=1.5, space_after=10)
    note(s, "分段聆听的顺序建议：先听 A 段→填表→再听 A 段验证；再听 B 段→填表→对比 A。不要一段听完立刻讲结论。")

    # ---------- P10 对比聆听 ----------
    s = slide_new(prs)
    header(s, "对比聆听", eyebrow="Compare", page=10,
           sub="第 3 次聆听：在比较中理解音乐要素的作用")
    audio_box(s, M, CONTENT_TOP, Inches(4.4), Inches(1.55), "作品甲音频", "本课作品", 1)
    audio_box(s, M + Inches(4.75), CONTENT_TOP, Inches(4.4), Inches(1.55), "作品乙音频",
              "教材其他作品 / 同一作品不同版本", 2)
    box(s, M + Inches(9.50), CONTENT_TOP, W - M * 2 - Inches(9.50), Inches(1.55),
        fill=PANEL, line=LINE, dash=True, radius=0.04)
    text(s, M + Inches(9.70), CONTENT_TOP + Inches(0.30), Inches(2.5), Inches(1.0),
         [[("对比要点占位", 15, True, GRAY)], [("可写关键词", 11, False, RGBColor(0xA8, 0xA2, 0x96))]],
         anchor=MSO_ANCHOR.MIDDLE, space_after=2)
    table_grid(s, M, CONTENT_TOP + Inches(1.90), W - M * 2, Inches(2.60), 5, 3,
               headers=["对比维度", "作品甲", "作品乙"])
    rows = [("体裁与编制", "填写", "填写"), ("节拍与速度", "填写", "填写"),
            ("调式色彩", "填写", "填写"), ("情绪与场景", "填写", "填写")]
    rh = Inches(2.60) / 5
    for i, (a, b, c) in enumerate(rows, start=1):
        yy = CONTENT_TOP + Inches(1.90) + rh * i
        text(s, M + Inches(0.12), yy, Inches(3.6), rh, [(a, 16, True, INK)],
             anchor=MSO_ANCHOR.MIDDLE)
        for j, t in enumerate([b, c]):
            text(s, M + Inches(4.0) + j * Inches(4.3), yy, Inches(4.1), rh,
                 [(t, 15, False, RGBColor(0xB8, 0xB2, 0xA6))],
                 anchor=MSO_ANCHOR.MIDDLE, align=PP_ALIGN.CENTER)
    note(s, "对比聆听是课标强调的高阶能力。对比对象优先从本资料库选同体裁或同民族的作品（见《横向速查表》）。")

    # ---------- P11 演唱/演奏实践 ----------
    s = slide_new(prs)
    header(s, "演唱 / 演奏实践", eyebrow="Practice", page=11,
           sub="先慢速分句，再原速连起来")
    score_box(s, M, CONTENT_TOP, Inches(7.0), Inches(3.0), "实践谱例占位",
              "只截取要练的 4–8 小节，放大呈现")
    audio_box(s, M, CONTENT_TOP + Inches(3.22), Inches(3.4), Inches(1.30),
              "示范音频", "教师范唱 / 范奏", 1)
    audio_box(s, M + Inches(3.60), CONTENT_TOP + Inches(3.22), Inches(3.4), Inches(1.30),
              "伴奏音频", "慢速伴奏 + 原速伴奏", 2)
    bullets(s, M + Inches(7.35), CONTENT_TOP, Inches(4.55), Inches(4.5),
            [("气息", "起句前统一吸气，句尾留足时值"),
             ("音准", "先读节奏再唱音高，难句用钢琴带 2 遍"),
             ("声部", "合唱课标出声部进出点与指挥手势"),
             ("变声期", "男生可降八度或减时值，不强求音色")],
            size=19, gap=16)
    note(s, "实践环节严格按「慢速分句 → 加词 → 连句 → 原速」四步走。步子跳大了，学生只会越唱越乱。")

    # ---------- P12 小组创编 ----------
    s = slide_new(prs)
    header(s, "小组创编", eyebrow="Create", page=12, sub="用本课的节奏或音型，编创 4–8 小节")
    cards = [("第 1 组", "用本课节奏型\n编创 4 小节节奏短句"),
             ("第 2 组", "为主题句\n编配固定音型伴奏"),
             ("第 3 组", "用打击乐器\n为 A 段加入声势"),
             ("第 4 组", "为作品\n设计一段情境朗诵")]
    cw = (W - M * 2 - Inches(0.30) * 3) / 4
    for i, (t, d) in enumerate(cards):
        x = M + i * (cw + Inches(0.30))
        box(s, x, CONTENT_TOP, cw, Inches(2.55), fill=WHITE, line=LINE, radius=0.05)
        box(s, x, CONTENT_TOP, cw, Inches(0.62), fill=ACCENT, radius=0.05)
        box(s, x, CONTENT_TOP + Inches(0.34), cw, Inches(0.28), fill=ACCENT,
            shape=MSO_SHAPE.RECTANGLE)
        text(s, x, CONTENT_TOP + Inches(0.03), cw, Inches(0.56), [(t, 20, True, WHITE)],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + Inches(0.18), CONTENT_TOP + Inches(0.86), cw - Inches(0.36), Inches(1.5),
             [(d, 17, False, INK2)], align=PP_ALIGN.CENTER, line_spacing=1.4, space_after=0)
    audio_box(s, M, CONTENT_TOP + Inches(2.85), W - M * 2, Inches(1.70),
              "成果录制 / 回放", "各组录 30 秒，全班回听并给出一条改进建议", 1)
    note(s, "创编不是放羊。给每组一张写死要求的任务卡（上面四张），并规定成果形式与时长，展示才有可比性。")

    # ---------- P13 拓展与迁移 ----------
    s = slide_new(prs)
    header(s, "拓展与迁移", eyebrow="Extension", page=13,
           sub="同体裁 / 同作曲家 / 同地域文化的延伸聆听")
    box(s, M, CONTENT_TOP, Inches(7.4), Inches(4.55), fill=PANEL, line=LINE,
        dash=True, radius=0.04)
    text(s, M + Inches(0.2), CONTENT_TOP + Inches(1.9), Inches(7.0), Inches(0.8),
         [[("拓展素材占位（图片 / 地图 / 乐器实物照）", 17, True, GRAY)],
          [("建议 2–3 张一组，形成视觉对比", 12, False, RGBColor(0xA8, 0xA2, 0x96))]],
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, space_after=3)
    bullets(s, M + Inches(7.75), CONTENT_TOP, Inches(4.15), Inches(4.5),
            [("推荐曲目一", "《　　　　　》　推荐理由：________"),
             ("推荐曲目二", "《　　　　　》　推荐理由：________"),
             ("延伸问题", "同一题材，不同作曲家如何处理？"),
             ("跨学科", "与语文 / 历史 / 地理的联结点：________")],
            size=19, gap=20)
    note(s, "拓展曲目优先从本资料库四册里选（见《初中音乐备课横向速查表.md》考点索引），避免推荐学生听不到的作品。")

    # ---------- P14 课堂小结 ----------
    s = slide_new(prs)
    header(s, "课堂小结", eyebrow="Summary", page=14, sub="由学生说，不由教师念")
    box(s, M, CONTENT_TOP, W - M * 2, Inches(3.35), fill=WHITE, line=LINE, radius=0.04)
    text(s, M + Inches(0.4), CONTENT_TOP + Inches(0.30), Inches(4.0), Inches(0.5),
         [("思维导图占位区", 17, True, GRAY)])
    cx = M + Inches(6.6)
    cy = CONTENT_TOP + Inches(1.62)
    core = box(s, cx - Inches(1.15), cy - Inches(0.62), Inches(2.3), Inches(1.24),
               fill=INK, radius=0.08)
    text(s, cx - Inches(1.15), cy - Inches(0.62), Inches(2.3), Inches(1.24),
         [("作品名称", 20, True, WHITE)], align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    for i, (lbl, dx, dy) in enumerate([("体裁", -3.3, -1.15), ("曲式", 3.3, -1.15),
                                       ("音色", -3.3, 1.00), ("情绪", 3.3, 1.00),
                                       ("背景", 0, 1.55)]):
        bx = cx + Inches(dx) - Inches(0.85)
        by = cy + Inches(dy) - Inches(0.36)
        box(s, bx, by, Inches(1.7), Inches(0.72), fill=ACCENT, radius=0.14)
        text(s, bx, by, Inches(1.7), Inches(0.72), [(lbl, 18, True, WHITE)],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    bullets(s, M, CONTENT_TOP + Inches(3.60), W - M * 2, Inches(1.0),
            ["今天的三个关键词是：________、________、________（学生说，教师板书）"],
            size=21, gap=8)
    note(s, "小结一定要让学生说。教师只把关键词写到板书上，再请一名学生用一句话串起来。")

    # ---------- P15 评价与作业 ----------
    s = slide_new(prs)
    header(s, "课堂评价与作业", eyebrow="Assessment", page=15)
    table_grid(s, M, CONTENT_TOP, Inches(7.5), Inches(3.10), 4, 3,
               headers=["评价维度", "合格", "优秀"])
    rows = [("聆听与描述", "能说出 1 处音乐要素变化", "能准确说出 2 处以上并解释作用"),
            ("演唱 / 模唱", "能跟随集体完整参与", "能独立准确表现主题句与节奏型"),
            ("创编与展示", "能参与小组活动并完成分工", "作品完整、能说明创编依据")]
    rh = Inches(3.10) / 4
    for i, (a, b, c) in enumerate(rows, start=1):
        yy = CONTENT_TOP + rh * i
        text(s, M + Inches(0.12), yy, Inches(2.4), rh, [(a, 16, True, INK)],
             anchor=MSO_ANCHOR.MIDDLE)
        text(s, M + Inches(2.55), yy, Inches(2.4), rh, [(b, 14, False, INK2)],
             anchor=MSO_ANCHOR.MIDDLE)
        text(s, M + Inches(5.00), yy, Inches(2.45), rh, [(c, 14, False, INK2)],
             anchor=MSO_ANCHOR.MIDDLE)
    box(s, M, CONTENT_TOP + Inches(3.35), Inches(7.5), Inches(1.20), fill=PANEL,
        line=LINE, radius=0.04)
    text(s, M + Inches(0.28), CONTENT_TOP + Inches(3.55), Inches(7.0), Inches(0.9),
         [[("必做：", 16, True, ACCENT2), ("回去完整听一遍本课作品，写下 3 个新发现。", 16, False, INK2)],
          [("选做：", 16, True, ACCENT2), ("查找一首同体裁作品，下周课堂分享 1 分钟。", 16, False, INK2)]],
         line_spacing=1.4, space_after=6)
    bullets(s, M + Inches(7.85), CONTENT_TOP, Inches(4.05), Inches(4.5),
            [("自评清单", "「我听出来了」5 条勾选项"),
             ("互评", "小组展示后给出一条改进建议"),
             ("教师评", "只写 1 条优点 + 1 条建议"),
             ("课后反思", "哪一处设计没达到预期？为什么？")],
            size=18, gap=18)
    note(s, "评价必须可观测。「有感情地演唱」这类表述无法评价，改为「能按力度记号唱出强弱对比」。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    prs.save(OUT)
    print("wrote", OUT)
    print("slides:", len(prs.slides._sldIdLst))
    return OUT


if __name__ == "__main__":
    build()
