# -*- coding: utf-8 -*-
"""未捕获异常必须折叠成 JSON（而不是纯文本 500）。

线上连着踩了两次：aspect KeyError、auto_detect_borders AttributeError。
两次 Starlette 都返回纯文本 "Internal Server Error"，前端 res.json() 只能报
「Unexpected token 'I', "Internal S"... is not valid JSON」——
真正的原因被完全遮住，每次都要进容器翻日志才能定位。

这个用例把「500 也必须是 JSON」钉死，防止再退回纯文本。
"""
import json

from app import main as app_main


def test_unhandled_exception_returns_json_not_plain_text(client_no_raise, monkeypatch):
    def boom():
        raise RuntimeError("模拟数据库炸了")

    # get_card 里 get_db() 不在 try 内，异常会冒泡到全局处理器
    monkeypatch.setattr(app_main, "get_db", boom)

    res = client_no_raise.get("/api/cards/任意卡号")

    assert res.status_code == 500
    # 关键：必须是 JSON，前端 res.json() 才不会二次报错
    body = json.loads(res.text)
    assert "detail" in body
    assert "RuntimeError" in body["detail"]
    assert "模拟数据库炸了" in body["detail"]


def test_error_body_is_parseable_by_frontend(client_no_raise, monkeypatch):
    monkeypatch.setattr(app_main, "get_db", lambda: (_ for _ in ()).throw(ValueError("x")))

    res = client_no_raise.get("/api/cards/任意卡号")

    assert res.headers["content-type"].startswith("application/json")
    json.loads(res.text)   # 不应抛异常
