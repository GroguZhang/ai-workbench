# -*- coding: utf-8 -*-
"""统一路径解析。

多机部署的核心原则：
- 代码 + CSV 主档 → 群晖同步目录（随 Drive 在多机间同步）
- SQLite + venv + .env → 各机器本地目录（~/.drzfitness/，绝不同步）

SQLite 放进同步目录会在写入时被同步客户端抓取到损坏的中间状态，
.env 放进同步目录会让 API Key 落盘到 NAS。
"""
import os
from pathlib import Path

# 群晖同步目录里的项目根（代码所在）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 本机运行时目录（可用环境变量 DRZ_LOCAL_DIR 覆盖）
LOCAL_DIR = Path(os.getenv("DRZ_LOCAL_DIR", Path.home() / ".drzfitness"))
LOCAL_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = LOCAL_DIR / "fitness.db"
ENV_FILE = LOCAL_DIR / ".env"
VENV_DIR = LOCAL_DIR / ".venv"

# 权威数据源：群晖同步的 body_records.csv
CSV_FILENAME = "body_records.csv"


def find_workspace_csv() -> Path | None:
    """定位权威 CSV。优先级：环境变量 > 兄弟工作区目录 > 项目自身 data/"""
    env = os.getenv("DRZ_WORKSPACE_CSV")
    if env:
        p = Path(env)
        return p if p.exists() else None

    candidates = [
        PROJECT_ROOT.parent / "健身记录" / "data" / CSV_FILENAME,   # 群晖 Drive 里的兄弟目录
        PROJECT_ROOT / "data" / CSV_FILENAME,                        # 项目自带
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def describe() -> str:
    """用于设置页展示的路径诊断信息。"""
    csv = find_workspace_csv()
    return (
        f"- 项目目录（同步）：`{PROJECT_ROOT}`\n"
        f"- 本机运行时（不同步）：`{LOCAL_DIR}`\n"
        f"- 数据库：`{DB_PATH}`\n"
        f"- 配置文件：`{ENV_FILE}`{'（已配置）' if ENV_FILE.exists() else '（未配置）'}\n"
        f"- 权威 CSV：{f'`{csv}`' if csv else '未找到 ⚠'}"
    )
