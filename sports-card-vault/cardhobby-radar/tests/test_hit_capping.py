# -*- coding: utf-8 -*-
"""命中封顶（_cap_by_player）的回归测试。

这段逻辑有个被低估的后果：超出上限的候选**既不推送、也不写入 hits.jsonl**，
所以网页端同样看不到。用户看到「今晚 2 张漏」时无法分辨那是全部，
还是被截断后的残影。因此行为本身必须被钉死，丢弃数量也必须上报。
"""
import cardhobby_sniper_radar as radar


def _row(player, profit=0.0):
    return {"player_cn": player, "profit_at_red": profit}


def test_per_player_cap_is_enforced():
    rows = [_row("科达"), _row("科达"), _row("科达")]
    kept = radar._cap_by_player(rows, total_cap=100, per_player_cap=2)
    assert len(kept) == 2, "同一球员最多保留 per_player_cap 张"


def test_total_cap_is_enforced():
    rows = [_row("A"), _row("B"), _row("C"), _row("D")]
    kept = radar._cap_by_player(rows, total_cap=3, per_player_cap=99)
    assert len(kept) == 3


def test_total_cap_takes_the_first_rows_in_given_order():
    # 调用方已按利润降序排好，这里保持原顺序取前 N 条
    rows = [_row("A"), _row("B"), _row("C"), _row("D")]
    kept = radar._cap_by_player(rows, total_cap=2, per_player_cap=99)
    assert [r["player_cn"] for r in kept] == ["A", "B"]


def test_interleaved_players_are_capped_independently():
    rows = [_row("A"), _row("B"), _row("A"), _row("B"), _row("A")]
    kept = radar._cap_by_player(rows, total_cap=100, per_player_cap=2)
    assert [r["player_cn"] for r in kept] == ["A", "B", "A", "B"]


def test_default_per_player_cap_comes_from_config():
    # 默认值来自 MAX_HITS_PER_PLAYER，改环境变量即可放宽，不需要改代码
    rows = [_row("A")] * 10
    kept = radar._cap_by_player(rows, total_cap=100)
    assert len(kept) == radar.MAX_HITS_PER_PLAYER


def test_empty_input_returns_empty():
    assert radar._cap_by_player([], total_cap=10) == []
