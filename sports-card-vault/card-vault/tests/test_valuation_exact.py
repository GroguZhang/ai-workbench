# -*- coding: utf-8 -*-
"""估值铁律测试：1:1 严格同款 + 最近成交价 + 无同款保留人工估值。

用户拍板的定价铁律：
  1. 禁止跨版别算中位数；
  2. 1:1 严格同款（球员+年份系列+平行版别+编数+带签+评级）；
  3. 命中同款取 sold_date 最近一笔真实成交价；
  4. 未命中保留 est_raw_price，valuation_status = insufficient。

这几条一旦被改回去（比如又有人把加权均值/中位数加回来），本文件必须红。
"""

import pytest

from app import main

CARD_FORM = {
    "sport": "篮球",
    "player": "卢卡·东契奇",
    "year_set": "2022 Panini Prizm",
    "card_type": "Base",
    "serial": "25/99",
    "est_raw_price": "1500",
    "cost_price": "800",
    "status": "在库",
}

# 一张卡的"同款"描述，供纯函数测试反复使用
CARD = {
    "player": "卢卡·东契奇",
    "year_set": "2022 Panini Prizm",
    "card_type": "Base",
    "serial": "25/99",
    "parallel": "",
    "notes": "",
    "grader": "",
    "grade_score": 0,
    "is_auto": False,
}

TITLE_BASE = "2022 Panini Prizm 东契奇 Base 25/99"


# ------------------------------------------------------------------ 最近成交价

def test_latest_sale_value_takes_newest_not_mean():
    """取最近一笔，不做任何均值——高价的老成交不能把价吊起来。"""
    sales = [
        {"price": 5000, "sold_date": "2023-01-10"},
        {"price": 1500, "sold_date": "2025-06-01"},
        {"price": 1800, "sold_date": "2024-03-01"},
    ]
    value, n, ordered, earliest = main._latest_sale_value(sales)
    assert value == 1500
    assert n == 3
    assert ordered[0]["sold_date"] == "2025-06-01"
    assert earliest == "2023-01-10"


def test_latest_sale_value_ignores_zero_and_junk_prices():
    sales = [
        {"price": 0, "sold_date": "2025-07-01"},
        {"price": "abc", "sold_date": "2025-07-02"},
        {"price": 888, "sold_date": "2024-01-01"},
    ]
    value, n, _, _ = main._latest_sale_value(sales)
    assert value == 888
    assert n == 1


def test_latest_sale_value_undated_loses_to_dated():
    """日期不明的成交不能抢走"最近"的位置。"""
    sales = [
        {"price": 3000, "sold_date": ""},
        {"price": 700, "sold_date": "2025-01-01"},
    ]
    value, _, ordered, _ = main._latest_sale_value(sales)
    assert value == 700
    assert ordered[-1]["price"] == 3000


def test_latest_sale_value_empty():
    assert main._latest_sale_value([]) == (0.0, 0, [], "")


def test_time_decayed_value_is_aliased_to_latest_sale():
    """旧名字保留兼容，但语义必须已是"最近成交价"。"""
    sales = [{"price": 100, "sold_date": "2024-01-01"},
             {"price": 900, "sold_date": "2025-01-01"}]
    assert main._time_decayed_value(sales)[0] == 900


# ------------------------------------------------------------------ 1:1 严格同款

def test_exact_match_accepts_same_card():
    assert main._is_exact_card_match(CARD, {"title": TITLE_BASE}) is True


@pytest.mark.parametrize("title,reason", [
    ("2022 Panini Prizm 东契奇 Silver 25/99", "平行版别不同"),
    ("2022 Panini Prizm 东契奇 Base 25/199", "印刷量不同"),
    ("2023 Panini Prizm 东契奇 Base 25/99", "年份不同"),
    ("2022 Panini Prizm 东契奇 Base 25/99 PSA 10", "评级状态不同"),
    ("2022 Panini Prizm 东契奇 Base 签字 25/99", "带签状态不同"),
    ("2022 Topps Chrome 东契奇 Base 25/99", "系列品牌不同"),
    ("2022 Panini Prizm 詹姆斯 Base 25/99", "球员不同"),
])
def test_exact_match_rejects_any_dimension_mismatch(title, reason):
    assert main._is_exact_card_match(CARD, {"title": title}) is False, reason


