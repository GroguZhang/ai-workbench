# -*- coding: utf-8 -*-
"""限量编识别（parse_serial_details）回归测试。

这是整套雷达的**抗骗核心**：假编、假尾编、卡号冒充编、评级分冒充 10/10、
截标日期冒充编数，全靠这一段挡住。任何一条识别错误都会直接变成
一个错误的买入上限，真金白银的误判。
"""
import pytest

import cardhobby_sniper_radar as radar


@pytest.mark.parametrize(
    "title,denom,is_special,badge",
    [
        # 空
        ("", None, False, "无编"),
        # 标准分子/分母
        ("2026 Topps Chrome 01/25", 25, True, "🌟首编(01/25)"),
        ("2024 Prizm Gold 25/25", 25, True, "🌟尾编(25/25)"),
        ("2023 Select 25/99", 99, False, "/99编(25/99)"),
        # 1/1 唯一编
        ("1/1 Superfractor", 1, True, "🔥1/1唯一编"),
        # —— 以下全部是「不该被识别成编」的负样本 ——
        ("PSA 10/10 gem mint", None, False, "无编"),          # 评级分冒充 10/10
        ("2026 Panini 10/12截标", None, False, "无编"),        # 截标日期冒充编
        ("卡号 #10 球星卡", None, False, "无编"),               # 卡号冒充编
        # 只有分母
        ("/99绿签", 99, False, "/99编"),
        # 中文明确编数
        ("25编 低编", 25, False, "/25编"),
    ],
)
def test_parse_serial_details(title, denom, is_special, badge):
    assert radar.parse_serial_details(title) == (denom, is_special, badge)


@pytest.mark.parametrize(
    "title",
    [
        "2025 Bowman Chrome PSA 10/10 金折",
        "BGS 9.5/10 银折",
        "双 10/10 神签",
    ],
)
def test_rating_score_never_leaks_as_serial(title):
    # 钉住最贵的那个坑：评级分被当成尾编会让整张卡的买入上限直接失真
    assert radar.parse_serial_details(title) == (None, False, "无编")


@pytest.mark.parametrize(
    "serial,expected",
    [
        ("48/49", 49),
        ("1/1", 1),
        ("/99", 99),
        ("待查背面", None),
        ("", None),
        (None, None),
    ],
)
def test_card_serial_denom(serial, expected):
    assert radar._card_serial_denom(serial) == expected
