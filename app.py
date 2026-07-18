"""IPSC 實時排名系統 - FastAPI 後端

API Endpoints:
  GET  /api/matches           — 比賽列表
  GET  /api/matches/{id}      — 比賽詳情
  GET  /api/matches/{id}/scrape — 觸發爬取
  GET  /api/matches/{id}/rankings — 四大排名數據
  GET  /api/matches/{id}/shooters — 射手列表
  GET  /api/scrape/status     — 爬取狀態
  POST /api/scrape/run        — 手動執行爬取
"""
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import os, sys, asyncio, aiohttp, threading, time
from datetime import datetime, timedelta
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(__file__))
from core.database import get_db, init_db
from core.scraper import load_config, scrape_match, sync_matches as scraper_sync_matches, parse_matches, fetch_html
from core.scoring_engine import calculate_all_rankings, calculate_division_rankings
from core.config import API_HOST, API_PORT, DIVISIONS

app = FastAPI(title="IPSC 實時排名系統", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 爬取狀態 + 線程鎖
scrape_status = {"running": False, "last_run": None, "progress": ""}
_scrape_lock = threading.Lock()
_last_auto_scrape = {}


def _should_auto_scrape(match_id, cooldown_sec=120):
    """Cooldown 檢查：同一場比賽至少隔 cooldown_sec 秒先可以再爬"""
    now = time.time()
    last = _last_auto_scrape.get(match_id, 0)
    if now - last >= cooldown_sec:
        _last_auto_scrape[match_id] = now
        return True
    return False


@app.on_event("startup")
def startup():
    init_db()
    # 定時任務：每 5 分鐘自動爬取進行中比賽
    def cron_loop():
        while True:
            time.sleep(300)  # 5 分鐘
            try:
                _auto_scrape_active_matches()
            except Exception as e:
                print(f"[CRON] 自動爬取出錯: {e}")

    t = threading.Thread(target=cron_loop, daemon=True)
    t.start()
    print("[CRON] 自動爬取已啟動（每 5 分鐘）")


# ===================== API Routes =====================

@app.get("/api/matches")
def get_matches():
    """獲取比賽列表"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT id, name, date, venue, level, is_completed, last_scraped
        FROM matches
        ORDER BY substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2) DESC, id DESC
    """)
    matches = [dict(row) for row in cursor.fetchall()]
    db.close()

    # 加 count
    for m in matches:
        db2 = get_db()
        c = db2.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM shooters WHERE match_id = ?", (m["id"],))
        row = c.fetchone()
        m["shooter_count"] = row["cnt"] if row else 0
        db2.close()

    return {"matches": matches}


