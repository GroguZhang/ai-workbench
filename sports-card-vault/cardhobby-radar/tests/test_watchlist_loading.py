# -*- coding: utf-8 -*-
"""词库加载与校验的回归测试。

watchlist.json 是手工维护的热加载文件，一个手误的代价被严重低估：
解析失败时雷达会**静默回退内置名单**，照常扫描照常出结果，
但用的是内置球员和内置红线 —— 用户以为在跑自己的策略，其实不是。
"""
import json

import pytest

import cardhobby_sniper_radar as radar


# ---------------------------------------------------------------- 归一化校验

def test_normalize_keeps_valid_entries():
    raw = [{"keyword": "科达", "en_name": " Sebastian Korda "}]
    entries, problems = radar._normalize_watchlist(raw)
    assert problems == []
    assert entries[0]["keyword"] == "科达"
    assert entries[0]["en_name"] == "Sebastian Korda", "en_name 应被去空白"


def test_normalize_drops_entry_without_keyword():
    raw = [{"keyword": "科达"}, {"en_name": "X"}, {"keyword": "   "}]
    entries, problems = radar._normalize_watchlist(raw)
    assert [e["keyword"] for e in entries] == ["科达"]
    assert len(problems) == 2
    assert "第 2 条" in problems[0] and "keyword" in problems[0]


def test_normalize_drops_non_dict_entries():
    entries, problems = radar._normalize_watchlist(["科达", 123, None, {"keyword": "阿尔卡拉斯"}])
    assert [e["keyword"] for e in entries] == ["阿尔卡拉斯"]
    assert len(problems) == 3


def test_normalize_dedupes_keyword_case_insensitively():
    raw = [{"keyword": "Korda"}, {"keyword": "korda"}, {"keyword": "科达"}]
    entries, problems = radar._normalize_watchlist(raw)
    assert [e["keyword"] for e in entries] == ["Korda", "科达"]
    assert any("重复" in p for p in problems)


def test_normalize_rejects_non_list_root():
    entries, problems = radar._normalize_watchlist({"keyword": "科达"})
    assert entries == []
    assert problems and "数组" in problems[0]


# ---------------------------------------------------------------- 加载与回退

@pytest.fixture()
def wl_path(tmp_path, monkeypatch):
    p = tmp_path / "watchlist.json"
    monkeypatch.setattr(radar, "EXTERNAL_WATCHLIST_PATH", str(p))
    monkeypatch.setattr(radar, "_WATCHLIST_PROBLEMS", [])
    return p


def test_load_reports_malformed_json_and_falls_back(wl_path):
    wl_path.write_text('{"keyword": "科达",}', encoding="utf-8")   # 尾逗号 = 非法 JSON

    entries = radar.load_watchlist()

    assert entries, "回退后仍应有可用词库"
    assert radar._WATCHLIST_PROBLEMS, "词库解析失败必须被记录下来"
    assert any("解析失败" in p for p in radar._WATCHLIST_PROBLEMS)


def test_load_external_valid_file_is_used(wl_path):
    wl_path.write_text(json.dumps([{"keyword": "科达", "tiers": {}}]), encoding="utf-8")

    entries = radar.load_watchlist()

    assert [e["keyword"] for e in entries] == ["科达"]
    assert radar._WATCHLIST_PROBLEMS == []


def test_load_external_all_invalid_falls_back_with_problem(wl_path):
    wl_path.write_text(json.dumps([{"en_name": "no keyword"}]), encoding="utf-8")

    entries = radar.load_watchlist()

    assert entries, "全部无效时必须回退内置词库，而不是空转"
    assert any("回退" in p or "没有可用条目" in p for p in radar._WATCHLIST_PROBLEMS)


def test_fallback_returns_deep_copy_not_shared_global(wl_path):
    """核心回归：回退时必须给深拷贝。

    apply_learned_rules 会原地往每个 cfg 里写 exclude_words / _calib / _tier_penalty。
    如果这里返回的是模块级 DEFAULT_GOLDEN_WATCHLIST 本体，
    这些写入会跨轮次累积（exclude_words 无限变长），而且外部完全看不出来。
    """
    wl_path.write_text("not a json at all", encoding="utf-8")

    first = radar.load_watchlist()
    first[0].setdefault("exclude_words", []).append("污染标记")
    first[0]["_calib"] = {"dirty": True}

    second = radar.load_watchlist()

    assert "污染标记" not in (second[0].get("exclude_words") or []), \
        "内置词库被跨轮次污染了"
    assert "_calib" not in second[0], "内置词库被写入了运行时字段"

    # 连模块级原始常量也必须干净
    assert "污染标记" not in (radar.DEFAULT_GOLDEN_WATCHLIST[0].get("exclude_words") or [])
    assert "_calib" not in radar.DEFAULT_GOLDEN_WATCHLIST[0]
