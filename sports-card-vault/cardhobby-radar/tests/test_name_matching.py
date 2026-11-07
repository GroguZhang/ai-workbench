# -*- coding: utf-8 -*-
"""球员名匹配与英文候选词生成的回归测试。

这里的核心风险是**同名误报**：把 "Luka Garza" 的卡当成东契奇的，
红线会按东契奇的价算，显示 75 倍暴利。所以「绝不拆出单名」这条必须钉死。
"""
import pytest

import cardhobby_sniper_radar as radar


@pytest.mark.parametrize(
    "en_name,expected",
    [
        ("Luka Doncic", ["Luka Doncic", "Doncic"]),   # 只到姓氏，绝不拆出 "Luka"
        ("Korda", ["Korda"]),                          # 单词名原样返回
        ("", []),
        (None, []),
    ],
)
def test_en_name_candidates(en_name, expected):
    assert radar.en_name_candidates(en_name) == expected


def test_single_given_name_is_never_a_candidate():
    # 这条单独拎出来：单名重名率过高，出现即事故
    cands = radar.en_name_candidates("Luka Doncic")
    assert "Luka" not in cands
    assert "Doncic" in cands


@pytest.mark.parametrize(
    "name,expected",
    [
        ("卢卡·东契奇", "卢卡东契奇"),
        ("Alex de Minaur", "AlexdeMinaur"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_cn(name, expected):
    assert radar._normalize_cn(name) == expected


@pytest.mark.parametrize(
    "title,seller,expected",
    [
        ("拔脚代卖 某卡", "", "拔脚代卖"),          # 已知代卖商家
        ("【老张卡屋】某卡", "sellerX", "老张卡屋"),  # 【】括注
        ("普通标题", "张三", "张三"),                # 回落卖家名
        ("普通标题", "", "独立卖家"),                # 兜底
    ],
)
def test_extract_consignor(title, seller, expected):
    assert radar.extract_consignor(title, seller) == expected
