# -*- coding: utf-8 -*-
"""Initialize mock database for Card Vault demonstration."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "cards.mock.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT UNIQUE,
    sport TEXT,
    player TEXT,
    year_set TEXT,
    card_type TEXT,
    serial TEXT,
    est_raw_price REAL DEFAULT 0,
    cost_price REAL DEFAULT 0,
    status TEXT DEFAULT '在库',
    grading_verdict TEXT,
    notes TEXT,
    img_front TEXT,
    img_back TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    photo_mode TEXT,
    rotate_front INTEGER DEFAULT 0,
    rotate_back INTEGER DEFAULT 0,
    img_front_orig TEXT,
    img_back_orig TEXT,
    search_query TEXT
);
""")

c.execute("""
CREATE TABLE IF NOT EXISTS radar_marks (
    item_id TEXT PRIMARY KEY,
    state TEXT,
    note TEXT,
    updated_at TEXT
);
""")

c.execute("""
CREATE TABLE IF NOT EXISTS card_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    median_rmb REAL DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    grade_bucket TEXT DEFAULT '',
    window_days INTEGER DEFAULT 0,
    basis TEXT DEFAULT ''
);
""")

# Sample Cards
cards_sample = [
    (
        "DEMO-001", "Basketball", "Victor Wembanyama", "2023-24 Panini Prizm",
        "Silver Refractor RC", "/99", 4500.0, 3200.0, "在库",
        "Centering 52/48 (Grade 9.5-10)", "RC Rookie season grail card.",
        "sample_wemby_front.jpg", "sample_wemby_back.jpg",
        "2026-11-01 10:00:00", "2026-11-01 10:00:00", "centered",
        0, 0, "sample_wemby_front.jpg", "sample_wemby_back.jpg",
        "2023-24 Panini Prizm Victor Wembanyama Silver /99"
    ),
    (
        "DEMO-002", "Tennis", "Carlos Alcaraz", "2024 Topps Chrome",
        "Gold Refractor Auto", "/50", 5800.0, 4100.0, "在库",
        "Centering 50/50 (Grade 10)", "On-card signature, pristine surface.",
        "sample_alcaraz_front.jpg", "sample_alcaraz_back.jpg",
        "2026-11-02 12:00:00", "2026-11-02 12:00:00", "centered",
        0, 0, "sample_alcaraz_front.jpg", "sample_alcaraz_back.jpg",
        "2024 Topps Chrome Carlos Alcaraz Gold Refractor Auto /50"
    )
]

for card in cards_sample:
    c.execute("""
    INSERT OR REPLACE INTO cards (
        card_id, sport, player, year_set, card_type, serial,
        est_raw_price, cost_price, status, grading_verdict, notes,
        img_front, img_back, created_at, updated_at, photo_mode,
        rotate_front, rotate_back, img_front_orig, img_back_orig, search_query
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, card)

# Sample price history
prices = [
    ("DEMO-001", "2026-10-01", 3800.0, 5, "PSA 10", 30, "eBay Median"),
    ("DEMO-001", "2026-10-15", 4100.0, 8, "PSA 10", 30, "CardHobby Fill"),
    ("DEMO-001", "2026-11-01", 4500.0, 6, "PSA 10", 30, "eBay Median"),
    ("DEMO-002", "2026-10-10", 5200.0, 3, "Raw", 30, "CardHobby Fill"),
    ("DEMO-002", "2026-11-01", 5800.0, 4, "Raw", 30, "eBay Median"),
]

for p in prices:
    c.execute("""
    INSERT INTO card_price_history (
        card_id, timestamp, median_rmb, sample_count, grade_bucket, window_days, basis
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, p)

conn.commit()
conn.close()
print("Initialized cards.mock.db successfully.")
