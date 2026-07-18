"""
IPSC 實時排名系統 — 核心異步爬蟲腳本
========================================

目標網站: https://hkg.as.ipscess.org/portal
爬取模式:
  - 未完賽 → POST/GET Verify?shooter=N 逐個抓取單一射手成績
  - 完賽   → 直接從 Results 分頁抓取排名數據

使用方式:
  python core/scraper.py                     # 爬取所有未完賽比賽
  python core/scraper.py --match 37          # 指定比賽 ID
  python core/scraper.py --mock              # 使用 Mock 數據測試（不需連線）

架構說明:
  1. config_loader()     → 載入 config.json
  2. fetch_html()        → 異步 HTTP 請求（aiohttp）
  3. parse_matches()     → 解析比賽列表
  4. parse_verify_page() → 解析 Verify 成績頁（★ 核心區塊，見下方 Mock）
  5. save_shooter()      → 存入 SQLite
  6. main()              → 主流程控制
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ─── 依賴提示 ──────────────────────────────────────
# 以下套件請用 pip install 安裝：
#   pip install aiohttp beautifulsoup4
#
try:
    import aiohttp
except ImportError:
    aiohttp = None
    print("[WARN] aiohttp 未安裝，無法使用真實 HTTP 請求。請執行: pip install aiohttp")
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("[WARN] beautifulsoup4 未安裝，無法解析 HTML。請執行: pip install beautifulsoup4")

# ─── 專案根目錄 ────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════
#  1. 設定載入
# ═══════════════════════════════════════════════════

def load_config(path=None):
    """載入 config.json，回傳 dict"""
    if path is None:
        path = ROOT / "config.json"
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


# ═══════════════════════════════════════════════════
#  2. 異步 HTTP 請求（aiohttp）
# ═══════════════════════════════════════════════════

async def fetch_html(session, url, timeout=30):
    """
    發送 GET 請求並回傳 HTML 字串。
    若 aiohttp 不可用，回退到 requests（同步 Fallback）。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
    }

    if aiohttp:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            resp.raise_for_status()
            return await resp.text()
    else:
        # Fallback: requests（同步包裝）
        import requests
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text


# ═══════════════════════════════════════════════════
#  3. 比賽列表解析
# ═══════════════════════════════════════════════════

def parse_matches(html):
    """
    從 portal 主頁 HTML 解析出比賽列表。

    ★ HTML 結構（2026-07 觀察）:
        <a href="?match=37">
          <h5>HKASA CHALLENGE 2026 - Round 1</h5>
          18/07/2026
          HKASA
          Nível I
        </a>

    ★ 回傳: list[dict] — [{id, name, date, venue, level, url}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for a_tag in soup.select("main a[href*='match=']"):
        href = a_tag.get("href", "")
        m = re.search(r"match=(\d+)", href)
        if not m:
            continue
        match_id = int(m.group(1))

        h5 = a_tag.find("h5")
        name = h5.get_text(strip=True) if h5 else ""

        # 取出日期/場地/級別
        full_text = a_tag.get_text(" ", strip=True)
        if name:
            full_text = full_text.replace(name, "", 1).strip()
        parts = [p.strip() for p in full_text.split() if p.strip()]
        date = ""
        venue = ""
        level = ""
        if parts:
            date_match = re.search(r"\d{2}/\d{2}/\d{4}", parts[0])
            if date_match:
                date = date_match.group()
                parts = parts[1:]
        if parts:
            venue = parts[0]
        if len(parts) > 1:
            level = " ".join(parts[1:])

        matches.append({
            "id": match_id,
            "name": name,
            "date": date,
            "venue": venue,
            "level": level,
            "url": f"{load_config()['base_url']}?match={match_id}",
        })

    return matches


def check_match_completion(match_id, base_url):
    """
    檢查比賽是否已完成。

    檢測邏輯：fetch match page，判斷頁面標題：
      - <h5 class="card-title">Results</h5>  → 已完成 (is_completed=1)
      - <h5 class="card-title">Verify</h5>    → 進行中 (is_completed=0)

    回傳: bool — True 表示已完成
    """
    import requests as _req
    try:
        url = f"{base_url}?match={match_id}"
        resp = _req.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        html = resp.text

        if "Results</h5>" in html or 'card-title">Results' in html:
            return True
        if "Verify</h5>" in html or 'card-title">Verify' in html:
            return False

        # Fallback: check for results links vs verify form
        if "/portal/results/" in html:
            return True
        if "/portal/verify/" in html:
            return False

        # Unknown — default to incomplete (safer for scraping)
        return False
    except Exception as e:
        print(f"  [WARN] check_match_completion(#{match_id}): {e}")
        return None  # Unknown — don't update


# ═══════════════════════════════════════════════════
#  4. Verify 成績頁解析  ★ 核心區塊 ★
# ═══════════════════════════════════════════════════
#  此函數是整個系統的靈魂 — 解析單一射手的成績單頁面。
#  下方保留了一個 parse_verify_page_mock() 用於離線測試，
#  以及真實的 parse_verify_page() 等待你填入 selectors。
# ═══════════════════════════════════════════════════

# ─── 4a. Mock 數據（離線測試用） ──────────────────

MOCK_VERIFY_HTML = """<!DOCTYPE html>
<html>
<body>
<form>
  <input type="text" placeholder="Type your ID" value="1" />
  <button>Verify</button>
