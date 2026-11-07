# -*- coding: utf-8 -*-
"""图像几何逻辑的纯函数测试（不需要真实照片）。"""
import numpy as np
import pytest

from app.photos import CARD_ASPECT, ASPECT_TOL, _aspect_of, _aspect_prior, _order_quad


def test_aspect_prior_peaks_at_standard_card_ratio():
    assert _aspect_prior(CARD_ASPECT) == pytest.approx(1.0)


def test_aspect_prior_decays_by_tolerance():
    assert _aspect_prior(CARD_ASPECT + ASPECT_TOL) == pytest.approx(0.0, abs=1e-9)
    assert _aspect_prior(CARD_ASPECT - ASPECT_TOL) == pytest.approx(0.0, abs=1e-9)


def test_aspect_prior_never_negative():
    assert _aspect_prior(0.0) == 0.0
    assert _aspect_prior(5.0) == 0.0


def test_order_quad_normalises_corner_order():
    # 故意打乱顺序输入，输出必须是 左上 → 右上 → 右下 → 左下
    pts = np.array([[90, 110], [10, 10], [95, 15], [12, 105]], dtype=np.float32)
    got = _order_quad(pts).astype(int).tolist()
    assert got == [[10, 10], [95, 15], [90, 110], [12, 105]]


def test_aspect_of_uses_short_over_long():
    img = np.zeros((100, 140, 3), dtype=np.uint8)
    assert _aspect_of(img) == pytest.approx(100 / 140)


def test_aspect_of_is_orientation_independent():
    a = np.zeros((100, 140, 3), dtype=np.uint8)
    b = np.zeros((140, 100, 3), dtype=np.uint8)
    assert _aspect_of(a) == pytest.approx(_aspect_of(b))
