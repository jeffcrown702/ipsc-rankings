"""IPSC 實時排名系統 - Flask 後端 (Vercel 相容)

API Endpoints:
  GET  /api/matches              — 比賽列表
  GET  /api/matches/{id}         — 比賽詳情
  GET  /api/matches/{id}/scrape  — 觸發爬取
  GET  /api/matches/{id}/rankings — 四大排名數據
  GET  /api/matches/{id}/shooters — 射手列表
  GET  /api/matches/{id}/stages   — 舞台列表
  GET  /api/scrape/status        — 爬取狀態
  POST /api/scrape/run           — 手動執行爬取
  GET  /api/cron/scrape          — Vercel cron job
  GET  /                         — 主頁
  GET  /match/{id}               — 比賽頁
  GET  /api/import               — import info
  POST /api/import               — import data
"""
import json
from datetime import datetime
from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_cors import CORS
import os
import sys
import time
import traceback

# Vercel 環境：唔 import threading/asyncio/aiohttp
_IS_VERCEL = os.environ.get("VERCEL") == "1"
if not _IS_VERCEL:
    import threading
    import asyncio
    import aiohttp

sys.path.insert(0, os.path.dirname(__file__))
from core.database import get_db, get_cursor, init_db as _init_db
from core.scraper import load_config, scrape_match, sync_matches as scraper_sync_matches, parse_matches, fetch_html, scrape_results_match
from core.scoring_engine import calculate_all_rankings, calculate_division_rankings
from core.config import API_HOST, API_PORT, DIVISIONS
import core.config as cfg

# ===================== Flask App Init =====================
app = Flask(__name__, template_folder="templates", static_folder="static")

# CORS - open to all origins
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ===================== DB Init =====================
_db_initialized = False

def ensure_db():
    """Lazy init DB on first request (Vercel-safe)"""
    global _db_initialized
    if not _db_initialized:
        try:
            _init_db()
            _db_initialized = True
        except Exception as e:
            print(f"[DB] init_db error: {e}")


# ===================== Scrape Status + Lock =====================
scrape_status = {"running": False, "last_run": None, "progress": ""}

if _IS_VERCEL:
    class _DummyLock:
        def acquire(self, blocking=True): return True
        def release(self): pass
        def locked(self): return False
    _scrape_lock = _DummyLock()
else:
    _scrape_lock = threading.Lock()

_last_auto_scrape = {}
_BATCH_SIZE = 5  # 每次爬取 shooter 數量


def _should_auto_scrape(match_id, cooldown_sec=120):
    """Cooldown 檢查：同一場比賽至少隔 cooldown_sec 秒先可以再爬"""
    now = time.time()
    last = _last_auto_scrape.get(match_id, 0)
    if now - last >= cooldown_sec:
        _last_auto_scrape[match_id] = now
        return True
    return False


# ===================== Error Handlers =====================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    import traceback
    tb = traceback.format_exc()
    print(f"[500] {e}\n{tb}")
    return jsonify({"error": str(e), "traceback": tb}), 500

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e)}), 400

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": str(e)}), 500


# ===================== API Routes =====================

@app.route("/api/debug/version")
def debug_version():
    """Check deployed version"""
    return jsonify({"version": "3b5f573", "db": "PostgreSQL" if os.environ.get('DATABASE_URL', '') else "SQLite"})


@app.route('/api/cron/keepwarm')
def cron_keepwarm():
    """Keep Neon warm — Vercel cron every 4 min"""
    try:
        db = get_db()
        c = get_cursor(db)
        c.execute("SELECT 1")
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/matches")
def get_matches():
    """獲取比賽列表"""
    ensure_db()
    db = get_db()
    cursor = get_cursor(db)
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
        c = get_cursor(db2)
        c.execute("SELECT COUNT(*) as cnt FROM shooters WHERE match_id = %s", (m["id"],))
        row = c.fetchone()
        m["shooter_count"] = row["cnt"] if row else 0
        db2.close()

    return jsonify({"matches": matches})


