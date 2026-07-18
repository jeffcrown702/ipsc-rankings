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

import os, sys, asyncio
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

# 爬取狀態
scrape_status = {"running": False, "last_run": None, "progress": ""}


@app.on_event("startup")
def startup():
    init_db()


# ===================== API Routes =====================

@app.get("/api/matches")
def get_matches():
    """獲取比賽列表"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT id, name, date, venue, level, is_completed, last_scraped
        FROM matches
        ORDER BY id DESC
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

    # 取得有數據嘅 Division
    cursor.execute("""
        SELECT DISTINCT division FROM shooters
        WHERE match_id = ? AND division IS NOT NULL AND division != ''
        ORDER BY division
    """, (match_id,))
    match_data["divisions"] = [r["division"] for r in cursor.fetchall()]

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
            ORDER BY r.place
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
                ORDER BY r.place
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
                    ORDER BY r.place
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
                ORDER BY r.place
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
                    ORDER BY r.place
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
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = ? AND r.division = ?
                  AND r.rank_type = 'stage' AND r.group_key = ?
                ORDER BY r.place
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
    """觸發爬取與計算（異步）"""
    global scrape_status

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM matches WHERE id = ?", (match_id,))
    if not cursor.fetchone():
        db.close()
        raise HTTPException(404, "比賽唔存在")
    db.close()

    scrape_status["running"] = True
    scrape_status["progress"] = f"開始爬取比賽 #{match_id}..."

    async def run():
        global scrape_status
        cfg = load_config()
        try:
            shooters, stages = await scrape_match(match_id, cfg["base_url"], cfg)
            scrape_status["progress"] = f"爬取完成: {shooters} 射手, {stages} stages，開始計算排名..."
            calculate_all_rankings(match_id)
            scrape_status["progress"] = f"排名計算完成"
            scrape_status["last_run"] = datetime.now().isoformat()
        except Exception as e:
            scrape_status["progress"] = f"錯誤: {str(e)}"
        finally:
            scrape_status["running"] = False

    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    return {"status": "started", "message": f"開始爬取比賽 #{match_id}"}


@app.post("/api/scrape/run")
def run_scrape():
    """手動執行爬取所有比賽（異步）"""
    global scrape_status
    if scrape_status["running"]:
        return {"status": "running", "message": "爬取進行中，請稍後"}

    async def task():
        global scrape_status
        scrape_status["running"] = True
        scrape_status["progress"] = "同步比賽列表..."
        cfg = load_config()
        base_url = cfg["base_url"]
        try:
            # 同步比賽列表
            html = await fetch_html(None, base_url)
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
            cursor.execute("""
                SELECT id, name FROM matches WHERE is_completed = 0 ORDER BY id DESC
            """)
            active = [dict(r) for r in cursor.fetchall()]
            db.close()

            for m in active:
                mid = m["id"]
                scrape_status["progress"] = f"處理比賽 #{mid} ({m['name']})..."
                scrape_status["last_run"] = datetime.now().isoformat()
                await scrape_match(mid, base_url, cfg)
                calculate_all_rankings(mid)

            scrape_status["progress"] = f"全部完成"
        except Exception as e:
            scrape_status["progress"] = f"錯誤: {e}"
        finally:
            scrape_status["running"] = False

    thread = threading.Thread(target=lambda: asyncio.run(task()), daemon=True)
    thread.start()
    return {"status": "started", "message": "爬取已開始（異步）"}


@app.get("/api/scrape/status")
def get_scrape_status():
    """獲取爬取狀態"""
    return scrape_status


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