</form>
<div>1 Cheng, Ka Ling</div>
<div>DIV: Standard CLASSE: C FATOR: Minor CAT: Lady</div>
<table>
  <thead>
    <tr>
      <th>STG</th><th>FACTOR</th><th>PTS</th><th>A</th><th>C</th>
      <th>D</th><th>MI</th><th>NS</th><th>PE</th><th></th><th>TIME</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>STAGE 1</td><td>2.4573</td><td>46</td><td>10</td><td>2</td>
      <td>0</td><td>0</td><td>1</td><td>0</td><td></td><td>18.72</td>
    </tr>
    <tr>
      <td>STAGE 2</td><td>3.5714</td><td>112</td><td>20</td><td>4</td>
      <td>0</td><td>0</td><td>0</td><td>0</td><td></td><td>31.36</td>
    </tr>
    <tr>
      <td>STAGE 3</td><td>5.5180</td><td>49</td><td>8</td><td>3</td>
      <td>0</td><td>0</td><td>0</td><td>0</td><td></td><td>8.88</td>
    </tr>
    <tr>
      <td>STAGE 4</td><td>1.1679</td><td>45</td><td>14</td><td>8</td>
      <td>1</td><td>1</td><td>0</td><td>4</td><td></td><td>38.53</td>
    </tr>
    <tr>
      <td>STAGE 5</td><td>5.0341</td><td>118</td><td>23</td><td>1</td>
      <td>0</td><td>0</td><td>0</td><td>0</td><td></td><td>23.44</td>
    </tr>
    <tr>
      <td>STAGE 6</td><td>4.7740</td><td>150</td><td>27</td><td>5</td>
      <td>0</td><td>0</td><td>0</td><td>0</td><td></td><td>31.42</td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""


def sanitize_name(name):
    """
    清洗選手姓名，移除 Excel 錯誤字串（#REF!, #VALUE!, #N/A 等）。

    若清洗後為空白 → 回傳 "[Unknown]"
    """
    if not name:
        return "[Unknown]"

    # Excel 錯誤 pattern 列表
    excel_errors = [
        r'#REF!', r'#VALUE!', r'#N/A', r'#NAME\?',
        r'#DIV/0!', r'#NULL!', r'#NUM!',
        r'#REF\b', r'#VALUE\b', r'#N/A\b',
    ]

    cleaned = name
    for pattern in excel_errors:
        cleaned = re.sub(pattern, '', cleaned)

    # 清理多餘空格同標點
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r',\s*$', '', cleaned)  # trailing comma
    cleaned = re.sub(r'^\s*,', '', cleaned)  # leading comma
    cleaned = cleaned.strip()

    if not cleaned or len(cleaned) < 2:
        return "[Unknown]"

    return cleaned


def parse_verify_page_mock():
    """
    使用 MOCK_VERIFY_HTML 測試解析邏輯。
    不回傳 HTML，直接傳入 mock 字串進行解析。

    當你完成真實 selectors 對接後，此函數可刪除或保留作為單元測試用。
    """
    return parse_verify_page(MOCK_VERIFY_HTML)


