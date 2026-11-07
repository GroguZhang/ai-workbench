# -*- coding: utf-8 -*-
"""HTTP 契约测试：健康检查与上游失败时的错误映射。

不发真实网络请求——上游用 monkeypatch 替身。
"""
import pytest
from fastapi.testclient import TestClient

import app as scrapling_app


@pytest.fixture()
def client():
    return TestClient(scrapling_app.app)


def test_health_reports_status_and_config(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "healthy"
    # 这三个字段是运维排查时唯一的线索来源，缺一个就等于盲飞
    for key in ("version", "dsh_api_base", "dsh_model", "vault_api_base"):
        assert key in body


def test_parse_card_maps_upstream_failure_to_502(client, monkeypatch):
    class Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("upstream down")

    monkeypatch.setattr(scrapling_app.httpx, "AsyncClient", Boom)

    res = client.post("/api/ai/parse-card", json={"title": "2024 Prizm Gold"})
    assert res.status_code == 502
    assert "调用 DSH 失败" in res.json()["detail"]


def test_parse_card_maps_non_200_upstream_to_502(client, monkeypatch):
    class BadStatus:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            class R:
                status_code = 500
                text = "boom"
            return R()

    monkeypatch.setattr(scrapling_app.httpx, "AsyncClient", BadStatus)

    res = client.post("/api/ai/parse-card", json={"title": "x"})
    assert res.status_code == 502
    assert "DSH 返回 500" in res.json()["detail"]
