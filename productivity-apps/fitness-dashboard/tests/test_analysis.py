# -*- coding: utf-8 -*-
"""分析层回归测试。

core/analysis.py 是纯标准库实现（statistics + datetime），
所以这里的断言不需要 pandas/streamlit，CI 里跑得很快。
"""
import pytest

from core.analysis import bia_error_check, compute_trend, moving_avg, training_volume


# ---------------------------------------------------------------- 移动平均

def test_moving_avg_centered_with_edge_padding():
    # window=7 → half=3；边缘用可用数据补，而不是丢弃
    assert moving_avg([1, 2, 3, 4, 5], 7) == [2.5, 3.0, 3.0, 3.0, 3.5]


def test_moving_avg_empty():
    assert moving_avg([], 7) == []


def test_moving_avg_all_none_yields_none():
    assert moving_avg([None, None], 7) == [None, None]


def test_moving_avg_skips_none_within_window():
    # 中间的空值被跳过，不当作 0 参与平均
    assert moving_avg([1, None, 3], 3) == [1.0, 2.0, 3.0]


def test_moving_avg_window_one_is_identity():
    assert moving_avg([10, 20, 30], 1) == [10.0, 20.0, 30.0]


# ---------------------------------------------------------------- 趋势

def test_compute_trend_sorts_and_computes_slope():
    records = [
        {"date": "2026-01-02", "weight": 79},
        {"date": "2026-01-01", "weight": 80},   # 故意乱序传入
    ]
    got = compute_trend(records, "weight")
    assert got["raw"] == [80, 79]               # 已按日期排序
    assert got["latest"] == 79
    assert got["slope"] == pytest.approx(0.0)


def test_compute_trend_empty_is_safe():
    got = compute_trend([], "weight")
    assert got == {"raw": [], "ma": [], "slope": None, "latest": None}


# ---------------------------------------------------------------- 训练容量

@pytest.mark.parametrize(
    "rows,expected",
    [
        ([{"sets": 3, "reps": 10, "weight": 50}], 1500.0),
        ([{"sets": 3, "reps": 10, "weight": 50}, {"sets": 2, "reps": 8, "weight": 40}], 2140.0),
        ([], 0.0),
        ([{"sets": 3}], 0.0),                    # 缺字段按 0，不抛异常
        ([{"sets": None, "reps": None, "weight": None}], 0.0),
    ],
)
def test_training_volume(rows, expected):
    assert training_volume(rows) == expected


# ---------------------------------------------------------------- BIA 连锁误差

def test_bia_without_baseline():
    assert bia_error_check({"water_pct": 50}, None) == {"risk": "no_baseline"}


def test_bia_detects_water_driven_chain_error():
    yesterday = {"water_pct": 55.0, "ffm_kg": 60.0, "fat_mass_kg": 15.0, "muscle_kg": 40.0}
    today = {"water_pct": 56.5, "ffm_kg": 59.2, "fat_mass_kg": 14.9, "muscle_kg": 39.9}
    got = bia_error_check(today, yesterday)
    assert got["risk"] == "water_chain_error"
    assert got["water_dominant"] is True
    assert got["trustworthy_signal"] == "fat_mass_kg"


def test_bia_detects_physiologically_impossible_muscle_loss():
    # 骨骼肌单日降 0.8kg 在生理上不可能，必须被识别为测量误差
    yesterday = {"water_pct": 55.0, "ffm_kg": 60.0, "fat_mass_kg": 15.0, "muscle_kg": 40.0}
    today = {"water_pct": 55.1, "ffm_kg": 60.0, "fat_mass_kg": 15.0, "muscle_kg": 39.2}
    got = bia_error_check(today, yesterday)
    assert got["impossible_muscle_loss"] is True
    assert got["risk"] == "water_chain_error"


def test_bia_normal_day_is_normal():
    yesterday = {"water_pct": 55.0, "ffm_kg": 60.0, "fat_mass_kg": 15.0, "muscle_kg": 40.0}
    today = {"water_pct": 55.1, "ffm_kg": 60.1, "fat_mass_kg": 14.9, "muscle_kg": 40.0}
    got = bia_error_check(today, yesterday)
    assert got["risk"] == "normal"
    assert got["water_dominant"] is False
