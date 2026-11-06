# -*- coding: utf-8 -*-
"""CSV / JSON 导入导出。兼容现有健身工作区的 body_records.csv 格式。"""
import csv
import io
import json
from pathlib import Path
from . import db

# 与群晖同步工作区 data/body_records.csv 完全一致的列序
CSV_HEADER = [
    "date","source","weight_kg","bmi","bodyfat_pct","fat_mass_kg","ffm_kg",
    "muscle_kg","muscle_pct","skeletal_muscle_kg","visceral_fat_lvl",
    "subcutaneous_fat_pct","water_pct","protein_pct","bone_kg",
    "sleep_h","sleep_score","steps","weekly_strength_min","weekly_cardio_min","note"
]

# CSV 列名 → DB 字段映射
CSV_MAP = {
    "date": "date", "weight_kg": "weight", "bmi": "bmi", "bodyfat_pct": "bodyfat_pct",
    "fat_mass_kg": "fat_mass_kg", "ffm_kg": "ffm_kg", "muscle_kg": "muscle_kg",
    "muscle_pct": "muscle_pct", "skeletal_muscle_kg": "skeletal_muscle_kg",
    "visceral_fat_lvl": "visceral_fat_lvl", "subcutaneous_fat_pct": "subcutaneous_fat_pct",
    "water_pct": "water_pct", "protein_pct": "protein_pct", "bone_kg": "bone_kg",
    "sleep_h": "sleep_h", "sleep_score": "sleep_score", "steps": "steps",
    "note": "note",
}


def import_csv(file_bytes: bytes) -> tuple[int, list[str]]:
    """从 CSV 字节流导入。返回 (成功条数, 跳过条目列表)。"""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    skipped = []
    for row in reader:
        rec = {}
        for csv_col, db_col in CSV_MAP.items():
            val = row.get(csv_col)
            if val in (None, "", "nan"):
                continue
            rec[db_col] = _cast(val)
        if not rec.get("date"):
            skipped.append(f"无日期: {row}")
            continue
        try:
            db.upsert_body(rec)
            count += 1
        except Exception as e:
            skipped.append(f"{rec.get('date')}: {e}")
    return count, skipped


def import_json(file_bytes: bytes) -> tuple[int, list[str]]:
    """从 JSON 数组导入。"""
    data = json.loads(file_bytes.decode("utf-8"))
    if isinstance(data, dict):
        data = [data]
    count = 0
    skipped = []
    for rec in data:
        if not rec.get("date"):
            skipped.append("无日期")
            continue
        try:
            db.upsert_body(rec)
            count += 1
        except Exception as e:
            skipped.append(f"{rec.get('date')}: {e}")
    return count, skipped


def export_csv(records: list[dict]) -> str:
    """导出为 CSV 字符串（与现有工作区格式兼容）。"""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_HEADER)
    writer.writeheader()
    for r in records:
        row = {}
        for col in CSV_HEADER:
            row[col] = r.get(col, "")
        writer.writerow(row)
    return out.getvalue()


def _cast(val: str):
    val = val.strip()
    if val.lstrip("-").replace(".", "", 1).isdigit():
        return float(val) if "." in val else int(val)
    return val
