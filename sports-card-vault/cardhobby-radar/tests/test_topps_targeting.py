# -*- coding: utf-8 -*-
"""Topps 雷达的「系列白名单 + 通知阈值」回归测试。

背景：上线版本的规则只有「是 Topps + 有动作词 + 有品类词就推送」，
改策略只能改代码。加入白名单与阈值后，最大的风险不是新功能不生效，
而是**默认配置把原来的推送悄悄掐死**——用户以为雷达还在跑，其实全被拦光了。

所以这组测试的第一条就是「空配置必须完全维持原行为」，剩下的按门禁逐条锁：
白名单中英文归一、自定义系列兜底、原价上限、截止提醒窗口、优先级分，
以及最关键的一条：被暂缓的条目不写 seen_articles，因此条件改善后还能推出来。
"""
import datetime
import time

from topps_drop_radar import ToppsDropRadar


def _radar(tmp_path, **env):
    """构造一个不联网、不推送、状态写在临时目录的雷达实例。"""
    base = {
        "PUSHPLUS_TOKEN": "",
        "BARK_URL": "",
        "WECHAT_WORK_WEBHOOK": "",
        "DEEPSEEK_API_KEY": "",
    }
    base.update(env)
    return ToppsDropRadar(data_dir=str(tmp_path), env=base)


def _article(title, snippet="", hours_ago=1.0):
    return {
        "title": title,
        "snippet": snippet,
        "account": "测试号",
        "url": "https://example.com/a",
        "pub_ts": int(time.time() - hours_ago * 3600),
        "pub_dt": "",
    }


# ---------------------------------------------------------------------------
# 一、兼容性：默认配置必须和上线行为完全一致
# ---------------------------------------------------------------------------

def test_default_config_keeps_original_behavior(tmp_path):
    r = _radar(tmp_path)
    assert r.series_whitelist == set()
    assert r.min_score == 0
    assert r.max_official_price == 0.0
    assert r.deadline_within_hours == 0.0

    art = _article("Topps Chrome 卡盒 抽签开启", "原价1200 预计溢价四位数")
    assert r.is_valid_topps_drop(art) is True
    assert r.score_drop(art)["blocks"] == []


def test_empty_config_never_blocks_any_series(tmp_path):
    r = _radar(tmp_path)
    for title in ("Topps Merlin 卡盒 抽签", "Topps 蓝宝石 发售", "Topps 王权 放货"):
        assert r.is_series_watched(title) is True


# ---------------------------------------------------------------------------
# 二、系列白名单
# ---------------------------------------------------------------------------

def test_whitelist_accepts_chinese_alias(tmp_path):
    """「蓝宝石」必须能被 sapphire 白名单认出来，否则中文推文全漏。"""
    r = _radar(tmp_path, TOPPS_SERIES_WHITELIST="sapphire")
    assert r.series_whitelist == {"sapphire"}
    assert r.is_series_watched("topps 蓝宝石 卡盒 抽签") is True
    assert r.is_valid_topps_drop(_article("Topps 蓝宝石 卡盒 抽签", "原价3000")) is True


def test_whitelist_rejects_unwatched_series(tmp_path):
    r = _radar(tmp_path, TOPPS_SERIES_WHITELIST="chrome,bowman")
    art = _article("Topps Merlin 卡盒 抽签", "原价1500")
    assert r.is_series_watched(art["title"]) is False
    assert r.is_valid_topps_drop(art) is False


def test_whitelist_custom_series_falls_back_to_substring(tmp_path):
    """用户自己加的新系列（代码里没收录）也要能用，按子串兜底。"""
    r = _radar(tmp_path, TOPPS_SERIES_WHITELIST="mythic")
    assert r.series_whitelist == {"mythic"}
    assert r.is_series_watched("topps mythic 卡盒 抽签") is True
    assert r.is_series_watched("topps chrome 卡盒 抽签") is False


def test_whitelist_list_parsing_tolerates_mixed_separators(tmp_path):
    r = _radar(tmp_path, TOPPS_SERIES_WHITELIST="chrome，蓝宝石; bowman|finest ")
    assert r.series_whitelist == {"chrome", "sapphire", "bowman", "finest"}


# ---------------------------------------------------------------------------
# 三、原价上限
# ---------------------------------------------------------------------------

def test_max_official_price_blocks_expensive_box(tmp_path):
    r = _radar(tmp_path, TOPPS_MAX_OFFICIAL_PRICE="2000")
    v = r.score_drop(_article("Topps Chrome 卡盒 抽签", "原价4649 预计溢价四位数"))
    assert any("超过上限" in b for b in v["blocks"])


def test_max_official_price_lets_affordable_box_through(tmp_path):
    r = _radar(tmp_path, TOPPS_MAX_OFFICIAL_PRICE="2000")
    v = r.score_drop(_article("Topps Chrome 卡盒 抽签", "原价1200 预计溢价"))
    assert v["blocks"] == []
    assert any("原价" in h for h in v["hits"])


# ---------------------------------------------------------------------------
# 四、截止时间解析与提醒窗口
# ---------------------------------------------------------------------------

