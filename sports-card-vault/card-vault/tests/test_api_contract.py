# -*- coding: utf-8 -*-
"""HTTP 层契约测试。

之前这个项目只测了纯函数（搜索词、图像几何），**HTTP 层完全没测**——
也就是说路由签名改了、返回结构调整了、404 变成 500 了，测试都不会红。
这一层补的就是这个空洞。
"""
import pytest

from app.main import DEFAULT_SORT

CARD_FORM = {
    "sport": "篮球",
    "player": "卢卡·东契奇",
    "year_set": "2022 Panini Prizm World Cup",
    "card_type": "Base",
    "serial": "25/99",
    "est_raw_price": "1500",
    "cost_price": "800",
    "status": "在库",
}


# ---------------------------------------------------------------- 静态选项接口

def test_sorts_endpoint_shape(client):
    res = client.get("/api/sorts")
    assert res.status_code == 200
    body = res.json()
    assert body["default"] == DEFAULT_SORT
    assert isinstance(body["sorts"], list) and body["sorts"]
    first = body["sorts"][0]
    assert set(first) == {"value", "label"}


def test_photo_modes_endpoint_shape(client):
    res = client.get("/api/photo-modes")
    assert res.status_code == 200
    body = res.json()
    # 前端下拉依赖这五项；opencv 透出与否决定自动裁切能不能跑
    for key in ("modes", "default", "opencv", "max_edge", "quality"):
        assert key in body
    assert isinstance(body["modes"], list) and body["modes"]
    assert set(body["modes"][0]) == {"value", "label"}


# ---------------------------------------------------------------- 卡片生命周期

def test_create_read_delete_lifecycle(client):
    created = client.post("/api/cards", data=CARD_FORM)
    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == "success"
    card_id = payload["card_id"]

    # 新增时服务端会自动补搜索词
    assert payload["search_query"]

    got = client.get(f"/api/cards/{card_id}")
    assert got.status_code == 200
    card = got.json()
    assert card["player"] == CARD_FORM["player"]
    assert card["sport"] == CARD_FORM["sport"]
    assert card["serial"] == CARD_FORM["serial"]
    assert card["est_raw_price"] == 1500.0
    assert card["cost_price"] == 800.0
    assert card["search_query"] == payload["search_query"]

    listed = client.get("/api/cards").json()
    assert card_id in [c["card_id"] for c in listed["cards"]]
    assert listed["stats"]["total_count"] >= 1

    removed = client.delete(f"/api/cards/{card_id}")
    assert removed.status_code == 200
    assert removed.json()["status"] == "success"

    assert client.get(f"/api/cards/{card_id}").status_code == 404


def test_search_query_is_autofilled_when_blank(client):
    res = client.post("/api/cards", data=CARD_FORM)
    q = res.json()["search_query"]
    # 年份 + 系列核心词 + 球员姓氏 + 编数分母
    assert "2022" in q and "东契奇" in q and "/99" in q


def test_explicit_search_query_is_preserved(client):
    form = dict(CARD_FORM, search_query="自定义 关键词")
    res = client.post("/api/cards", data=form)
    assert res.json()["search_query"] == "自定义 关键词"


# ---------------------------------------------------------------- 错误路径

def test_get_unknown_card_returns_404(client):
    res = client.get("/api/cards/不存在的卡号")
    assert res.status_code == 404
    assert "不存在" in res.json()["detail"]


def test_delete_unknown_card_returns_404(client):
    res = client.delete("/api/cards/不存在的卡号")
    assert res.status_code == 404


# ---------------------------------------------------------------- 列表与过滤

def test_list_filters_by_sport(client):
    client.post("/api/cards", data=CARD_FORM)
    client.post("/api/cards", data=dict(CARD_FORM, sport="网球", player="阿尔卡拉斯"))

    all_cards = client.get("/api/cards").json()["cards"]
    assert len(all_cards) == 2

    basketball = client.get("/api/cards", params={"sport": "篮球"}).json()["cards"]
    assert len(basketball) == 1
    assert basketball[0]["sport"] == "篮球"

    # "全部" 是前端的哨兵值，不能被当成真实过滤条件
    everything = client.get("/api/cards", params={"sport": "全部"}).json()["cards"]
    assert len(everything) == 2


def test_list_unknown_sort_falls_back_to_default(client):
    res = client.get("/api/cards", params={"sort": "nonsense"})
    assert res.status_code == 200
    assert res.json()["sort"] == DEFAULT_SORT


def test_list_response_envelope(client):
    body = client.get("/api/cards").json()
    # 这个信封结构是前端唯一的取数入口，改动即破坏前端
    for key in ("cards", "sort", "market", "stats"):
        assert key in body
    for key in ("total_count", "total_val", "total_cost"):
        assert key in body["stats"]


def test_stats_are_global_even_when_list_is_filtered(client):
    """stats 是**全局口径**，不随 cards 的筛选变化。

    前端把 stats 用在资产看板头部（总张数 / 总估值），那里要回答的是
    「我这个收藏总共值多少」，而不是「筛出来的这一屏值多少」。
    这条测试把它钉住，避免将来有人把它"顺手修正"成筛选后统计——
    那样资产看板会在切运动分类时数字乱跳。
    """
    client.post("/api/cards", data=CARD_FORM)
    client.post("/api/cards", data=dict(CARD_FORM, sport="网球", player="阿尔卡拉斯"))

    body = client.get("/api/cards", params={"sport": "篮球"}).json()
    assert len(body["cards"]) == 1          # 列表被筛到 1 张
    assert body["stats"]["total_count"] == 2  # 但全局统计仍是 2 张


# ---------------------------------------------------------------- 健康检查

def test_healthz_returns_ok(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_healthz_probes_database(client, monkeypatch):
    """健康检查必须真的摸数据库：只回 200 的浅检查会把 DB 故障藏起来。"""
    from app import main

    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(main, "get_db", boom)

    res = client.get("/healthz")
    assert res.status_code == 503
    assert "数据库不可用" in res.json()["detail"]
