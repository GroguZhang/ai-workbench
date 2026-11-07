# -*- coding: utf-8 -*-
"""智能拉正（perspective-crop）端到端回归测试。

真实故障（2026-11-07 线上）：
    前端弹出「透视矫正请求异常：Unexpected token 'I', "Internal S"... is not valid JSON」

根因不在前端，也不在算法，而在接口返回体的最后一行：
    "aspect": res["aspect"]
而 photos.warp_quad_manual() 只返回 data/width/height，没有 aspect 这个键
（前端其实也从未使用过它）。于是每次拉正都在**成功完成变换之后**抛 KeyError，
被 Starlette 兜成纯文本 500，前端 res.json() 只能报一句解析错误。

这个用例把整条链路跑通：上传真实图片 → 建卡 → 调拉正 → 断言 200 与字段完整。
修复前它会拿到 500。
"""
import io

import pytest
from PIL import Image

from app.main import DEFAULT_SORT  # noqa: F401  (确保 app.main 已导入)


def _make_card_photo(w=600, h=840) -> bytes:
    """造一张「深色桌面上摆着一张浅色卡」的图，让自动检测有东西可找。"""
    img = Image.new("RGB", (w, h), (28, 30, 36))
    card = Image.new("RGB", (w - 60, h - 80), (235, 235, 240))
    img.paste(card, (30, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture()
def card_with_photo(client):
    photo = _make_card_photo()
    res = client.post(
        "/api/cards",
        data={
            "sport": "篮球", "player": "测试球员", "year_set": "2022 Panini Prizm",
            "card_type": "Base", "serial": "25/99",
        },
        files={"img_front": ("front.jpg", photo, "image/jpeg")},
    )
    assert res.status_code == 200, res.text
    return res.json()["card_id"]


def test_perspective_crop_returns_success_and_complete_payload(client, card_with_photo):
    res = client.post(
        f"/api/cards/{card_with_photo}/perspective-crop",
        json={
            "side": "front",
            "corners": [[30, 40], [570, 40], [570, 800], [30, 800]],
            "rotate": 0,
        },
    )

    # 修复前这里就是 500（纯文本 "Internal Server Error"）
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert body["side"] == "front"
    assert body["img"]
    assert body["new_url"].startswith("/uploads/")

    # 这三个字段前端会用到，必须齐全
    for key in ("width", "height", "aspect"):
        assert key in body, f"响应缺少 {key}"

    # aspect 是短边/长边，必然落在 (0, 1]
    assert 0 < body["aspect"] <= 1, f"aspect 非法: {body['aspect']}"


def test_perspective_crop_rejects_bad_corner_count(client, card_with_photo):
    res = client.post(
        f"/api/cards/{card_with_photo}/perspective-crop",
        json={"side": "front", "corners": [[1, 1], [2, 2]]},
    )
    assert res.status_code == 400
    assert "4 个角点" in res.json()["detail"]


def test_perspective_crop_unknown_card_returns_404(client):
    res = client.post(
        "/api/cards/不存在的卡/perspective-crop",
        json={"side": "front", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    assert res.status_code == 404
