import sys, os, requests, re, time
sys.path.insert(0, r'E:\ctb988\ipsc-rankings')
os.environ['DATABASE_URL'] = 'postgresql://neondb_owner:npg_dKiIVuJ4kaA1@ep-soft-voice-azr1ah9f-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'
from core import database
database.USE_POSTGRES = True
database.DATABASE_URL = os.environ['DATABASE_URL']
from core.database import get_db, get_cursor

# 用 PostgreSQL advisory lock (id=888) 防止並行 recalc 重疊
db = get_db()
c = get_cursor(db)
c.execute("SELECT pg_try_advisory_lock(888)")
if not c.fetchone()['pg_try_advisory_lock']:
    print('SKIP: 另一個 recalc 進行中 (advisory lock held)')
    db.close()
    sys.exit(0)
print('Got advisory lock')

try:
    # 1. Sync match list
    try:
        r = requests.get('https://hkg.as.ipscess.org/portal', timeout=15, headers={'User-Agent':'Mozilla/5.0 Chrome/125.0'})
        r.encoding = r.apparent_encoding
        from core.scraper import parse_matches
        for m in parse_matches(r.text):
            c.execute("""INSERT INTO matches (id, name, date, venue, level, is_completed)
                         VALUES (%s,%s,%s,%s,%s,0)
                         ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, date=EXCLUDED.date,
                            venue=EXCLUDED.venue, level=EXCLUDED.level""",
                      (m['id'], m['name'], m.get('date',''), m.get('venue',''), m.get('level','')))
        db.commit()
        c.execute('SELECT id FROM matches WHERE is_completed = 0 ORDER BY id DESC LIMIT 1')
        mids = [row['id'] for row in c.fetchall()]
        print('Active matches:', mids)
    except Exception as e:
        print('Sync err:', str(e)[:80]); mids = []

    # 2. Scan latest match comp 1-220, skip those WITH stage data
    if mids:
        from core.scraper import parse_verify_page
        import app
        mid = mids[0]
        c.execute("""SELECT DISTINCT s.competitor_number FROM shooters s
                     JOIN stage_scores ss ON ss.shooter_id = s.id WHERE s.match_id=%s""", (mid,))
        skip = set(str(row['competitor_number']) for row in c.fetchall())
        scraped = 0
        for comp in range(1, 221):
            if str(comp) in skip:
                continue
            try:
                rr = requests.get(f'https://hkg.as.ipscess.org/portal/verify/{mid}?shooter={comp}', timeout=6, headers={'User-Agent':'Mozilla/5.0 Chrome/125.0'})
                rr.encoding = rr.apparent_encoding
                result = parse_verify_page(rr.text)
                if result and result.get('name') and result['name'] != 'Unknown' and result.get('stages'):
                    result['competitor_number'] = comp
                    result['region'] = 'HKG'
                    app._save_shooter_data(mid, result)
                    scraped += 1
            except Exception as e:
                if 'ConnectionReset' in str(e) or '10054' in str(e):
                    time.sleep(2)
        print(f'Match {mid}: scraped {scraped} new shooters')

    # 3. Recalculate
    from core.scoring_engine import calculate_all_rankings
    for mid in mids:
        try:
            calculate_all_rankings(mid)
            print(f'Match {mid}: rankings OK')
        except Exception as e:
            print(f'Match {mid}: rank err {str(e)[:60]}')
finally:
    # Release advisory lock
    try:
        db.rollback(); c.execute("SELECT pg_advisory_unlock(888)"); db.commit()
    except: pass
    db.close()
print('SCRAPE-DONE')
