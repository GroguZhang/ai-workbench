# -*- coding: utf-8 -*-
"""测试前置：确保 app.py 所在目录在 sys.path 上。"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
