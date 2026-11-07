# -*- coding: utf-8 -*-
"""居中度测量（calculate_centering / auto_detect_borders）回归测试。

线上故障（2026-11-08）：
    点「居中测量」→ 「加载居中度测量异常：Unexpected token 'I', "Internal S"... is not valid JSON」
    容器日志：AttributeError: module 'app.photos' has no attribute 'auto_detect_borders'

原因是 photos.py 在一次改写中丢掉了 calculate_centering / auto_detect_borders，
而 main.py 仍在调用。这两个函数是「预评级」结论的唯一来源，丢了界面上什么都出不来。
"""
import io

import pytest
from PIL import Image

from app import photos


def _make_card_photo(w=800, h=1120) -> bytes:
    img = Image.new("RGB", (w, h), (20, 22, 28))
    img.paste(Image.new("RGB", (w - 80, h - 100), (240, 240, 245)), (40, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- 纯函数契约

@pytest.mark.parametrize(
    "left,right,top,bottom,expect_tag,expect_psa_fragment",
    [
        (50, 50, 50, 50, "excellent", "PSA 10"),
        (58, 42, 50, 50, "good", "PSA 10"),
        (63, 37, 50, 50, "fair", "PSA 9"),
        (68, 32, 50, 50, "warning", "PSA 8"),
        (80, 20, 50, 50, "poor", "偏版"),
    ],
)
def test_centering_grade_buckets(left, right, top, bottom, expect_tag, expect_psa_fragment):
    res = photos.calculate_centering(left, right, top, bottom)
    assert res["tag"] == expect_tag
    assert expect_psa_fragment in res["psa_grade"]
    assert 0 <= res["overall_max"] <= 100


def test_centering_perfect_ratios():
    res = photos.calculate_centering(50, 50, 50, 50)
    assert res["lr_max"] == pytest.approx(50.0)
    assert res["tb_max"] == pytest.approx(50.0)
    assert res["lr_ratio"] == "50.0 / 50.0"


def test_centering_string_formats_are_high_then_low():
    res = photos.calculate_centering(70, 30, 40, 60)
    assert res["lr_ratio"] == "70.0 / 30.0", "左右应保持 大/小 的顺序"
    assert res["tb_ratio"] == "60.0 / 40.0", "上下同样按 大/小 展示"


def test_centering_zero_sum_does_not_divide_by_zero():
    # 用户把四条边都拖到 0 时不能崩，退化成分不出高下
    res = photos.calculate_centering(0, 0, 0, 0)
    assert res["lr_max"] == pytest.approx(50.0)
    assert res["tb_max"] == pytest.approx(50.0)
    assert res["tag"] == "excellent"


def test_centering_returns_full_contract():
    res = photos.calculate_centering(50, 45, 48, 52)
    for key in ("lr_ratio", "tb_ratio", "lr_max", "tb_max", "overall_max",
                "psa_grade", "bgs_grade", "tag", "recommendation", "verdict_text"):
        assert key in res, f"缺少字段 {key}"


# ---------------------------------------------------------------- 自动探测

def test_auto_detect_borders_returns_geometry_and_grade():
    res = photos.auto_detect_borders(_make_card_photo())
    for key in ("left", "right", "top", "bottom", "width", "height"):
        assert key in res, f"缺少几何字段 {key}"
    # 预评结论必须一并算好，前端直接读 tag / psa_grade
    assert "tag" in res and "psa_grade" in res
    assert res["width"] > 0 and res["height"] > 0


# ---------------------------------------------------------------- 兼容层

def test_process_alias_points_at_process_photo():
    """main.run_process 调的是 photos.process；改名后必须留兼容别名，
    否则异常被吞，表现为「自动裁切静默失效」。"""
    assert photos.process is photos.process_photo


# ---------------------------------------------------------------- HTTP 层

def test_centering_endpoint_returns_json(client):
    created = client.post(
        "/api/cards",
        data={"sport": "篮球", "player": "居中度测试", "year_set": "2022 Panini Prizm"},
        files={"img_front": ("front.jpg", _make_card_photo(), "image/jpeg")},
    )
    assert created.status_code == 200, created.text
    card_id = created.json()["card_id"]

    res = client.get(f"/api/cards/{card_id}/centering", params={"side": "front"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert "psa_grade" in body and "tag" in body