def test_exact_match_rejects_box_sales():
    """整盒成交不能当单卡同款。"""
    assert main._is_exact_card_match(CARD, {"title": "2022 Panini Prizm 东契奇 整盒 hobby box"}) is False


def test_signed_card_does_not_match_unsigned_sale():
    signed = dict(CARD, card_type="Base 签字", is_auto=True)
    assert main._is_exact_card_match(signed, {"title": "2022 Panini Prizm 东契奇 签字 25/99"}) is True
    assert main._is_exact_card_match(signed, {"title": TITLE_BASE}) is False


def test_graded_card_requires_same_grader_and_score():
    graded = dict(CARD, grader="PSA", grade_score=10)
    assert main._is_exact_card_match(graded, {"title": TITLE_BASE + " PSA 10"}) is True
    assert main._is_exact_card_match(graded, {"title": TITLE_BASE + " PSA 9"}) is False
    assert main._is_exact_card_match(graded, {"title": TITLE_BASE}) is False


# ------------------------------------------------------------------ 核心估值入口

@pytest.fixture()
def no_archive(monkeypatch):
    """隔离数据库里的历史档案，让每个用例的候选池只有它自己给的那几笔。"""
    monkeypatch.setattr(main, "_archive_sales", lambda *a, **k: (0, 0))
    monkeypatch.setattr(main, "_load_archived_sales", lambda *a, **k: [])


def _refresh(sales, **over):
    params = dict(
        player_hint="卢卡·东契奇", keyword="kw", raw_sales=sales, tokens=[],
        parallel="", card_type="Base", notes="",
        year_set="2022 Panini Prizm", serial="25/99",
    )
    params.update(over)
    return main._refresh_card_value(None, "card-x", **params)


def test_refresh_picks_latest_exact_sale_price(no_archive):
    sales = [
        {"id": "1", "title": TITLE_BASE, "price": 900, "end_time": "2024-01-01"},
        {"id": "2", "title": TITLE_BASE, "price": 1500, "end_time": "2025-06-01"},
        # 同球员同编数但版别不同，价格高 5 倍，绝不能算进来
        {"id": "3", "title": "2022 Panini Prizm 东契奇 Silver 25/99",
         "price": 8000, "end_time": "2025-07-01"},
    ]
    out = _refresh(sales)
    assert out["source"] == "exact_latest_sale"
    assert out["value"] == 1500
    assert out["latest_sale_date"] == "2025-06-01"
    assert out["exact_count"] == 2
    assert "最近同款成交" in out["note"]
    # 展示样本也只包含真同款
    assert all("Silver" not in s["title"] for s in out["samples"])


def test_refresh_returns_insufficient_when_no_exact_match(no_archive):
    sales = [
        {"id": "1", "title": "2022 Panini Prizm 东契奇 Silver 25/99",
         "price": 8000, "end_time": "2025-01-01"},
        {"id": "2", "title": "2022 Panini Prizm 东契奇 Base 25/199",
         "price": 400, "end_time": "2025-02-01"},
    ]
    out = _refresh(sales)
    assert out["source"] == "insufficient"
    assert out["value"] == 0
    assert out["sample"] == 0
    assert out["samples"] == []
    assert out["note"] == main.INSUFFICIENT_NOTE


def test_refresh_merges_archive_pool(monkeypatch):
    """档案是累积池：本次没抓到、但档案里有同款，也要能定出价。"""
    monkeypatch.setattr(main, "_archive_sales", lambda *a, **k: (0, 0))
    monkeypatch.setattr(main, "_load_archived_sales", lambda *a, **k: [
        {"item_id": "old-1", "title": TITLE_BASE, "price": 1234,
         "sold_date": "2025-04-04"},
    ])
    out = _refresh([])
    assert out["value"] == 1234
    assert out["archive_size"] == 1


