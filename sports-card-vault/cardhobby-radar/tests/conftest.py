# -*- coding: utf-8 -*-
"""雷达测试前置。

cardhobby_sniper_radar.py 是单文件模块，模块级会读环境变量并算出
data/config/logs 三个目录常量，所以必须在导入前把 RADAR_BASE_DIR
指到临时目录，否则会往真实数据目录里写东西。
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="radar-tests-")
for sub in ("data", "config", "logs"):
    os.makedirs(os.path.join(_TMP, sub), exist_ok=True)
os.environ["RADAR_BASE_DIR"] = _TMP
os.environ.pop("PUSHPLUS_TOKEN", None)   # 测试里绝不允许真的推送


def pytest_sessionfinish(session, exitstatus):
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
