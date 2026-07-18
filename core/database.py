"""IPSC 排名系統 - 數據庫模型"""
import sqlite3
import os
from config import DB_PATH


def get_db():
    """獲取數據庫連接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化數據庫表結構"""
    conn = get_db()
    cursor = conn.cursor()

    # 比賽表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            date TEXT,
            venue TEXT,
            level TEXT,
            url TEXT,
            is_completed INTEGER DEFAULT 0,
            last_scraped TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 選手表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            competitor_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            division TEXT,
            class TEXT,
            factor TEXT,
            category TEXT,
            region TEXT DEFAULT 'HKG',
            total_score REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(match_id, competitor_number)
        )
    """)

    # Stage 成績表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stage_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shooter_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            stage_number INTEGER NOT NULL,
            stage_name TEXT,
            hit_factor REAL DEFAULT 0,
            pts INTEGER DEFAULT 0,
            a INTEGER DEFAULT 0,
            c INTEGER DEFAULT 0,
            d INTEGER DEFAULT 0,
            mi INTEGER DEFAULT 0,
            ns INTEGER DEFAULT 0,
            pe INTEGER DEFAULT 0,
            time REAL DEFAULT 0,
            stage_score REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (shooter_id) REFERENCES shooters(id),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(shooter_id, stage_number)
        )
    """)

    # 排名快取表 (四大維度)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            division TEXT NOT NULL,
            rank_type TEXT NOT NULL,
            group_key TEXT,
            competitor_number INTEGER NOT NULL,
            place INTEGER NOT NULL,
            total_score REAL DEFAULT 0,
            score_percent REAL DEFAULT 0,
            calculated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(match_id, division, rank_type, group_key, competitor_number)
        )
    """)

    # Scraping 紀錄
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            shooters_found INTEGER DEFAULT 0,
            stages_found INTEGER DEFAULT 0,
            status TEXT DEFAULT 'success',
            message TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (match_id) REFERENCES matches(id)
        )
    """)

    conn.commit()
    conn.close()
    print(f"[DB] 數據庫初始化完成: {DB_PATH}")


if __name__ == "__main__":
    init_db()