# ------------------------------------------------------------------ 采纳估值接口

PRICE_CASES = [
    ("1200", 1200.0),
    ("¥1,200", 1200.0),
    ("1,200.50", 1200.5),
    (" 1500 元 ", 1500.0),
    (1200, 1200.0),
]


@pytest.mark.parametrize("raw,expect", PRICE_CASES)
def test_clean_price_accepts_common_formats(raw, expect):
    assert main._clean_price(raw) == expect


@pytest.mark.parametrize("raw", [None, "", "undefined", "null", "abc", True, "  "])
def test_clean_price_rejects_unparseable(raw):
    """清洗不出来的一律 None（True/空/undefined 都不能变成价）。"""
    assert main._clean_price(raw) is None


@pytest.mark.parametrize("raw,expect", [(0, 0.0), (-5, -5.0), ("-5", -5.0)])
def test_clean_price_passes_through_nonpositive(raw, expect):
    """非正数交由接口层拒绝（400），清洗层不偷偷改值。"""
    assert main._clean_price(raw) == expect


def test_apply_quote_accepts_cleaned_form_value(client):
    """「采纳FMV为估值」必须能吃下 ¥1,200 这种写法，而不是 422。"""
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]
    res = client.post(f"/api/cards/{card_id}/apply-quote", data={"price": "¥1,200"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["est_raw_price"] == 1200.0
    assert client.get(f"/api/cards/{card_id}").json()["est_raw_price"] == 1200.0


def test_apply_quote_accepts_json_body(client):
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]
    res = client.post(f"/api/cards/{card_id}/apply-quote", json={"price": 1999})
    assert res.status_code == 200
    assert res.json()["est_raw_price"] == 1999.0


def test_apply_quote_reports_bad_price_instead_of_unknown_error(client):
    """以前这里会 422/500，前端只能显示「采纳失败: 未知错误」。"""
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]
    res = client.post(f"/api/cards/{card_id}/apply-quote", data={"price": "undefined"})
    assert res.status_code == 400
    assert "估价" in res.json()["message"]


def test_apply_quote_unknown_card_404(client):
    res = client.post("/api/cards/不存在/apply-quote", data={"price": "100"})
    assert res.status_code == 404


# ------------------------------------------------------------------ 重估接口

def _patch_scrapling(monkeypatch, sales):
    def fake_post(path, payload, timeout=90):
        return {"status": "success", "recent_sales": sales}, None
    monkeypatch.setattr(main, "_scrapling_post", fake_post)


def test_revalue_applies_latest_exact_sale(client, monkeypatch, no_archive):
    _patch_scrapling(monkeypatch, [
        {"id": "s1", "title": TITLE_BASE, "price": 1888, "end_time": "2025-03-05"},
        {"id": "s2", "title": TITLE_BASE, "price": 999, "end_time": "2024-03-05"},
    ])
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]

    res = client.post(f"/api/revalue/card/{card_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["price_source"] == "exact_latest_sale"
    assert body["fair_market_value"] == 1888
    assert body["latest_sale_date"] == "2025-03-05"
    # median_price 只是兼容字段，值必须也是最近成交价
    assert body["median_price"] == 1888

    card = client.get(f"/api/cards/{card_id}").json()
    assert card["est_raw_price"] == 1888


def test_revalue_keeps_manual_estimate_when_no_exact_match(client, monkeypatch, no_archive):
    _patch_scrapling(monkeypatch, [
        {"id": "s1", "title": "2022 Panini Prizm 东契奇 Silver 25/99",
         "price": 8000, "end_time": "2025-03-05"},
        {"id": "s2", "title": "2022 Panini Prizm 东契奇 Base 25/99 PSA 10",
         "price": 3000, "end_time": "2025-04-05"},
    ])
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]

    body = client.post(f"/api/revalue/card/{card_id}").json()
    assert body["status"] == "insufficient"
    assert body["new_value"] == 1500          # 人工估值原样保留
    assert body["valuation_note"] == main.INSUFFICIENT_NOTE
    assert body["filtered_sales"] == []

    card = client.get(f"/api/cards/{card_id}").json()
    assert card["est_raw_price"] == 1500
    assert card["valuation_status"] == "insufficient"