@app.get("/api/matches/{match_id}")
def get_match(match_id: int):
    """獲取比賽詳情"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
    match = cursor.fetchone()
    if not match:
        db.close()
        raise HTTPException(404, "比賽唔存在")
    match_data = dict(match)

    # 取得有數據嘅 Division（跟 config 順序）
    cursor.execute("""
        SELECT DISTINCT division FROM shooters
        WHERE match_id = ? AND division IS NOT NULL AND division != ''
    """, (match_id,))
    db_divs = {r["division"] for r in cursor.fetchall()}
    # 跟 config DIVISIONS 順序排列
    from core.config import DIVISIONS
    match_data["divisions"] = [d for d in DIVISIONS if d in db_divs]

    # 射手總數
    cursor.execute("SELECT COUNT(*) as cnt FROM shooters WHERE match_id = ?", (match_id,))
    match_data["shooter_count"] = cursor.fetchone()["cnt"]

    # 最後計算時間
    cursor.execute("""
        SELECT MAX(calculated_at) as last_calc FROM rankings WHERE match_id = ?
    """, (match_id,))
    row = cursor.fetchone()
    match_data["last_calculated"] = row["last_calc"] if row else None

    db.close()
    return {"match": match_data}


@app.get("/api/matches/{match_id}/shooters")
def get_shooters(match_id: int, division: str = None):
    """獲取射手列表"""
    db = get_db()
    cursor = db.cursor()
    if division:
        cursor.execute("""
            SELECT id, competitor_number, name, division, class, factor, category,
                   region, total_score
            FROM shooters
            WHERE match_id = ? AND division = ?
            ORDER BY competitor_number
        """, (match_id, division))
    else:
        cursor.execute("""
            SELECT id, competitor_number, name, division, class, factor, category,
                   region, total_score
            FROM shooters
            WHERE match_id = ?
            ORDER BY competitor_number
        """, (match_id,))
    shooters = [dict(row) for row in cursor.fetchall()]
    db.close()
    return {"shooters": shooters}


@app.get("/api/matches/{match_id}/rankings")
def get_rankings(match_id: int, division: str, rank_type: str = "overall",
                 group_key: str = None):
    """獲取排名數據

    rank_type: overall, category, class, stage
    group_key: 可選，指定 subgroup（category 名稱、class 名稱、stage 名稱）
    """
    db = get_db()
    cursor = db.cursor()

    if rank_type == "overall":
        cursor.execute("""
            SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                   s.name, s.division, s.class, s.factor, s.category, s.region
            FROM rankings r
            JOIN shooters s ON r.match_id = s.match_id
                AND r.competitor_number = s.competitor_number
            WHERE r.match_id = ? AND r.division = ? AND r.rank_type = 'overall'
            ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
        """, (match_id, division))
        rows = [dict(r) for r in cursor.fetchall()]
        db.close()
        return {"rank_type": "overall", "division": division, "rankings": rows}

    elif rank_type == "category":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = ? AND r.division = ?
                  AND r.rank_type = 'category' AND r.group_key = ?
                ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return {"rank_type": "category", "division": division,
                    "group_key": group_key, "rankings": rows}
        else:
            # 返回所有 category 分組
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = ? AND r.division = ? AND r.rank_type = 'category'
                ORDER BY r.group_key
            """, (match_id, division))
            groups = [r["group_key"] for r in cursor.fetchall()]

            result = {}
            for g in groups:
                cursor.execute("""
                    SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                           s.name, s.division, s.class, s.factor, s.category, s.region
                    FROM rankings r
                    JOIN shooters s ON r.match_id = s.match_id
                        AND r.competitor_number = s.competitor_number
                    WHERE r.match_id = ? AND r.division = ?
                      AND r.rank_type = 'category' AND r.group_key = ?
                    ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
                """, (match_id, division, g))
                result[g] = [dict(r) for r in cursor.fetchall()]

            db.close()
            return {"rank_type": "category", "division": division,
                    "groups": list(result.keys()), "rankings": result}

    elif rank_type == "class":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = ? AND r.division = ?
                  AND r.rank_type = 'class' AND r.group_key = ?
                ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return {"rank_type": "class", "division": division,
                    "group_key": group_key, "rankings": rows}
        else:
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = ? AND r.division = ? AND r.rank_type = 'class'
                ORDER BY r.group_key
            """, (match_id, division))
            groups = [r["group_key"] for r in cursor.fetchall()]
            result = {}
            for g in groups:
                cursor.execute("""
                    SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                           s.name, s.division, s.class, s.factor, s.category, s.region
                    FROM rankings r
                    JOIN shooters s ON r.match_id = s.match_id
                        AND r.competitor_number = s.competitor_number
                    WHERE r.match_id = ? AND r.division = ?
                      AND r.rank_type = 'class' AND r.group_key = ?
                    ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
                """, (match_id, division, g))
                result[g] = [dict(r) for r in cursor.fetchall()]
            db.close()
            return {"rank_type": "class", "division": division,
                    "groups": list(result.keys()), "rankings": result}

    elif rank_type == "stage":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score AS stage_score,
                       r.score_percent AS hit_factor,
                       ss.pts AS points, ss.time AS stage_time,
                       ROUND(r.score_percent * 100.0 / NULLIF((
                           SELECT MAX(r2.score_percent) FROM rankings r2
                           WHERE r2.match_id = r.match_id AND r2.division = r.division
                             AND r2.rank_type = 'stage' AND r2.group_key = r.group_key
                       ), 0), 2) AS score_percent,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                LEFT JOIN stage_scores ss ON ss.shooter_id = s.id
                    AND ss.match_id = r.match_id
                    AND (ss.stage_name = r.group_key
                         OR ss.stage_name = REPLACE(r.group_key, ' 0', ' '))
                WHERE r.match_id = ? AND r.division = ?
                  AND r.rank_type = 'stage' AND r.group_key = ?
                ORDER BY CASE WHEN r.place = 0 THEN 1 ELSE 0 END, r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return {"rank_type": "stage", "division": division,
                    "group_key": group_key, "rankings": rows}
        else:
            # 列出所有 Stage
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = ? AND r.division = ? AND r.rank_type = 'stage'
                ORDER BY r.group_key
            """, (match_id, division))
            stages = [r["group_key"] for r in cursor.fetchall()]
            db.close()
            return {"rank_type": "stage", "division": division,
                    "stages": stages}

    db.close()
    raise HTTPException(400, f"唔支援嘅 rank_type: {rank_type}")