def parse_verify_page(html):
    """
    解析 Verify 成績單頁面，提取選手資料 + 所有 Stage 成績。

    ★★★ 請你根據實際網頁 HTML 填入以下 Selector ★★★
    ★★★ 見下方 # TODO 標記處 ★★★

    HTML 結構（參考 2026-07 觀察）:
      <div>1 Cheng, Ka Ling</div>
      <div>DIV: Standard CLASSE: C FATOR: Minor CAT: Lady</div>
      <table>
        <tr><td>STAGE 1</td><td>2.4573</td><td>46</td>...</tr>
      </table>

    回傳: dict 或 None（如該編號無效）
    """
    soup = BeautifulSoup(html, "html.parser")

    # ─── TODO: 請填入實際 Selector ─────────────────
    # 當前使用的是 2026-07 觀察到的標籤結構。
    # 若目標網站的 class/id 有變，請修改這裡。

    # 1. 檢查是否為有效頁面（有選手姓名）
    #    Selector 目標: 包含 "編號 姓名, 姓氏" 嘅 div
    #    真實 HTML: <div class="col-4">\n                1 Cheng, Ka Ling            </div>
    name_elem = soup.find(string=re.compile(r"\d+\s+\w+,"))
    if not name_elem:
        return None

    # 2. 提取選手基本資料行
    #    Selector 目標: 包含 "DIV:", "CLASSE:", "FATOR:", "CAT:" 嘅文字
    #    真實 HTML: <div class="col-8 text-right">\n                DIV: Standard CLASSE: C FATOR: Minor CAT:  Lady
    info_text = soup.find(string=re.compile(r"DIV:"))
    if not info_text:
        return None

    div_match   = re.search(r"DIV:\s*(\S+)", info_text)
    cls_match   = re.search(r"CLASSE:\s*(\S+)", info_text)
    fac_match   = re.search(r"FATOR:\s*(\S+)", info_text)
    cat_match   = re.search(r"CAT:\s*(\S+(?:\s+\S+)?)", info_text)

    # 3. 選手姓名（去除前導編號 + 清洗 Excel 錯誤）
    name = re.sub(r"^\d+\s+", "", name_elem.strip())
    name = sanitize_name(name)

    shooter = {
        "name": name.strip(),
        "division": div_match.group(1) if div_match else "",
        "class": cls_match.group(1) if cls_match else "U",
        "factor": fac_match.group(1) if fac_match else "Minor",
        "category": cat_match.group(1) if cat_match else "",
        "stages": [],
    }

    # 4. 提取 Stage 成績表格
    #    Selector 目標: 成績 <table>
    #    若表格有 id/class，改為 soup.select("table#scores") 等
    table = soup.find("table")
    if not table:
        return shooter  # 有選手資料但未有 stage 成績（剛註冊未打）

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        # 忽略 thead 行（少於 10 個欄位）
        if len(cells) < 10:
            continue

        stage_name = cells[0].get_text(strip=True)
        sn_match = re.search(r"(\d+)", stage_name)
        if not sn_match:
            continue

        # ─── TODO: 驗證欄位索引 ─────────────────
        # 索引對應（0-based）：
        #   0: STG      1: FACTOR (HF)  2: PTS
        #   3: A        4: C            5: D
        #   6: MI       7: NS           8: PE
        #   9: (空欄)  10: TIME
        # 若目標網頁欄位順序不同，請修改下方索引。
        try:
            stage = {
                "stage_number": int(sn_match.group(1)),
                "stage_name": stage_name,
                "hit_factor": float(cells[1].get_text(strip=True) or 0),
                "pts":         int(cells[2].get_text(strip=True) or 0),
                "a":           int(cells[3].get_text(strip=True) or 0),
                "c":           int(cells[4].get_text(strip=True) or 0),
                "d":           int(cells[5].get_text(strip=True) or 0),
                "mi":          int(cells[6].get_text(strip=True) or 0),
                "ns":          int(cells[7].get_text(strip=True) or 0),
                "pe":          int(cells[8].get_text(strip=True) or 0),
                "time":        float(cells[10].get_text(strip=True) or 0),
            }
        except (ValueError, IndexError) as e:
            print(f"  [WARN] 解析 Stage 欄位失敗 ({stage_name}): {e}")
            continue

        shooter["stages"].append(stage)

    return shooter


# ═══════════════════════════════════════════════════
#  5. 數據存取（SQLite）
# ═══════════════════════════════════════════════════

def get_db():
    """取得 SQLite 連線（自動建立目錄）"""
    cfg = load_config()
    db_path = ROOT / cfg["database"]["path"]
    os.makedirs(db_path.parent, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if cfg["database"].get("wal_mode", True):
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tables():
    """初始化資料表（如未存在）"""
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
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
        );

        CREATE TABLE IF NOT EXISTS shooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            competitor_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            division TEXT,
            class TEXT,
            factor TEXT,
            category TEXT,
            total_score REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(match_id, competitor_number)
        );

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
            FOREIGN KEY (shooter_id) REFERENCES shooters(id),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(shooter_id, stage_number)
        );

        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            shooters_found INTEGER DEFAULT 0,
            stages_found INTEGER DEFAULT 0,
            status TEXT DEFAULT 'success',
            message TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    print("[DB] 資料表初始化完成")


