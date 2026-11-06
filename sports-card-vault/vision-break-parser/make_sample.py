#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成一张模拟 HobbyStock 风格的拆卡报告截图，用于无真实截图时测试 parse_card.py。
运行: python make_sample.py
输出: ./screenshots/card_sample.jpg
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path(__file__).resolve().parent / "screenshots" / "card_sample.jpg"

# Windows 常见中文字体候选（按优先级）
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",    # 微软雅黑 Bold
    "C:/Windows/Fonts/simhei.ttf",    # 黑体
    "C:/Windows/Fonts/simsun.ttc",    # 宋体
    "/System/Library/Fonts/PingFang.ttc",
]


def load_font(size: int, bold: bool = False):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
    return ImageFont.load_default()


def text_w(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def main():
    W, H = 540, 900
    img = Image.new("RGB", (W, H), "#f4f5f7")
    draw = ImageDraw.Draw(img)

    # ---- 顶部标题栏 ----
    draw.rectangle([0, 0, W, 110], fill="#1f2430")
    f_title = load_font(22, bold=True)
    title = "2025-26 篮球 NBA Topps 盗梦 Inception Hobby #1"
    tw = text_w(draw, title, f_title)
    draw.text(((W - tw) / 2, 40), title, fill="#ffffff", font=f_title)

    # 平台标签
    f_tag = load_font(16)
    tag = "HobbyStock · 拼盒拆卡报告"
    tw2 = text_w(draw, tag, f_tag)
    draw.text(((W - tw2) / 2, 74), tag, fill="#9aa4b2", font=f_tag)

    # ---- 卡牌图片区域（占位） ----
    card_x, card_y, card_w, card_h = 70, 150, 400, 460
    draw.rounded_rectangle([card_x, card_y, card_x + card_w, card_y + card_h],
                           radius=14, fill="#2b3a55", outline="#0f6fff", width=3)
    # 简易球员剪影
    cx, cy = card_x + card_w // 2, card_y + card_h // 2 + 20
    draw.ellipse([cx - 45, cy - 90, cx + 45, cy], fill="#e8edf5")          # 头
    draw.rounded_rectangle([cx - 70, cy, cx + 70, cy + 110], radius=20, fill="#e8edf5")  # 身
    f_card = load_font(20, bold=True)
    card_label = "RYAN DUNN  #RF-RD"
    clw = text_w(draw, card_label, f_card)
    draw.text(((W - clw) / 2, card_y + card_h - 50), card_label, fill="#ffd34d", font=f_card)

    # ---- 图片下方单卡描述栏 ----
    desc_y = card_y + card_h + 30
    draw.rounded_rectangle([40, desc_y, W - 40, desc_y + 150], radius=10,
                           fill="#ffffff", outline="#e1e4e8", width=1)
    f_desc = load_font(22, bold=True)
    desc1 = "太阳 Phoenix Suns"
    draw.text((60, desc_y + 22), desc1, fill="#1a73e8", font=f_desc)
    desc2 = "瑞安·邓恩 Ryan Dunn"
    draw.text((60, desc_y + 58), desc2, fill="#1f2430", font=f_desc)
    desc3 = "Rise to Fame Signatures"
    draw.text((60, desc_y + 94), desc3, fill="#5f6368", font=load_font(20))

    # 底部信息
    f_foot = load_font(15)
    draw.text((40, H - 50), "开售时间: 2025-08-01 20:00  ·  产品编号: INC-H1",
              fill="#80868b", font=f_foot)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUTPUT, "JPEG", quality=92)
    print(f"[OK] 样例截图已生成: {OUTPUT}")


if __name__ == "__main__":
    main()