@app.get("/api/matches/{match_id}/stages")
def get_stages(match_id: int):
    """獲取比賽的所有 Stage 名稱"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT DISTINCT ss.stage_number, ss.stage_name
        FROM stage_scores ss
        WHERE ss.match_id = ?
        ORDER BY ss.stage_number
    """, (match_id,))
    stages = [dict(r) for r in cursor.fetchall()]
    db.close()
    return {"stages": stages}


@app.get("/api/matches/{match_id}/scrape")
def trigger_scrape(match_id: int):
    """觸發爬取與計算（異步，線程安全）"""
    global scrape_status

    if not _scrape_lock.acquire(blocking=False):
        return {"status": "busy", "message": "爬取系統繁忙，請稍後再試"}

    try:
        if scrape_status["running"]:
            return {"status": "running", "message": "爬取進行中，請稍後"}

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id, is_completed FROM matches WHERE id = ?", (match_id,))
        row = cursor.fetchone()
        if not row:
            db.close()
            raise HTTPException(404, "比賽唔存在")

        # 已完賽比賽唔需要重新爬取
        if row["is_completed"]:
            db.close()
            _scrape_lock.release()
            return {"status": "skipped", "message": f"比賽 #{match_id} 已完賽，唔需要重新爬取"}

        db.close()

        scrape_status["running"] = True
        scrape_status["progress"] = f"開始爬取比賽 #{match_id}..."

        def run():
            global scrape_status
            cfg = load_config()
            try:
                shooters, stages = scrape_match(match_id, cfg["base_url"], cfg)
                scrape_status["progress"] = f"爬取完成: {shooters} 射手, {stages} stages，開始計算排名..."
                calculate_all_rankings(match_id)
                scrape_status["progress"] = f"排名計算完成"
                scrape_status["last_run"] = datetime.now().isoformat()
            except Exception as e:
                scrape_status["progress"] = f"錯誤: {str(e)}"
                import traceback; traceback.print_exc()
            finally:
                scrape_status["running"] = False
                _scrape_lock.release()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return {"status": "started", "message": f"開始爬取比賽 #{match_id}"}
    except Exception:
        _scrape_lock.release()
        raise


def _auto_scrape_active_matches():
    """自動爬取所有進行中比賽（背景 cron 用）— 跳過已經完賽嘅"""
    cfg = load_config()
    base_url = cfg["base_url"]

    async def run():
        db = get_db()
        c = db.cursor()
        # 只揀未完賽（is_completed=0）嘅比賽
        c.execute("SELECT id, name FROM matches WHERE is_completed = 0 ORDER BY id DESC")
        active = [dict(r) for r in c.fetchall()]
        db.close()

        for m in active:
            mid = m["id"]
            if not _should_auto_scrape(mid, cooldown_sec=120):
                continue
            if not _scrape_lock.acquire(blocking=False):
                continue
            try:
                if scrape_status["running"]:
                    _scrape_lock.release()
                    continue
                scrape_status["running"] = True
                scrape_status["progress"] = f"[自動] 爬取比賽 #{mid}..."
                scrape_match(mid, base_url, cfg)
                calculate_all_rankings(mid)
                scrape_status["progress"] = f"[自動] 比賽 #{mid} 完成"
                scrape_status["last_run"] = datetime.now().isoformat()
            except Exception as e:
                scrape_status["progress"] = f"[自動] 錯誤: {e}"
            finally:
                scrape_status["running"] = False
                _scrape_lock.release()

    asyncio.run(run())


