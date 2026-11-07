# -*- coding: utf-8 -*-
"""搜索词生成逻辑的回归测试。

这三段逻辑是雷达与档案库之间唯一的"弱约定"：查询写宽了召回爆炸、写窄了一条搜不到。
所以每个分支都钉死一个用例。
"""
import pytest

from app.main import (
    _player_short,
    _serial_token,
    _series_core,
    build_search_query,
    sort_sql,
    _safe_ext,
    DEFAULT_SORT,
)


@pytest.mark.parametrize(
    "year_set,expected",
    [
        ("2026 Topps Series 1 Baseball", "Topps"),          # 只剩母品牌时保留
        ("2019 Panini Prizm", "Prizm"),                     # 母品牌被剔除
        ("Panini Immaculate Collection", "Immaculate Collection"),
        ("2022 Panini Prizm World Cup", "World Cup"),        # 越靠右越具体
        ("", ""),
    ],
)
def test_series_core(year_set, expected):
    assert _series_core(year_set) == expected


@pytest.mark.parametrize(
    "player,expected",
    [
        ("克里斯蒂亚诺·罗纳尔多 (Cristiano Ronaldo)", "Ronaldo"),  # 括号英文取姓氏
        ("Carlos Alcaraz (阿尔卡拉斯)", "阿尔卡拉斯"),              # 括号中文取整段
        ("卢卡·东契奇", "东契奇"),                                  # 中文分隔点取末段
        ("", ""),
    ],
)
def test_player_short(player, expected):
    assert _player_short(player) == expected


@pytest.mark.parametrize(
    "serial,expected",
    [
        ("0149/2026", ""),   # 四位数当产品年份，不进查询
        ("/99", "/99"),
        ("25/99", "/99"),
        ("1/1", "1/1"),
        ("", ""),
    ],
)
def test_serial_token(serial, expected):
    assert _serial_token(serial) == expected


def test_build_search_query_full():
    got = build_search_query(
        sport="篮球",
        player="卢卡·东契奇",
        year_set="2022 Panini Prizm World Cup",
        card_type="Base",
        serial="25/99",
    )
    assert got == "2022 World Cup 东契奇 /99"


def test_build_search_query_skips_optional_parts():
    # 只有球员名时不应凭空补出年份或系列
    assert build_search_query(player="卢卡·东契奇") == "东契奇"


def test_build_search_query_empty_is_empty_string():
    assert build_search_query() == ""


@pytest.mark.parametrize("key", ["price_desc", "price_asc", "pnl_desc", "pnl_asc", "newest", "oldest"])
def test_sort_sql_known_keys(key):
    got_key, clause = sort_sql(key)
    assert got_key == key
    assert clause


def test_sort_sql_unknown_falls_back_without_raising():
    for bogus in ["nonsense", "", None]:
        got_key, clause = sort_sql(bogus)
        assert got_key == DEFAULT_SORT
        assert clause


@pytest.mark.parametrize(
    "filename,expected",
    [("a.PNG", ".png"), ("b.jpeg", ".jpeg"), ("c.exe", ".jpg"), ("noext", ".jpg"), ("", ".jpg")],
)
def test_safe_ext(filename, expected):
    assert _safe_ext(filename) == expected
