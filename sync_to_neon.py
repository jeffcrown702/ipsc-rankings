#!/usr/bin/env python3
"""Sync local SQLite IPSC rankings to Neon PostgreSQL."""
import sys, os, sqlite3, psycopg2

NEON_URL = 'postgresql://neondb_owner:npg_dKiIVuJ4kaA1@ep-soft-voice-azr1ah9f-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'
SQLITE_PATH = r'E:\ctb988\ipsc-rankings\data\ipsc_rankings.db'

src = sqlite3.connect(SQLITE_PATH)
src.row_factory = sqlite3.Row

pg = psycopg2.connect(NEON_URL)
cur = pg.cursor()

# Delete in correct FK order: stage_scores -> rankings -> shooters
cur.execute("DELETE FROM stage_scores")
cur.execute("DELETE FROM rankings")
cur.execute("DELETE FROM shooters")
pg.commit()
print("Cleared dependent tables")

# Neon column sets (matching exactly what Neon tables have)
TABLES = {
    "matches": ["id", "name", "date", "venue", "level", "url", "is_completed", "created_at", "last_scraped"],
    "shooters": ["id", "match_id", "competitor_number", "name", "division", "class", "factor", "category", "region", "total_score", "updated_at"],
    "stage_scores": ["id", "shooter_id", "match_id", "stage_number", "stage_name", "hit_factor", "pts", "a", "c", "d", "mi", "ns", "pe", "time", "stage_score", "created_at"],
    "rankings": ["id", "match_id", "division", "rank_type", "group_key", "competitor_number", "place", "total_score", "score_percent", "calculated_at"],
}

for table, cols in TABLES.items():
    rows = [dict(r) for r in src.execute(f'SELECT {",".join(cols)} FROM {table}').fetchall()]
    print(f"Read {len(rows)} rows from {table}")

    if not rows:
        print(f"  Skipping {table} (no data)")
        continue

    if table == "matches":
        for r in rows:
            ph = ",".join(["%s"] * len(cols))
            cn = ",".join(cols)
            vals = tuple(r[c] for c in cols)
            cur.execute(
                f"INSERT INTO {table} ({cn}) VALUES ({ph}) "
                f"ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != 'id'),
                vals
            )
    else:
        for r in rows:
            ph = ",".join(["%s"] * len(cols))
            cn = ",".join(cols)
            vals = tuple(r[c] for c in cols)
            cur.execute(f"INSERT INTO {table} ({cn}) VALUES ({ph})", vals)

    pg.commit()
    print(f"  Synced {len(rows)} rows to {table}")

# Verify counts
print("\n--- Verification ---")
for t in TABLES:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    cnt = cur.fetchone()[0]
    print(f"{t}: {cnt} rows")

cur.close()
pg.close()
src.close()
print("\nSync complete!")