def test_revalue_dry_run_does_not_write(client, monkeypatch, no_archive):
    _patch_scrapling(monkeypatch, [
        {"id": "s1", "title": TITLE_BASE, "price": 1888, "end_time": "2025-03-05"},
    ])
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]

    body = client.post(f"/api/revalue/card/{card_id}", params={"apply": "false"}).json()
    assert body["fair_market_value"] == 1888
    assert body["applied"] is False
    assert client.get(f"/api/cards/{card_id}").json()["est_raw_price"] == 1500


def test_valuation_check_exposes_exact_diagnostics(client, monkeypatch, no_archive):
    _patch_scrapling(monkeypatch, [
        {"id": "s1", "title": TITLE_BASE, "price": 1888, "end_time": "2025-03-05"},
    ])
    card_id = client.post("/api/cards", data=CARD_FORM).json()["card_id"]
    body = client.get(f"/api/cards/{card_id}/valuation-check").json()
    rv = body["revaluation"]
    assert rv["status"] == "success"
    assert rv["price_basis"] == "latest_exact_sale"
    assert rv["latest_sale_date"] == "2025-03-05"


# ------------------------------------------------------------------ 行情展示口径

def test_filter_quote_samples_kills_median_even_without_filtering():
    """没过滤掉任何样本时也必须改写口径。

    这里是最隐蔽的一处中位数泄漏：radar 回传 median_rmb，早退逻辑一旦原样
    返回，前端主数字就还是中位数。用户明令禁止。
    """
    q = {
        "median_rmb": 999,
        "sample_count": 2,
        "samples": [
            {"title": TITLE_BASE, "price": 1500, "closed_str": "2025-06-01"},
            {"title": TITLE_BASE, "price": 900, "closed_str": "2024-01-01"},
        ],
    }
    out = main._filter_quote_samples(q, CARD)
    assert out["median_rmb"] == 1500          # 最近一笔，不是 (1500+900)/2
    assert out["latest_rmb"] == 1500
    assert out["latest_closed"] == "2025-06-01"
    assert out["price_basis"] == "latest_exact_sale"


def test_filter_quote_samples_clears_when_nothing_left():
    q = {
        "median_rmb": 999,
        "samples": [{"title": "2022 Topps Chrome 詹姆斯 Silver", "price": 5000,
                     "closed_str": "2025-06-01"}],
    }
    out = main._filter_quote_samples(q, CARD)
    assert out["median_rmb"] == 0
    assert out["sample_count"] == 0
    assert out["price_basis"] == "insufficient"


def test_quote_for_exposes_price_basis():
    quotes = {"c1": {"median_rmb": 100, "latest_rmb": 1200,
                     "latest_closed": "2025-05-01", "sample_count": 3,
                     "samples": []}}
    q = main._quote_for(quotes, "c1")
    assert q["price_basis"] == "latest_sale"
    assert q["latest_sale_date"] == "2025-05-01"

    quotes2 = {"c2": {"median_rmb": 100, "latest_rmb": 0, "samples": []}}
    assert main._quote_for(quotes2, "c2")["price_basis"] == "insufficient"


def test_revalue_batch_separates_insufficient_from_failed(client, monkeypatch, no_archive):
    _patch_scrapling(monkeypatch, [
        {"id": "s1", "title": "2022 Panini Prizm 东契奇 Silver 25/99",
         "price": 8000, "end_time": "2025-03-05"},
    ])
    client.post("/api/cards", data=CARD_FORM)
    body = client.post("/api/revalue/batch", params={"limit": 5, "apply": "false"}).json()
    assert body["status"] == "success"
    assert body["insufficient"] == 1          # 无同款 = 正确结果，不算失败
    assert body["failed"] == 0
