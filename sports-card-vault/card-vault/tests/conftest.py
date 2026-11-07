# -*- coding: utf-8 -*-
"""测试前置：把数据目录指向临时目录，并把工作目录切到 card-vault 根。

main.py 在 import 期就会建目录、挂载静态目录，并把 templates 指向相对路径
"app/templates"，所以必须在导入前准备好环境，否则会在真实磁盘上留下痕迹。
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cardvault-tests-")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")

# 相对路径 app/templates 需要以 card-vault 根为 cwd 才能解析
os.chdir(ROOT)


def pytest_sessionfinish(session, exitstatus):
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