@app.route("/api/matches/<int:match_id>")
def get_match(match_id):
    """獲取比賽詳情（含 divisions）"""
    ensure_db()
    db = get_db()
    cursor = get_cursor(db)
    cursor.execute("SELECT id, name, date, venue, level, is_completed FROM matches WHERE id = %s", (match_id,))
    row = cursor.fetchone()
    if not row:
        db.close()
        return jsonify({"error": "比賽不存在"}), 404

    match_data = dict(row)

    # 取得有數據嘅 Division（跟 config 順序）
    cursor.execute("""
        SELECT DISTINCT division FROM shooters
        WHERE match_id = %s AND division IS NOT NULL AND division != ''
    """, (match_id,))
    db_divs = {r["division"] for r in cursor.fetchall()}
    # 跟 config DIVISIONS 順序排列
    from core.config import DIVISIONS
    match_data["divisions"] = [d for d in DIVISIONS if d in db_divs]

    # 射手總數
    cursor.execute("SELECT COUNT(*) as cnt FROM shooters WHERE match_id = %s", (match_id,))
    match_data["shooter_count"] = cursor.fetchone()["cnt"]

    # 最後計算時間
    cursor.execute("""
        SELECT MAX(calculated_at) as last_calc FROM rankings WHERE match_id = %s
    """, (match_id,))
    row = cursor.fetchone()
    match_data["last_calculated"] = row["last_calc"] if row else None

    db.close()
    return jsonify({"match": match_data})


@app.route("/api/matches/<int:match_id>/shooters")
def get_shooters(match_id):
    """獲取射手列表"""
    ensure_db()
    division = request.args.get("division")
    db = get_db()
    cursor = get_cursor(db)
    if division:
        cursor.execute("""
            SELECT id, competitor_number, name, division, class, factor, category,
                   region, total_score
            FROM shooters
            WHERE match_id = %s AND division = %s
            ORDER BY competitor_number
        """, (match_id, division))
    else:
        cursor.execute("""
            SELECT id, competitor_number, name, division, class, factor, category,
                   region, total_score
            FROM shooters
            WHERE match_id = %s
            ORDER BY competitor_number
        """, (match_id,))
    shooters = [dict(row) for row in cursor.fetchall()]
    db.close()
    return jsonify({"shooters": shooters})


@app.route("/api/matches/<int:match_id>/rankings")
def get_rankings(match_id):
    """獲取排名數據

    Query params:
      rank_type: overall, category, class, stage
      division:  Open, Standard, etc.
      group_key: 可選，指定 subgroup
    """
    ensure_db()
    division = request.args.get("division", "")
    rank_type = request.args.get("rank_type", "overall")
    group_key = request.args.get("group_key")

    if not division:
        return jsonify({"error": "division parameter is required"}), 400

    db = get_db()
    cursor = get_cursor(db)

    if rank_type == "overall":
        cursor.execute("""
            SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                   s.name, s.division, s.class, s.factor, s.category, s.region
            FROM rankings r
            JOIN shooters s ON r.match_id = s.match_id
                AND r.competitor_number = s.competitor_number
            WHERE r.match_id = %s AND r.division = %s AND r.rank_type = 'overall'
            ORDER BY r.place
        """, (match_id, division))
        rows = [dict(r) for r in cursor.fetchall()]
        db.close()
        return jsonify({"rank_type": "overall", "division": division, "rankings": rows})

    elif rank_type == "category":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = %s AND r.division = %s
                  AND r.rank_type = 'category' AND r.group_key = %s
                ORDER BY r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return jsonify({"rank_type": "category", "division": division,
                            "group_key": group_key, "rankings": rows})
        else:
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = %s AND r.division = %s AND r.rank_type = 'category'
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
                    WHERE r.match_id = %s AND r.division = %s
                      AND r.rank_type = 'category' AND r.group_key = %s
                    ORDER BY r.place
                """, (match_id, division, g))
                result[g] = [dict(r) for r in cursor.fetchall()]

            db.close()
            return jsonify({"rank_type": "category", "division": division,
                            "groups": list(result.keys()), "rankings": result})

    elif rank_type == "class":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score, r.score_percent,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = %s AND r.division = %s
                  AND r.rank_type = 'class' AND r.group_key = %s
                ORDER BY r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return jsonify({"rank_type": "class", "division": division,
                            "group_key": group_key, "rankings": rows})
        else:
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = %s AND r.division = %s AND r.rank_type = 'class'
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
                    WHERE r.match_id = %s AND r.division = %s
                      AND r.rank_type = 'class' AND r.group_key = %s
                    ORDER BY r.place
                """, (match_id, division, g))
                result[g] = [dict(r) for r in cursor.fetchall()]
            db.close()
            return jsonify({"rank_type": "class", "division": division,
                            "groups": list(result.keys()), "rankings": result})

    elif rank_type == "stage":
        if group_key:
            cursor.execute("""
                SELECT r.place, r.competitor_number, r.total_score AS stage_score,
                       r.score_percent AS hit_factor,
                       s.name, s.division, s.class, s.factor, s.category, s.region
                FROM rankings r
                JOIN shooters s ON r.match_id = s.match_id
                    AND r.competitor_number = s.competitor_number
                WHERE r.match_id = %s AND r.division = %s
                  AND r.rank_type = 'stage' AND r.group_key = %s
                ORDER BY r.place
            """, (match_id, division, group_key))
            rows = [dict(r) for r in cursor.fetchall()]
            db.close()
            return jsonify({"rank_type": "stage", "division": division,
                            "group_key": group_key, "rankings": rows})
        else:
            cursor.execute("""
                SELECT DISTINCT r.group_key
                FROM rankings r
                WHERE r.match_id = %s AND r.division = %s AND r.rank_type = 'stage'
                ORDER BY r.group_key
            """, (match_id, division))
            stages = [r["group_key"] for r in cursor.fetchall()]
            db.close()
            return jsonify({"rank_type": "stage", "division": division,
                            "stages": stages})

    db.close()
    return jsonify({"error": f"唔支援嘅 rank_type: {rank_type}"}), 400