def test_parse_deadline_ts_handles_today_tomorrow_and_month_day(tmp_path):
    r = _radar(tmp_path)
    now = datetime.datetime(2026, 11, 17, 10, 0, 0)

    got = r.parse_deadline_ts("登记截止：今天 14:00", now=now)
    assert datetime.datetime.fromtimestamp(got) == datetime.datetime(2026, 11, 17, 14, 0, 0)

    got = r.parse_deadline_ts("明天 09:30 开签", now=now)
    assert datetime.datetime.fromtimestamp(got) == datetime.datetime(2026, 11, 18, 9, 30, 0)

    got = r.parse_deadline_ts("11月20日 14:00 截止", now=now)
    assert datetime.datetime.fromtimestamp(got) == datetime.datetime(2026, 11, 20, 14, 0, 0)


def test_parse_deadline_ts_handles_pm_words(tmp_path):
    r = _radar(tmp_path)
    now = datetime.datetime(2026, 11, 17, 10, 0, 0)
    got = r.parse_deadline_ts("今天 晚上 8:30 截止", now=now)
    assert datetime.datetime.fromtimestamp(got) == datetime.datetime(2026, 11, 17, 20, 30, 0)


def test_parse_deadline_ts_returns_none_when_absent(tmp_path):
    r = _radar(tmp_path)
    assert r.parse_deadline_ts("Topps Chrome 新系列上架") is None


def test_deadline_window_blocks_far_future(tmp_path, monkeypatch):
    r = _radar(tmp_path, TOPPS_DEADLINE_WITHIN_HOURS="24")
    monkeypatch.setattr(r, "parse_deadline_ts", lambda text, now=None: time.time() + 72 * 3600)
    v = r.score_drop(_article("Topps Chrome 卡盒 抽签", "截止 3天后"))
    assert any("提醒窗口" in b for b in v["blocks"])


def test_deadline_window_allows_imminent(tmp_path, monkeypatch):
    r = _radar(tmp_path, TOPPS_DEADLINE_WITHIN_HOURS="24")
    monkeypatch.setattr(r, "parse_deadline_ts", lambda text, now=None: time.time() + 2 * 3600)
    v = r.score_drop(_article("Topps Chrome 卡盒 抽签", "截止 今天"))
    assert v["blocks"] == []
    assert any("距截止" in h for h in v["hits"])


def test_expired_deadline_is_always_blocked(tmp_path, monkeypatch):
    """已经关门的抽签再推就是噪音，与阈值配置无关。"""
    r = _radar(tmp_path)
    monkeypatch.setattr(r, "parse_deadline_ts", lambda text, now=None: time.time() - 5 * 3600)
    v = r.score_drop(_article("Topps Chrome 卡盒 抽签", ""))
    assert any("已过截止" in b for b in v["blocks"])


# ---------------------------------------------------------------------------
# 五、check_and_notify 门禁：推、不推、以及「暂缓后还能再推」
# ---------------------------------------------------------------------------

def test_qualified_drop_alerts_and_never_repeats(tmp_path, monkeypatch):
    r = _radar(tmp_path, TOPPS_SERIES_WHITELIST="chrome", TOPPS_MIN_SCORE="30")
    calls = []
    art = _article("Topps Chrome 卡盒 抽签开启", "原价1200 预计溢价四位数")
    monkeypatch.setattr(r, "scan", lambda lookback_hours=72.0: [dict(art)])
    monkeypatch.setattr(r, "dispatch_alert", lambda drop: calls.append(drop))

    assert r.check_and_notify() == 1
    assert len(calls) == 1
    assert calls[0]["priority_score"] >= 30

    # 同一指纹第二轮不应重复推送
    assert r.check_and_notify() == 0
    assert len(calls) == 1


def test_min_score_gate_suppresses_without_marking_seen(tmp_path, monkeypatch):
    """被暂缓的条目绝不能写 seen_articles，否则就永远推不出来了。"""
    r = _radar(tmp_path, TOPPS_MIN_SCORE="999")
    calls = []
    art = _article("Topps Chrome 卡盒 抽签", "原价1200")
    monkeypatch.setattr(r, "scan", lambda lookback_hours=72.0: [dict(art)])
    monkeypatch.setattr(r, "dispatch_alert", lambda drop: calls.append(drop))

    assert r.check_and_notify() == 0
    assert calls == []
    assert r._state["seen_articles"] == {}
    assert len(r._state["suppressed"]) == 1
    assert r.last_scan_report["alerted"] == 0
    assert len(r.last_scan_report["suppressed"]) == 1


def test_suppressed_drop_fires_after_threshold_relaxed(tmp_path, monkeypatch):
    """暂缓 → 条件改善（例如截止临近分数升高）→ 应该能正常推出去。"""
    r = _radar(tmp_path, TOPPS_MIN_SCORE="999")
    calls = []
    art = _article("Topps Chrome 卡盒 抽签", "原价1200")
    monkeypatch.setattr(r, "scan", lambda lookback_hours=72.0: [dict(art)])
    monkeypatch.setattr(r, "dispatch_alert", lambda drop: calls.append(drop))

    assert r.check_and_notify() == 0
    r.min_score = 0
    assert r.check_and_notify() == 1
    assert len(calls) == 1
    # 推出去之后暂缓记录要被清掉
    assert r._state["suppressed"] == {}


def test_bad_config_values_do_not_crash(tmp_path):
    """配置写错（空值/乱码）时退回默认值，不能让整轮巡检炸掉。"""
    r = _radar(tmp_path,
               TOPPS_MIN_SCORE="不是数字",
               TOPPS_MAX_OFFICIAL_PRICE="",
               TOPPS_DEADLINE_WITHIN_HOURS="abc")
    assert r.min_score == 0
    assert r.max_official_price == 0.0
    assert r.deadline_within_hours == 0.0
