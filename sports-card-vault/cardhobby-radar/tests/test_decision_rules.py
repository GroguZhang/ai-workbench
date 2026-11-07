# -*- coding: utf-8 -*-
"""决策规则回归测试：评级乘数 / 卡品判定 / 竞争烈度。

这三条规则直接参与「这条捡漏要不要报」的最终判断：
- 评级乘数算错 → 红线定错，高评卡被当裸卡压价、低分壳被当宝贝；
- 卡品误判 → 「无瑕疵」被当成残卡打 75 折，好卡被自己过滤掉；
- 竞争烈度误判 → 把「无人问津」当成「有人蹲守」，或者反过来。
"""
import pytest

import cardhobby_sniper_radar as radar


# ---------------------------------------------------------------- 评级乘数

@pytest.mark.parametrize(
    "title",
    ["PSA 10 gem mint", "BGS 9.5 银折", "SGC 10 金折", "psa 10"],
)
def test_grade10_gets_high_multiplier(title):
    mult, note = radar.parse_grading_info(title)
    assert mult == radar.GRADE10_MULTIPLIER
    assert "高评壳" in note


@pytest.mark.parametrize("title", ["PSA 9 银折", "BGS 9 金折"])
def test_grade9_gets_mid_multiplier(title):
    mult, note = radar.parse_grading_info(title)
    assert mult == 1.15
    assert "保真壳" in note


@pytest.mark.parametrize("title", ["PSA 7 折价", "BGS 8.5 出清"])
def test_low_grade_triggers_downgrade(title):
    mult, note = radar.parse_grading_info(title)
    assert mult == 0.80
    assert "低分评级壳" in note


def test_unrated_card_is_raw():
    assert radar.parse_grading_info("2024 Prizm Gold 金折") == (1.0, "裸卡(Raw)")


# ---------------------------------------------------------------- 卡品判定

@pytest.mark.parametrize(
    "title,expected_flag,expected_word",
    [
        ("角损 低价出", True, "角损"),
        ("小瑕疵 金折", True, "小瑕疵"),
        ("完美品相 可过户", False, "卡品自述优良"),
        ("2024 Prizm 普通金折", False, ""),
        ("", False, ""),
    ],
)
def test_assess_condition_basic(title, expected_flag, expected_word):
    assert radar.assess_condition(title) == (expected_flag, expected_word)


@pytest.mark.parametrize("title", ["无瑕疵 金折", "没有划痕 品相极佳", "不带折痕 出售"])
def test_negated_condition_words_do_not_flag_card_as_damaged(title):
    # 这是最贵的一类误判：把「无瑕疵」读成「瑕疵」，好卡被自动打 75 折后过滤掉
    flagged, word = radar.assess_condition(title)
    assert flagged is False, f"{title!r} 被误判为残卡，命中词={word!r}"


# ---------------------------------------------------------------- 竞争烈度

@pytest.mark.parametrize(
    "out_count,bid_count,expected_level",
    [
        (0, 0, "cold"),
        (0, 1, "watching"),
        (0, 3, "warm"),
        (0, 8, "hot"),
        (130, 0, "hot"),      # 围观数达标也算白热
        (45, 0, "warm"),
        (30, 0, "watching"),
    ],
)
def test_competition_levels(out_count, bid_count, expected_level):
    level, _hint = radar.assess_competition(out_count, bid_count, 60)
    assert level == expected_level


def test_competition_handles_none_inputs():
    # 接口偶尔给 None，不能让整条评估崩掉
    assert radar.assess_competition(None, None, 0)[0] == "cold"


def test_competition_never_negative():
    # 负数没有任何业务含义，必须被夹到 0 而不是当成「负出价」
    assert radar.assess_competition(-5, -5, 0)[0] == "cold"