@app.route("/api/matches/<int:match_id>/stages")
def get_stages(match_id):
    """獲取比賽的所有 Stage 名稱"""
    ensure_db()
    db = get_db()
    cursor = get_cursor(db)
    cursor.execute("""
        SELECT DISTINCT ss.stage_number, ss.stage_name
        FROM stage_scores ss
        WHERE ss.match_id = %s
        ORDER BY ss.stage_number
    """, (match_id,))
    stages = [dict(r) for r in cursor.fetchall()]
    db.close()
    return jsonify({"stages": stages})


@app.route("/api/matches/<int:match_id>/scrape")
def trigger_scrape(match_id):
    """觸發爬取與計算（線程安全）"""
    global scrape_status
    ensure_db()

    if not _scrape_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "爬取系統繁忙，請稍後再試"})

    try:
        if scrape_status["running"]:
            return jsonify({"status": "running", "message": "爬取進行中，請稍後"})

        db = get_db()
        cursor = get_cursor(db)
        cursor.execute("SELECT id, is_completed FROM matches WHERE id = %s", (match_id,))
        row = cursor.fetchone()
        if not row:
            db.close()
            return jsonify({"error": "比賽唔存在"}), 404

        if row["is_completed"]:
            db.close()
            _scrape_lock.release()
            return jsonify({"status": "skipped", "message": f"比賽 #{match_id} 已完賽，唔需要重新爬取"})

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
                traceback.print_exc()
            finally:
                scrape_status["running"] = False
                _scrape_lock.release()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return jsonify({"status": "started", "message": f"開始爬取比賽 #{match_id}"})

    except Exception:
        _scrape_lock.release()
        raise


def _auto_scrape_active_matches():
    """自動爬取所有進行中比賽（背景 cron 用）"""
    if _IS_VERCEL:
        return
    cfg = load_config()
    base_url = cfg["base_url"]

    db = get_db()
    c = get_cursor(db)
    c.execute("SELECT id, name FROM matches WHERE is_completed = 0 ORDER BY id DESC")
    active = [dict(r) for r in c.fetchall()]
    db.close()

    if not active:
        print("[CRON] 冇進行中比賽")
        return

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
            scrape_status["progress"] = f"[自動] 爬取比賽 #{mid} ({m['name'][:30]})..."
            print(f"[CRON] 開始自動爬取 #{mid}: {m['name'][:40]}")
            scrape_match(mid, base_url, cfg)
            calculate_all_rankings(mid)
            scrape_status["progress"] = f"[自動] 比賽 #{mid} 完成"
            scrape_status["last_run"] = datetime.now().isoformat()
            print(f"[CRON] 自動爬取 #{mid} 完成")
        except Exception as e:
            scrape_status["progress"] = f"[自動] 錯誤: {e}"
            print(f"[CRON] 自動爬取 #{mid} 錯誤: {e}")
        finally:
            scrape_status["running"] = False
            _scrape_lock.release()


@app.route("/api/scrape/run", methods=["POST"])
def run_scrape():
    """Vercel: 同步爬 _BATCH_SIZE 個 shooter（保證 10 秒內完成）"""
    global scrape_status
    ensure_db()

    scrape_status["running"] = True
    scrape_status["progress"] = "直接爬比賽 #37..."
    base_url = cfg.BASE_URL

    try:
        _scrape_batch(37, base_url, {}, _BATCH_SIZE)
        scrape_status["progress"] = "爬取完成，計算排名..."
        calculate_all_rankings(37)
        scrape_status["progress"] = "全部完成"
    except Exception as e:
        scrape_status["progress"] = f"錯誤: {e}"
        traceback.print_exc()
    finally:
        scrape_status["running"] = False

    return jsonify({"status": "completed", "message": scrape_status["progress"]})