@app.post("/api/scrape/run")
def run_scrape():
    """手動執行爬取所有比賽（異步，線程安全）"""
    global scrape_status
    if not _scrape_lock.acquire(blocking=False):
        return {"status": "busy", "message": "爬取系統繁忙，請稍後再試"}
    if scrape_status["running"]:
        _scrape_lock.release()
        return {"status": "running", "message": "爬取進行中，請稍後"}

    def task():
        global scrape_status
        scrape_status["running"] = True
        scrape_status["progress"] = "同步比賽列表..."
        cfg = load_config()
        base_url = cfg["base_url"]
        try:
            # 同步比賽列表
            matches = parse_matches("")
            import requests as req
            resp = req.get(base_url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            resp.raise_for_status()
            html = resp.text
            matches = parse_matches(html)
            db = get_db()
            c = db.cursor()
            for m in matches:
                c.execute("INSERT OR IGNORE INTO matches (id, name, date, venue, level, url) VALUES (?,?,?,?,?,?)",
                          (m["id"], m["name"], m["date"], m["venue"], m["level"], m["url"]))
                c.execute("UPDATE matches SET name=?, date=?, venue=?, level=?, url=? WHERE id=?",
                          (m["name"], m["date"], m["venue"], m["level"], m["url"], m["id"]))
            db.commit()
            db.close()
            scrape_status["progress"] = f"同步 {len(matches)} 場比賽"

            db = get_db()
            cursor = db.cursor()
            # 只爬未完賽（is_completed=0）嘅比賽，已完賽唔會更新內容
            cursor.execute("""
                SELECT id, name FROM matches WHERE is_completed = 0 ORDER BY id DESC
            """)
            active = [dict(r) for r in cursor.fetchall()]
            db.close()

            if not active:
                scrape_status["progress"] = "冇進行中比賽需要爬取"

            for m_dict in active:
                mid = m_dict["id"]
                scrape_status["progress"] = f"處理比賽 #{mid} ({m_dict['name']})..."
                scrape_status["last_run"] = datetime.now().isoformat()
                scrape_match(mid, base_url, cfg)
                calculate_all_rankings(mid)

            scrape_status["progress"] = f"全部完成"
        except Exception as e:
            scrape_status["progress"] = f"錯誤: {e}"
            import traceback; traceback.print_exc()
        finally:
            scrape_status["running"] = False
            _scrape_lock.release()

    thread = threading.Thread(target=task, daemon=True)
    thread.start()
    return {"status": "started", "message": "爬取已開始（異步）"}


@app.get("/api/scrape/status")
def get_scrape_status():
    """獲取爬取狀態"""
    s = dict(scrape_status)
    s["next_auto_scrape_sec"] = 300
    if _last_auto_scrape:
        s["next_auto_scrape_sec"] = max(5, 300 - int(time.time() - max(_last_auto_scrape.values())))
    s["locked"] = not _scrape_lock.acquire(blocking=False)
    if not s["locked"]:
        _scrape_lock.release()
    s["cooldown_sec"] = 120
    return s


@app.get("/api/matches/{match_id}/recalculate")
def recalculate(match_id: int):
    """重新計算排名（唔爬取，只用已有數據）"""
    try:
        calculate_all_rankings(match_id)
        return {"status": "success", "message": f"比賽 #{match_id} 排名已重新計算"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/scrape/log")
def get_scrape_log(limit: int = 20):
    """獲取爬取日誌"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT sl.*, m.name as match_name
        FROM scrape_log sl
        JOIN matches m ON sl.match_id = m.id
        ORDER BY sl.id DESC LIMIT ?
    """, (limit,))
    logs = [dict(r) for r in cursor.fetchall()]
    db.close()
    return {"logs": logs}


# ===================== Frontend =====================

@app.get("/", response_class=HTMLResponse)
def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/match/{match_id}", response_class=HTMLResponse)
def match_page(match_id: int):
    with open("templates/match.html", "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{MATCH_ID}}", str(match_id))


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", API_PORT))
    uvicorn.run(app, host=API_HOST, port=port)
