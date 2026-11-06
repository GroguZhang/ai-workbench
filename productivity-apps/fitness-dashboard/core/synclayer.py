# -*- coding: utf-8 -*-
"""CSV 权威源 ↔ 本地 SQLite 缓存 双向同步。

设计前提（多机部署）：
- 群晖同步目录里的 body_records.csv 是**唯一权威源**
- 每台机器的 SQLite 只是本地缓存，随时可从 CSV 重建
- 因此 SQLite 放在 ~/.drzfitness/，不进同步目录（避免写坏）

合并策略：
- CSV → SQLite：以 CSV 为准覆盖同日期记录（另一台机器的数据优先）
- SQLite → CSV：把合并后的全量写回，不会丢本机手动录入的日期
"""
from pathlib import Path

from . import db, importer
from .paths import find_workspace_csv, CSV_FILENAME, PROJECT_ROOT


def get_authoritative_csv() -> Path:
    """返回权威 CSV 路径，不存在时回退到项目内 data/。"""
    p = find_workspace_csv()
    if p:
        return p
    fallback = PROJECT_ROOT / "data" / CSV_FILENAME
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


def pull(csv_path: Path = None) -> tuple[int, list[str]]:
    """CSV → SQLite。返回 (导入条数, 跳过列表)。"""
    path = csv_path or get_authoritative_csv()
    if not path or not path.exists():
        return 0, []
    db.init_db()
    return importer.import_csv(path.read_bytes())


def push(csv_path: Path = None) -> int:
    """SQLite → CSV，写回权威源。返回写出的记录条数。"""
    path = csv_path or get_authoritative_csv()
    db.init_db()
    records = db.get_body_range()
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(importer.export_csv(records), encoding="utf-8")
    return len(records)


def sync() -> dict:
    """完整同步：先拉后推。每次会话启动时调用一次。"""
    path = get_authoritative_csv()
    db.init_db()
    before = {r["date"] for r in db.get_body_range()}
    n_import, skipped = pull(path)
    after = db.get_body_range()
    after_dates = {r["date"] for r in after}
    n_export = push(path)
    return {
        "csv": str(path),
        "imported": n_import,
        "exported": n_export,
        "new_from_csv": sorted(after_dates - before),
        "total": len(after),
        "skipped": skipped,
    }


def ensure_synced() -> dict:
    """每个 Streamlit 会话只同步一次，避免反复写 CSV 触发群晖同步风暴。"""
    try:
        import streamlit as st
        if st.session_state.get("_synced"):
            return st.session_state["_sync_result"]
        result = sync()
        st.session_state["_synced"] = True
        st.session_state["_sync_result"] = result
        return result
    except ImportError:
        return sync()