def _scrape_batch(match_id, base_url, cfg_dict, batch_size=5):
    """只爬少量 shooter，適合 Vercel 10 秒限制"""
    from core.scraper import parse_verify_page
    import requests as _req

    db = get_db()
    cursor = get_cursor(db)

    # 找未爬的 shooter
    cursor.execute("""
        SELECT s.competitor_number FROM shooters s
        WHERE s.match_id = %s
        AND (
            SELECT COUNT(*) FROM stage_scores ss WHERE ss.shooter_id = s.id
        ) = 0
        ORDER BY s.competitor_number
        LIMIT ?
    """, (match_id, batch_size))
    to_scrape = [r[0] for r in cursor.fetchall()]
    cursor.close()

    if not to_scrape:
        cursor = get_cursor(db)
        cursor.execute("SELECT MAX(competitor_number) FROM shooters WHERE match_id = %s", (match_id,))
        max_num = cursor.fetchone()[0] or 0
        cursor.close()
        to_scrape = list(range(max_num + 1, min(max_num + 1 + batch_size, 220)))

    scraped = 0
    for comp_num in to_scrape:
        verify_url = f"{base_url}/portal/verify_competitor.php?comp_num={comp_num}"
        try:
            resp = _req.get(verify_url, timeout=10)
            html = resp.text
        except:
            html = None
        if html:
            result = parse_verify_page(html)
            if result and result["name"] != "Unknown":
                _save_shooter_data(match_id, result)
                scraped += 1

    db.close()
    return scraped


