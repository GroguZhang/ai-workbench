# -*- coding: utf-8 -*-
"""命令行手动同步：权威 CSV（群晖同步）↔ 本机 SQLite 缓存。

用法（任意机器）：
    python sync_workspace.py           # 双向同步
    python sync_workspace.py --status  # 只看状态

多机部署说明：
- CSV 在群晖同步目录，是唯一权威源
- SQLite 在本机 ~/.drzfitness/，仅是缓存
- 每台机器打开仪表盘时自动同步，此脚本供命令行手动触发
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import synclayer, paths


def main() -> None:
    if "--status" in sys.argv:
        print(paths.describe())
        csv = synclayer.get_authoritative_csv()
        if csv and csv.exists():
            lines = csv.read_text(encoding="utf-8-sig").strip().splitlines()
            print(f"CSV 行数：{len(lines) - 1} 条记录")
        return

    r = synclayer.sync()
    print(json.dumps({
        "csv": r["csv"],
        "imported": r["imported"],
        "exported": r["exported"],
        "new_from_csv": r["new_from_csv"],
        "total_records": r["total"],
        "skipped": len(r["skipped"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
