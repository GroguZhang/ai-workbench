# -*- coding: utf-8 -*-
"""SQLite 数据层：表结构定义 + CRUD。"""
import sqlite3
import json
from datetime import date, datetime

from .paths import DB_PATH  # 数据库在本机 ~/.drzfitness/，不在同步目录

SCHEMA = """
CREATE TABLE IF NOT EXISTS body_records (
    date TEXT PRIMARY KEY,
    weight REAL, bmi REAL, bodyfat_pct REAL, fat_mass_kg REAL, ffm_kg REAL,
    muscle_kg REAL, skeletal_muscle_kg REAL, visceral_fat_lvl REAL,
    water_pct REAL, sleep_h REAL, sleep_score REAL, deep_sleep_h REAL,
    steps INTEGER, resting_hr INTEGER, hrv INTEGER, energy_kcal INTEGER,
    note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS training_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, template_name TEXT, movement TEXT NOT NULL,
    sets INTEGER, reps REAL, weight REAL, unit TEXT, rpe REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS ai_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, plan_type TEXT, content TEXT, created_at TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ---- body_records CRUD ----
BODY_COLS = [
    "date","weight","bmi","bodyfat_pct","fat_mass_kg","ffm_kg","muscle_kg",
    "skeletal_muscle_kg","visceral_fat_lvl","water_pct","sleep_h",
    "sleep_score","deep_sleep_h","steps","resting_hr","hrv","energy_kcal","note"
]

def upsert_body(rec: dict) -> None:
    init_db()
    conn = get_conn()
    rec.setdefault("created_at", datetime.now().isoformat())
    cols = [c for c in BODY_COLS if c in rec] + ["created_at"]
    vals = [rec.get(c) for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    updates = ",".join([f"{c}=excluded.{c}" for c in cols if c != "date"])
    sql = (f"INSERT INTO body_records ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(date) DO UPDATE SET {updates}")
    conn.execute(sql, vals)
    conn.commit()
    conn.close()


def get_body_range(start: str = None, end: str = None) -> list[dict]:
    init_db()
    conn = get_conn()
    if start and end:
        rows = conn.execute("SELECT * FROM body_records WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM body_records ORDER BY date DESC LIMIT 500").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_body(date_str: str) -> None:
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM body_records WHERE date=?", (date_str,))
    conn.commit()
    conn.close()


# ---- training_logs CRUD ----
def add_training(rec: dict) -> int:
    init_db()
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO training_logs (date,template_name,movement,sets,reps,weight,unit,rpe,note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rec.get("date"), rec.get("template_name"), rec.get("movement"),
         rec.get("sets"), rec.get("reps"), rec.get("weight"),
         rec.get("unit","kg"), rec.get("rpe"), rec.get("note"))
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_training_range(start: str = None, end: str = None) -> list[dict]:
    init_db()
    conn = get_conn()
    if start and end:
        rows = conn.execute("SELECT * FROM training_logs WHERE date BETWEEN ? AND ? ORDER BY date,movement", (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM training_logs ORDER BY date DESC LIMIT 500").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_training(tid: int) -> None:
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM training_logs WHERE id=?", (tid,))
    conn.commit()
    conn.close()


# ---- ai_plans CRUD ----
def save_plan(date_str: str, plan_type: str, content: str) -> int:
    init_db()
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO ai_plans (date,plan_type,content,created_at) VALUES (?,?,?,?)",
        (date_str, plan_type, content, datetime.now().isoformat())
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_plans(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM ai_plans ORDER BY date DESC,id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