def _save_shooter_data(match_id, data):
    """儲存單一射手數據到 DB"""
    from core.database import get_db, get_cursor

    db = get_db()
    cursor = get_cursor(db)

    # 檢查是否存在
    cursor.execute("SELECT id FROM shooters WHERE match_id = %s AND competitor_number = %s",
                   (match_id, data["competitor_number"]))
    existing = cursor.fetchone()

    if existing:
        shooter_id = existing[0]
    else:
        cursor.execute("""
            INSERT INTO shooters (match_id, competitor_number, name, division, category, class, factor, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_id, data["competitor_number"], data["name"], data["division"],
              data.get("category", ""), data.get("class", ""), data.get("factor", ""), data.get("region", "")))
        db.commit()
        shooter_id = cursor.lastrowid

    # 清空舊 stage scores
    cursor.execute("DELETE FROM stage_scores WHERE shooter_id = %s", (shooter_id,))

    # 插入新 stage scores
    for stg in data.get("stages", []):
        cursor.execute("""
            INSERT INTO stage_scores (shooter_id, match_id, stage_number, pts, a, c, d, mi, ns, pe, time, hit_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (shooter_id, match_id, stg["stage_num"], stg.get("pts", 0),
              stg.get("a", 0), stg.get("c", 0), stg.get("d", 0),
              stg.get("mi", 0), stg.get("ns", 0), stg.get("pe", 0),
              stg.get("time", 0), stg.get("hit_factor", 0)))

    db.commit()
    cursor.close()


@app.route("/api/scrape/status")
def get_scrape_status():
    """獲取爬取狀態（Vercel 友好版）"""
    s = dict(scrape_status)
    s["next_auto_scrape_sec"] = 300
    if _last_auto_scrape:
        s["next_auto_scrape_sec"] = max(5, 300 - int(time.time() - max(_last_auto_scrape.values())))
    s["locked"] = not _scrape_lock.acquire(blocking=False)
    if not s["locked"]:
        _scrape_lock.release()
    s["cooldown_sec"] = 120
    s["running"] = s.get("running", False) or _scrape_lock.locked()
    return jsonify(s)


@app.route("/api/matches/<int:match_id>/recalculate")
def recalculate(match_id):
    """重新計算排名（唔爬取，只用已有數據）"""
    ensure_db()
    try:
        calculate_all_rankings(match_id)
        return jsonify({"status": "success", "message": f"比賽 #{match_id} 排名已重新計算"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape/log")
def get_scrape_log():
    """獲取爬取日誌"""
    ensure_db()
    limit = request.args.get("limit", 20, type=int)
    db = get_db()
    cursor = get_cursor(db)
    cursor.execute("""
        SELECT sl.*, m.name as match_name
        FROM scrape_log sl
        JOIN matches m ON sl.match_id = m.id
        ORDER BY sl.id DESC LIMIT ?
    """, (limit,))
    logs = [dict(r) for r in cursor.fetchall()]
    db.close()
    return jsonify({"logs": logs})


# ===================== Frontend Pages =====================

@app.route("/")
def index():
    """主頁 — 伺服器端 render 比賽列表"""
    ensure_db()
    html = open("templates/index.html", encoding="utf-8").read()
    try:
        db = get_db()
        cursor = get_cursor(db)
        cursor.execute("""
            SELECT m.id, m.name, m.date, m.venue, m.level, m.is_completed,
                   COUNT(s.id) as shooter_count
            FROM matches m
            LEFT JOIN shooters s ON s.match_id = m.id
            GROUP BY m.id
            ORDER BY substr(m.date,7,4)||'-'||substr(m.date,4,2)||'-'||substr(m.date,1,2) DESC, m.id DESC
        """)
        matches = [dict(row) for row in cursor.fetchall()]
        db.close()

        cards = []
        for m in matches:
            status = "✅ 已完成" if m["is_completed"] else ("🟡 進行中" if m.get("shooter_count", 0) > 0 else "🔴 未開始")
            level_str = ("· " + m["level"]) if m.get("level") else ""
            cards.append(
                '<a href="/match/%d" class="match-card">'
                '<div class="top-row">'
                '<span class="match-name">%s</span>'
                '<span class="match-date">%s</span>'
                '</div>'
                '<div class="match-meta">'
                '%s %s · <strong>%d</strong> 位射手'
                '</div>'
                '<div class="match-status">%s</div>'
                '</a>' % (m["id"], m["name"], m["date"] or "", m["venue"] or "", level_str, m.get("shooter_count", 0), status)
            )
        cards_html = "\n".join(cards)
        html = html.replace('<div id="matchList"></div>',
            '<div id="matchList">%s</div>' % cards_html)
        html = html.replace("載入中...", "")
        return html
    except Exception as e:
        return html.replace("載入中...", "<div class='error-banner'>載入失敗: %s</div>" % str(e))
    return render_template("index.html")


@app.route("/match/<int:match_id>")
def match_page(match_id):
    """比賽頁"""
    ensure_db()
    return render_template("match.html", MATCH_ID=match_id)


# ===================== Vercel Cron =====================

@app.route("/api/cron/scrape")
def cron_scrape():
    """Vercel cron job: 自動爬取 active matches"""
    if not _IS_VERCEL:
        return jsonify({"error": "only for Vercel"})

    ensure_db()
    from core.scraper import fetch_html, sync_matches, parse_matches, scrape_match
    from core.scoring_engine import calculate_all_rankings

    html = fetch_html(cfg.BASE_URL)
    if html:
        sync_matches(html)
        parse_matches(html)

    db = get_db()
    cursor = get_cursor(db)
    cursor.execute("SELECT id FROM matches WHERE is_completed = 0")
    mids = [r[0] for r in cursor.fetchall()]
    cursor.close()

    for mid in mids:
        try:
            _scrape_batch(mid, cfg.BASE_URL, {}, _BATCH_SIZE)
            calculate_all_rankings(mid)
        except Exception:
            pass

    db.close()
    return jsonify({"ok": True, "matches": len(mids)})


# ===================== Import =====================

@app.route("/api/import", methods=["GET"])
def import_data_endpoint():
    """Import info"""
    return jsonify({"error": "Use POST with JSON body"})


@app.route("/api/import", methods=["POST"])
def import_data_post():
    """Import data from JSON body (for Vercel migration)"""
    ensure_db()
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error": "invalid JSON"}), 400

    db = get_db()
    cursor = get_cursor(db)
    counts = {}

    for table in ['matches', 'shooters', 'stage_scores', 'rankings']:
        rows = data.get(table, [])
        if not rows:
            continue
        cols = list(rows[0].keys())
        placeholders = ','.join(['%s'] * len(cols))
        col_names = ','.join(cols)
        for row in rows:
            vals = [row.get(c) for c in cols]
            try:
                cursor.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", vals)
            except:
                pass
        db.commit()
        counts[table] = len(rows)

    db.close()
    return jsonify(counts)


# ===================== Background Cron (non-Vercel only) =====================

if not _IS_VERCEL:
    def _start_background_cron():
        """定時任務：每 5 分鐘自動爬取進行中比賽"""
        def cron_loop():
            while True:
                time.sleep(300)
                try:
                    _auto_scrape_active_matches()
                except Exception as e:
                    print(f"[CRON] 自動爬取出錯: {e}")

        t = threading.Thread(target=cron_loop, daemon=True)
        t.start()
        print("[CRON] 自動爬取已啟動（每 5 分鐘）")

    # Start background cron on import
    _start_background_cron()


# ===================== Main =====================

if __name__ == "__main__":
    ensure_db()
    if not _IS_VERCEL:
        port = int(os.environ.get("PORT", API_PORT))
        app.run(host=API_HOST, port=port, debug=False)