def save_shooter(conn, match_id, competitor_number, data, cursor=None):
    """
    將 parse_verify_page() 回傳的 shooter dict 存入資料庫。
    支援批次處理（外部傳入 cursor）。
    """
    close_after = False
    if cursor is None:
        conn = get_db()
        cursor = conn.cursor()
        close_after = True

    try:
        # 寫入 shooters 表
        cursor.execute("""
            INSERT OR IGNORE INTO shooters
            (match_id, competitor_number, name, division, class, factor, category)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (match_id, competitor_number, data["name"],
              data["division"], data["class"], data["factor"], data["category"]))
        cursor.execute("""
            UPDATE shooters SET name=?, division=?, class=?, factor=?, category=?,
                                updated_at=datetime('now')
            WHERE match_id=? AND competitor_number=?
        """, (data["name"], data["division"], data["class"],
              data["factor"], data["category"],
              match_id, competitor_number))

        cursor.execute("""
            SELECT id FROM shooters
            WHERE match_id = ? AND competitor_number = ?
        """, (match_id, competitor_number))
        row = cursor.fetchone()
        if not row:
            return
        shooter_id = row["id"]

        # 清除舊 Stage 成績
        cursor.execute("DELETE FROM stage_scores WHERE shooter_id = ?", (shooter_id,))

        # 寫入 stage_scores 表
        for st in data["stages"]:
            cursor.execute("""
                INSERT INTO stage_scores
                (shooter_id, match_id, stage_number, stage_name, hit_factor,
                 pts, a, c, d, mi, ns, pe, time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (shooter_id, match_id,
                  st["stage_number"], st["stage_name"],
                  st["hit_factor"], st["pts"],
                  st["a"], st["c"], st["d"],
                  st["mi"], st["ns"], st["pe"],
                  st["time"]))

        if close_after:
            conn.commit()
    finally:
        if close_after:
            conn.close()


# ═══════════════════════════════════════════════════
#  6. 主流程控制
# ═══════════════════════════════════════════════════

import time as _time

def scrape_match(match_id, base_url, cfg, use_mock=False):
    """
    爬取單一比賽的全部射手（同步 requests 版本）。

    流程:
      1. 從 competitor_number = 1 開始遞增
      2. 連續 empty_streak_limit 個空號就停止
      3. 每成功一個寫入 DB

    ★ 當 use_mock=True 時，使用 Mock HTML 測試（不需連線）
    """
    delay = cfg["scraper"]["request_delay_sec"]
    max_num = cfg["scraper"]["max_competitor"]
    streak_limit = cfg["scraper"]["empty_streak_limit"]
    timeout = cfg["scraper"]["timeout_sec"]

    print(f"[SCRAPE] 開始爬取比賽 #{match_id} (mock={use_mock}, max={max_num}, streak_limit={streak_limit})")
    total_shooters = 0
    total_stages = 0
    empty_streak = 0
    http_errors = 0

    import requests as _req
    _session = _req.Session()
    _session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
        "Referer": base_url,
    })

    conn = get_db()
    cursor = conn.cursor()

    # Skip 已經有 stage data 嘅 shooter number
    cursor.execute("""
        SELECT DISTINCT s.competitor_number FROM shooters s
        WHERE s.match_id = ?
    """, (match_id,))
    shoters_in_db = {r[0] for r in cursor.fetchall()}
    cursor.execute("""
        SELECT COUNT(*) FROM shooters s
        JOIN stage_scores ss ON ss.shooter_id = s.id
        WHERE s.match_id = ? AND ss.hit_factor > 0
    """, (match_id,))
    scored_count = cursor.fetchone()[0]
    skipped = 0
    if scored_count > 0:
        print(f"  [SKIP] {len(shoters_in_db)} 位射手已知，{scored_count} 位已有 stage scores")

    try:
        for num in range(1, max_num + 1):
            if num in shoters_in_db:
                skipped += 1
                if skipped % 20 == 0:
                    print(f"  [{num}] 跳過 (已有數據)")
                continue
            skipped = 0
            if use_mock:
                html = MOCK_VERIFY_HTML
            else:
                verify_url = f"{base_url}/verify/{match_id}?shooter={num}"
                try:
                    resp = _session.get(verify_url, timeout=timeout)
                    print(f"  [{num}] Status: {resp.status_code}, Size: {len(resp.text)}b", end="")
                    resp.raise_for_status()
                    html = resp.text
                except _req.exceptions.HTTPError as e:
                    http_errors += 1
                    print(f" ✗ HTTP {resp.status_code}")
                    empty_streak += 1
                    if empty_streak >= streak_limit:
                        print(f"  [STOP] 連續 {streak_limit} 個 HTTP 錯誤 ({http_errors} total)")
                        break
                    continue
                except Exception as e:
                    http_errors += 1
                    print(f" ✗ 錯誤: {e}")
                    empty_streak += 1
                    if empty_streak >= streak_limit:
                        print(f"  [STOP] 連續 {streak_limit} 個錯誤")
                        break
                    continue

            data = parse_verify_page(html)

            if data is None:
                print(f" ✗ parse 失敗（空號/無效頁面）")
                empty_streak += 1
                if empty_streak >= streak_limit:
                    print(f"  [STOP] 連續 {streak_limit} 個空號")
                    break
                continue

            empty_streak = 0
            save_shooter(conn, match_id, num, data, cursor)
            total_shooters += 1
            total_stages += len(data["stages"])
            print(f" ✓ {data['name']} ({data['division']}, {data['class']}) - {len(data['stages'])} stg")

            if not use_mock:
                _time.sleep(delay)

        conn.commit()

    finally:
        conn.close()

    # 寫 log
    conn2 = get_db()
    c2 = conn2.cursor()
    c2.execute("""
        INSERT INTO scrape_log (match_id, shooters_found, stages_found, message)
        VALUES (?, ?, ?, ?)
    """, (match_id, total_shooters, total_stages,
          f"{'Mock ' if use_mock else ''}模式: {total_shooters} 射手, {total_stages} stages"))
    c2.execute("UPDATE matches SET last_scraped = datetime('now') WHERE id = ?", (match_id,))
    conn2.commit()
    conn2.close()

    print(f"[SCRAPE] 比賽 #{match_id}: 完成! {total_shooters} 射手, {total_stages} stages")
    return total_shooters, total_stages


