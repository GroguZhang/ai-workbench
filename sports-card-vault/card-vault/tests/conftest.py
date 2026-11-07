# -*- coding: utf-8 -*-
"""测试前置：把数据目录指向临时目录，并把工作目录切到 card-vault 根。

main.py 在 import 期就会建目录、挂载静态目录、执行 init_db()，并把 templates
指向相对路径 "app/templates"，所以必须在导入前准备好环境，否则会在真实磁盘上
留下痕迹。
"""
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cardvault-tests-")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")

# 相对路径 app/templates 需要以 card-vault 根为 cwd 才能解析
os.chdir(ROOT)


@pytest.fixture()
def client():
    """HTTP 契约测试用。TestClient 以上下文管理器方式使用，才会走 lifespan。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_no_raise():
    """与 client 相同，但服务器内部异常会被折叠成 500 响应而不是抛到测试里。

    验证全局异常处理器必须用它——否则 TestClient 会直接把原始异常重新抛出，
    根本走不到处理器。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_cards():
    """每个用例前清空 cards 表，保证用例之间不互相污染。"""
    from app import main

    conn = main.get_db()
    conn.execute("DELETE FROM cards")
    conn.commit()
    conn.close()
    yield


def pytest_sessionfinish(session, exitstatus):
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
