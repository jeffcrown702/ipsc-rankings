"""IPSC 排名系統 - 數據庫模型（雙模式：SQLite / PostgreSQL）"""
import os, sys

# 自動檢測模式：Vercel 環境用 PostgreSQL（需有 DATABASE_URL），否則用 SQLite
_IS_VERCEL = os.environ.get("VERCEL") == "1"
_DATABASE_URL = (os.environ.get("DATABASE_URL") or
                 os.environ.get("NEON_DATABASE_URL") or
                 os.environ.get("POSTGRES_URL") or
                 os.environ.get("POSTGRES_PRISMA_URL") or
                 "")
USE_POSTGRES = _IS_VERCEL and _DATABASE_URL != "" or os.environ.get("DATABASE_URL") is not None
DATABASE_URL = _DATABASE_URL  # always available, may be empty for SQLite

if not USE_POSTGRES:
    import sqlite3
    from core.config import DB_PATH


def get_db():
    """獲取數據庫連接（自動選擇 SQLite 或 PostgreSQL）"""
    if USE_POSTGRES and DATABASE_URL:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            pass
        if 'psycopg2' in sys.modules:
            try:
                conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
                conn.autocommit = False
                return conn
            except Exception:
                pass
    import sqlite3
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_cursor(db):
    """獲取 cursor，PostgreSQL 用 RealDictCursor 支援 dict(row)，
    同時自動轉換 SQL placeholder ? → %s 俾 PostgreSQL"""
    if USE_POSTGRES and DATABASE_URL:
        import psycopg2.extras
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return cur
    return db.cursor()


def get_cursor(conn):
    """獲取 cursor（兼容 sqlite3 Row 和 psycopg2 DictCursor）"""
    if USE_POSTGRES:
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def init_db():
    """初始化數據庫表結構"""
    conn = get_db()
    cur = get_cursor(conn)

    if USE_POSTGRES:
        # PostgreSQL 版 SQL
        cur.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                date TEXT,
                venue TEXT,
                level TEXT,
                url TEXT,
                is_completed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_scraped TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shooters (
                id SERIAL PRIMARY KEY,
                match_id INTEGER NOT NULL REFERENCES matches(id),
                competitor_number INTEGER NOT NULL,
                name TEXT,
                division TEXT,
                class TEXT,
                factor TEXT,
                category TEXT,
                region TEXT,
                total_score REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shooters_unique
            ON shooters(match_id, competitor_number)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stage_scores (
                id SERIAL PRIMARY KEY,
                shooter_id INTEGER NOT NULL REFERENCES shooters(id),
                match_id INTEGER NOT NULL REFERENCES matches(id),
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_stage_scores_shooter
            ON stage_scores(shooter_id, stage_number)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rankings (
                id SERIAL PRIMARY KEY,
                match_id INTEGER NOT NULL,
                division TEXT NOT NULL,
                rank_type TEXT NOT NULL,
                group_key TEXT DEFAULT '',
                competitor_number INTEGER NOT NULL,
                place INTEGER DEFAULT 0,
                total_score REAL DEFAULT 0,
                score_percent REAL DEFAULT 0,
                calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rankings_lookup
            ON rankings(match_id, division, rank_type, group_key)
        """)
    else:
        # SQLite 版 SQL
        cur.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                date TEXT,
                venue TEXT,
                level TEXT,
                url TEXT,
                is_completed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                last_scraped TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shooters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                competitor_number INTEGER NOT NULL,
                name TEXT,
                division TEXT,
                class TEXT,
                factor TEXT,
                category TEXT,
                region TEXT,
                total_score REAL DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (match_id) REFERENCES matches(id),
                UNIQUE(match_id, competitor_number)
            )
        """)
        cur.execute("""
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
                FOREIGN KEY (match_id) REFERENCES matches(id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_stage_scores_shooter
            ON stage_scores(shooter_id, stage_number)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                division TEXT NOT NULL,
                rank_type TEXT NOT NULL,
                group_key TEXT DEFAULT '',
                competitor_number INTEGER NOT NULL,
                place INTEGER DEFAULT 0,
                total_score REAL DEFAULT 0,
                score_percent REAL DEFAULT 0,
                calculated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rankings_lookup
            ON rankings(match_id, division, rank_type, group_key)
        """)

    conn.commit()
    conn.close()
    print(f"[DB] 數據庫初始化完成 ({'PostgreSQL' if USE_POSTGRES else 'SQLite'})")


def dict_from_row(row):
    """將 database row 轉為 dict（兼容 sqlite3.Row 和 psycopg2 RealDictRow）"""
    if row is None:
        return None
    if hasattr(row, 'keys'):
        return dict(row)
    return dict(row)


def fetch_one_as_dict(cur):
    """Fetch one row and return as dict"""
    row = cur.fetchone()
    if row is None:
        return None
    return dict_from_row(row)


def fetch_all_as_dict(cur):
    """Fetch all rows and return as list of dicts"""
    rows = cur.fetchall()
    return [dict_from_row(r) for r in rows]