def sync_matches(base_url):
    """同步比賽列表到資料庫（同步 requests 版本），自動檢測 completion 狀態"""
    import requests as _req
    resp = _req.get(base_url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    html = resp.text
    matches = parse_matches(html)

    conn = get_db()
    c = conn.cursor()
    for m in matches:
        c.execute("""
            INSERT OR IGNORE INTO matches (id, name, date, venue, level, url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (m["id"], m["name"], m["date"], m["venue"], m["level"], m["url"]))
        c.execute("""
            UPDATE matches SET name=?, date=?, venue=?, level=?, url=?
            WHERE id=?
        """, (m["name"], m["date"], m["venue"], m["level"], m["url"], m["id"]))

    conn.commit()

    # 檢測每場比賽 completion 狀態
    print(f"[SYNC] 檢測 {len(matches)} 場比賽 completion 狀態...")
    updated = 0
    for m in matches:
        mid = m["id"]
        is_done = check_match_completion(mid, base_url)
        if is_done is not None:
            new_val = 1 if is_done else 0
            c.execute("UPDATE matches SET is_completed = ? WHERE id = ? AND is_completed != ?",
                      (new_val, mid, new_val))
            if c.rowcount > 0:
                status = "已完成" if is_done else "進行中"
                print(f"  #{mid} → {status}")
                updated += 1

    conn.commit()
    conn.close()
    print(f"[SYNC] 同步 {len(matches)} 場比賽, 更新 {updated} 個 completion 狀態")
    return matches


DIVISION_ID_MAP = {
    1: "Open", 2: "Standard", 3: "Production", 4: "Revolver",
    5: "Classic", 22: "Production Optics", 39: "Optics"
}


def scrape_results_match(match_id, base_url):
    """
    爬取已完成比賽的 Results 頁面，提取 Overall 排名數據。

    流程:
      1. Fetch match page → 檢測有邊幾個 Division（有 Results links）
      2. 逐個 Division fetch Overall results table
      3. 解析: Place, #, Shooter, Category, Class, Factor, Region, Total Score
      4. 寫入 shooters 表（總分直接來自官方排名）

    Division ID map: {1:Open, 2:Standard, 3:Production, 4:Revolver,
                      5:Classic, 22:Production Optics, 39:Optics}
    """
    import requests as _req
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    print(f"[RESULTS] 開始爬取比賽 #{match_id} 的 Results 數據...")

    # 1. 檢測可用 Divisions
    match_url = f"{base_url}?match={match_id}"
    try:
        resp = session.get(match_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[RESULTS] 無法 fetch match page: {e}")
        return 0, 0

    soup = BeautifulSoup(resp.text, "html.parser")
    available_divs = set()
    for a in soup.select("a[href*='results/']"):
        m = re.search(r"division=(\d+)", a.get("href", ""))
        if m:
            available_divs.add(int(m.group(1)))

    if not available_divs:
        # Fallback: try all known divisions
        available_divs = set(DIVISION_ID_MAP.keys())

    print(f"[RESULTS] 可用 Divisions: {[DIVISION_ID_MAP.get(d, d) for d in sorted(available_divs)]}")

    # 2. 逐個 Division 爬取 Overall results
    conn = get_db()
    cursor = conn.cursor()
    total_shooters = 0

    try:
        for div_id in sorted(available_divs):
            div_name = DIVISION_ID_MAP.get(div_id, f"Div{div_id}")
            url = f"{base_url}/results/{match_id}?division={div_id}&group=overall"
            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                print(f"  [{div_name}] HTTP error: {e}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="table")
            if not table:
                print(f"  [{div_name}] 冇 result table")
                continue

            rows = table.find("tbody")
            if not rows:
                print(f"  [{div_name}] 冇 tbody")
                continue

            div_shooters = 0
            for tr in rows.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 9:
                    continue

                try:
                    place = int(cells[0].get_text(strip=True) or 0)
                    comp_num = int(cells[1].get_text(strip=True) or 0)
                    name = cells[2].get_text(strip=True)
                    category = cells[3].get_text(strip=True)
                    cls = cells[4].get_text(strip=True)
                    factor = cells[5].get_text(strip=True)
                    region = cells[6].get_text(strip=True) if len(cells) > 6 else "HKG"
                    total_score = float(cells[7].get_text(strip=True) or 0) if len(cells) > 7 else 0

                    # 清洗 name
                    name = sanitize_name(name)
                    if name == "[Unknown]":
                        continue
                except (ValueError, IndexError) as e:
                    continue

                # 寫入 shooters
                cursor.execute("""
                    INSERT OR IGNORE INTO shooters
                    (match_id, competitor_number, name, division, class, factor, category, region, total_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (match_id, comp_num, name, div_name, cls, factor, category, region, total_score))

                cursor.execute("""
                    UPDATE shooters SET name=?, division=?, class=?, factor=?, category=?,
                                        region=?, total_score=?, updated_at=datetime('now')
                    WHERE match_id=? AND competitor_number=?
                """, (name, div_name, cls, factor, category, region, total_score,
                      match_id, comp_num))

                div_shooters += 1

            total_shooters += div_shooters
            print(f"  [{div_name}] {div_shooters} shooters")

        conn.commit()

    except Exception as e:
        print(f"[RESULTS] 錯誤: {e}")
        import traceback; traceback.print_exc()
    finally:
        conn.close()

    print(f"[RESULTS] 比賽 #{match_id}: 共 {total_shooters} 射手")
    return total_shooters


def main_sync():
    """主入口（同步版本）"""
    cfg = load_config()
    base_url = cfg["base_url"]

    # 解析 CLI 參數
    args = sys.argv[1:]
    use_mock = "--mock" in args
    match_id = None
    for i, a in enumerate(args):
        if a == "--match" and i + 1 < len(args):
            try:
                match_id = int(args[i + 1])
            except ValueError:
                pass

    init_tables()

    if use_mock:
        print("[MOCK] 使用 Mock 數據測試")
        scrape_match(37, base_url, cfg, use_mock=True)
        return

    if match_id:
        print(f"[MAIN] 爬取指定比賽 #{match_id}")
        scrape_match(match_id, base_url, cfg, use_mock=False)
        return

    # 預設模式：同步比賽列表 → 爬取所有未完賽比賽
    print("[MAIN] 同步比賽列表...")
    sync_matches(base_url)

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, name FROM matches WHERE is_completed = 0 ORDER BY id DESC
    """)
    active = [dict(r) for r in c.fetchall()]
    conn.close()

    for m in active:
        print(f"\n[MAIN] 處理比賽 #{m['id']}: {m['name']}")
        scrape_match(m["id"], base_url, cfg, use_mock=False)

    print("\n[MAIN] 全部完成！")


def main():
    """同步入口"""
    main_sync()


if __name__ == "__main__":
    main()
