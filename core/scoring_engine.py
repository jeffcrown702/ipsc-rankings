"""IPSC 核心計分引擎

嚴格按照 IPSC 標準公式計算：
1. HF = PTS / TIME
2. Max_HF = 該 Stage 所有射手最高 HF
3. Max_Stage_Score = (A + C + D + MI) × 5
4. Stage_Score = (個人 HF / Max_HF) × Max_Stage_Score
5. Total Score = Σ 所有 Stage_Score
"""
import sqlite3
from core.database import get_db
from core.config import DIVISIONS, CATEGORIES, CLASSES


def calculate_division_rankings(match_id, division):
    """為指定比賽 + Division 計算所有排名"""
    db = get_db()
    cursor = db.cursor()

    # 1. 取得該 Division 所有射手
    cursor.execute("""
        SELECT s.id, s.competitor_number, s.name, s.division,
               s.class, s.factor, s.category, s.region, s.total_score
        FROM shooters s
        WHERE s.match_id = ? AND s.division = ?
        ORDER BY s.competitor_number
    """, (match_id, division))
    shooters = cursor.fetchall()

    if not shooters:
        print(f"[RANK] {division}: 冇射手")
        return

    shooter_ids = [s["id"] for s in shooters]
    print(f"[RANK] {division}: {len(shooters)} 位射手")

    # 2. 取得所有射手嘅 Stage 成績
    placeholders = ",".join("?" for _ in shooter_ids)
    cursor.execute(f"""
        SELECT ss.shooter_id, ss.stage_number, ss.stage_name,
               ss.hit_factor, ss.pts, ss.a, ss.c, ss.d, ss.mi, ss.ns, ss.pe, ss.time
        FROM stage_scores ss
        WHERE ss.shooter_id IN ({placeholders})
        ORDER BY ss.shooter_id, ss.stage_number
    """, shooter_ids)
    stage_rows = cursor.fetchall()

    # 組織數據: {shooter_id: {stage_num: {...}}}
    shooter_stages = {}
    for row in stage_rows:
        sid = row["shooter_id"]
        if sid not in shooter_stages:
            shooter_stages[sid] = {}
        shooter_stages[sid][row["stage_number"]] = {
            "stage_name": row["stage_name"],
            "hit_factor": row["hit_factor"],
            "pts": row["pts"],
            "a": row["a"],
            "c": row["c"],
            "d": row["d"],
            "mi": row["mi"],
            "ns": row["ns"],
            "pe": row["pe"],
            "time": row["time"],
        }

    # 3. 取得所有 Stage 編號
    cursor.execute("""
        SELECT DISTINCT stage_number
        FROM stage_scores
        WHERE shooter_id IN ({placeholders})
        ORDER BY stage_number
    """.format(placeholders=placeholders), shooter_ids)
    all_stages = [row["stage_number"] for row in cursor.fetchall()]

    # 4. 計算每個 Stage 的 Max_HF 和 Max_Stage_Score
    #    IPSC 標準：Max_Stage_Score = (A+C+D+MI) × 5
    #    其中 A,C,D,MI 係該 Stage 內**單一射手**最高嘅總紙靶數
    stage_max_hf = {}
    stage_max_score = {}
    for stage_num in all_stages:
        hf_values = []
        max_paper_sum = 0
        for sid in shooter_ids:
            stages = shooter_stages.get(sid, {})
            ss = stages.get(stage_num)
            if ss and ss["time"] > 0:
                hf_values.append(ss["hit_factor"])
                paper_sum = ss["a"] + ss["c"] + ss["d"] + ss["mi"]
                if paper_sum > max_paper_sum:
                    max_paper_sum = paper_sum

        stage_max_hf[stage_num] = max(hf_values) if hf_values else 1
        # Max_Stage_Score = (A + C + D + MI) × 5（單一射手最高紙靶總和）
        stage_max_score[stage_num] = max_paper_sum * 5

    # 5. 計算每人 Stage_Score 同 Total_Score
    #    若有 stage 數據 → 從 stage_scores 計算
    #    若冇 stage 數據（completed match）→ 直接用 shooters.total_score
    shooter_totals = {}

    if all_stages:
        # 有 stage 數據：從 Hit Factor 計算
        for sid in shooter_ids:
            total = 0
            stages = shooter_stages.get(sid, {})
            for stage_num in all_stages:
                ss = stages.get(stage_num)
                if ss and ss["time"] > 0:
                    max_hf = stage_max_hf.get(stage_num, 1)
                    max_ss = stage_max_score.get(stage_num, 0)
                    hf = ss["hit_factor"]
                    stage_score = (hf / max_hf) * max_ss if max_hf > 0 else 0
                    total += stage_score

                    # ★ 回寫 dict（供後續 stage ranking 使用）
                    stages[stage_num]["stage_score"] = stage_score

                    cursor.execute("""
                        UPDATE stage_scores
                        SET stage_score = ?
                        WHERE shooter_id = ? AND stage_number = ?
                    """, (round(stage_score, 4), sid, stage_num))
                else:
                    if stage_num in stages:
                        stages[stage_num]["stage_score"] = 0

            shooter_totals[sid] = round(total, 4)

            cursor.execute("""
                UPDATE shooters
                SET total_score = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (round(total, 4), sid))
    else:
        # 冇 stage 數據：直接用 shooters.total_score（e.g. completed match 已有 results）
        for s in shooters:
            ts = s["total_score"] or 0
            shooter_totals[s["id"]] = float(ts)

    db.commit()

    # 6. 清除舊排名
    cursor.execute("""
        DELETE FROM rankings
        WHERE match_id = ? AND division = ?
    """, (match_id, division))

    # 建立射手查找表
    shooter_map = {s["id"]: s for s in shooters}

    # ===== OVERALL 排名 (score=0 排最後) =====
    def sort_key(sid):
        return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
    ranked = sorted(shooter_ids, key=sort_key)
    # 有分數嘅射手先計 Place
    valid_count = 0
    top_score = 0
    for sid in ranked:
        ts = shooter_totals.get(sid, 0)
        if ts > 0:
            valid_count += 1
            if top_score == 0:
                top_score = ts

    for place_counter, sid in enumerate(ranked, 1):
        ts = shooter_totals.get(sid, 0)
        if ts > 0:
            place = sum(1 for s2 in ranked[:place_counter] if shooter_totals.get(s2, 0) > 0)
        else:
            place = 0  # 無分選手 place=0，前端會顯示「—」
        pct = round((ts / top_score) * 100, 2) if top_score > 0 and ts > 0 else 0
        s = shooter_map[sid]
        cursor.execute("""
            INSERT OR REPLACE INTO rankings
            (match_id, division, rank_type, group_key, competitor_number,
             place, total_score, score_percent, calculated_at)
            VALUES (?, ?, 'overall', '', ?, ?, ?, ?, datetime('now'))
        """, (match_id, division, s["competitor_number"], place, ts, pct))

    # ===== CATEGORY 排名 =====
    cat_shooters = {}
    for s in shooters:
        cat = s["category"].strip() if s["category"] else "None"
        if cat and cat != "None":
            if cat not in cat_shooters:
                cat_shooters[cat] = []
            cat_shooters[cat].append(s["id"])

    for cat, sids in cat_shooters.items():
        def cat_sort(sid):
            return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
        ranked_cat = sorted(sids, key=cat_sort)
        top_cat = 0
        valid_cat = 0
        for sid in ranked_cat:
            if shooter_totals.get(sid, 0) > 0:
                valid_cat += 1
                if top_cat == 0:
                    top_cat = shooter_totals[sid]
        for pc, sid in enumerate(ranked_cat, 1):
            ts = shooter_totals.get(sid, 0)
            if ts > 0:
                place_cat = sum(1 for s2 in ranked_cat[:pc] if shooter_totals.get(s2, 0) > 0)
            else:
                place_cat = 0  # 無分選手 place=0
            pct = round((ts / top_cat) * 100, 2) if top_cat > 0 and ts > 0 else 0
            s = shooter_map[sid]
            cursor.execute("""
                INSERT OR REPLACE INTO rankings
                (match_id, division, rank_type, group_key, competitor_number,
                 place, total_score, score_percent, calculated_at)
                VALUES (?, ?, 'category', ?, ?, ?, ?, ?, datetime('now'))
            """, (match_id, division, cat, s["competitor_number"], place_cat, ts, pct))

    # ===== CLASS 排名 (score=0 排最後) =====
    class_shooters = {}
    for s in shooters:
        cls = s["class"].strip() if s["class"] else "U"
        if cls not in class_shooters:
            class_shooters[cls] = []
        class_shooters[cls].append(s["id"])

    for cls, sids in class_shooters.items():
        def cls_sort(sid):
            return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
        ranked_cls = sorted(sids, key=cls_sort)
        top_cls = 0
        valid_cls = 0
        for sid in ranked_cls:
            if shooter_totals.get(sid, 0) > 0:
                valid_cls += 1
                if top_cls == 0:
                    top_cls = shooter_totals[sid]
        for pc, sid in enumerate(ranked_cls, 1):
            ts = shooter_totals.get(sid, 0)
            if ts > 0:
                place_cls = sum(1 for s2 in ranked_cls[:pc] if shooter_totals.get(s2, 0) > 0)
            else:
                place_cls = 0  # 無分選手 place=0
            pct = round((ts / top_cls) * 100, 2) if top_cls > 0 and ts > 0 else 0
            s = shooter_map[sid]
            cursor.execute("""
                INSERT OR REPLACE INTO rankings
                (match_id, division, rank_type, group_key, competitor_number,
                 place, total_score, score_percent, calculated_at)
                VALUES (?, ?, 'class', ?, ?, ?, ?, ?, datetime('now'))
            """, (match_id, division, cls, s["competitor_number"], place_cls, ts, pct))

    # ===== STAGE 排名 =====
    for stage_num in all_stages:
        stage_ranked = []
        for sid in shooter_ids:
            stages = shooter_stages.get(sid, {})
            ss = stages.get(stage_num)
            if ss and ss["time"] > 0:
                stage_ranked.append((sid, ss["hit_factor"], ss.get("stage_score", 0)))
            else:
                stage_ranked.append((sid, 0, 0))

        stage_ranked.sort(key=lambda x: x[1], reverse=True)
        for place, (sid, hf, ss_score) in enumerate(stage_ranked, 1):
            s = shooter_map[sid]
            stage_key = f"STAGE {stage_num:02d}"
            cursor.execute("""
                INSERT OR REPLACE INTO rankings
                (match_id, division, rank_type, group_key, competitor_number,
                 place, total_score, score_percent, calculated_at)
                VALUES (?, ?, 'stage', ?, ?, ?, ?, ?, datetime('now'))
            """, (match_id, division, stage_key, s["competitor_number"],
                  place, round(ss_score, 4), round(hf, 4)))

    db.commit()
    db.close()
    print(f"[RANK] {division}: 排名計算完成 (Overall + Category + Class + Stage)")


def calculate_all_rankings(match_id):
    """為比賽中所有有數據嘅 Division 計算排名，另加 *ALL* 跨組排名"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT DISTINCT division FROM shooters
        WHERE match_id = ?
          AND division IS NOT NULL
          AND division != ''
        ORDER BY division
    """, (match_id,))
    divisions = [row["division"] for row in cursor.fetchall()]
    db.close()

    for div in divisions:
        calculate_division_rankings(match_id, div)

    # === *ALL* 跨 Division 排名 ===
    division = "*ALL*"
    db = get_db()
    cursor = db.cursor()

    # 1. 取得全部射手
    cursor.execute("""
        SELECT s.id, s.competitor_number, s.name, s.division,
               s.class, s.factor, s.category, s.region, s.total_score
        FROM shooters s
        WHERE s.match_id = ?
        ORDER BY s.competitor_number
    """, (match_id,))
    shooters = cursor.fetchall()

    if shooters:
        shooter_ids = [s["id"] for s in shooters]
        shooter_map = {s["id"]: s for s in shooters}
        placeholders = ",".join("?" for _ in shooter_ids)

        # 2. 取得全部 stage_scores
        cursor.execute(f"""
            SELECT ss.shooter_id, ss.stage_number, ss.stage_name,
                   ss.hit_factor, ss.pts, ss.a, ss.c, ss.d, ss.mi, ss.ns, ss.pe, ss.time
            FROM stage_scores ss
            WHERE ss.shooter_id IN ({placeholders})
            ORDER BY ss.shooter_id, ss.stage_number
        """, shooter_ids)
        stage_rows = cursor.fetchall()

        # 3. 組織數據
        shooter_stages = {}
        for row in stage_rows:
            sid = row["shooter_id"]
            if sid not in shooter_stages:
                shooter_stages[sid] = {}
            shooter_stages[sid][row["stage_number"]] = {
                k: row[k] for k in ["stage_name", "hit_factor", "pts", "a", "c", "d", "mi", "ns", "pe", "time"]
            }

        # 4. 所有 Stage
        cursor.execute(f"""
            SELECT DISTINCT stage_number FROM stage_scores
            WHERE shooter_id IN ({placeholders}) ORDER BY stage_number
        """, shooter_ids)
        all_stages = [r["stage_number"] for r in cursor.fetchall()]

        # 5. Max_HF 同 Max_Stage_Score
        stage_max_hf = {}
        stage_max_score = {}
        for stage_num in all_stages:
            hf_vals = []
            max_paper_sum = 0
            for sid in shooter_ids:
                ss = shooter_stages.get(sid, {}).get(stage_num)
                if ss and ss["time"] > 0:
                    hf_vals.append(ss["hit_factor"])
                    paper_sum = ss["a"] + ss["c"] + ss["d"] + ss["mi"]
                    if paper_sum > max_paper_sum:
                        max_paper_sum = paper_sum
            stage_max_hf[stage_num] = max(hf_vals) if hf_vals else 1
            stage_max_score[stage_num] = max_paper_sum * 5

        # 6. 計算每人 Score
        #    若有 stage 數據 → 從 stage_scores 計算
        #    若冇 stage 數據 → 直接用 shooters.total_score
        shooter_totals = {}
        if all_stages:
            for sid in shooter_ids:
                total = 0
                for stage_num in all_stages:
                    ss = shooter_stages.get(sid, {}).get(stage_num)
                    if ss and ss["time"] > 0:
                        mhf = stage_max_hf.get(stage_num, 1)
                        mss = stage_max_score.get(stage_num, 0)
                        stage_score = (ss["hit_factor"] / mhf) * mss if mhf > 0 else 0
                        total += stage_score
                        # ★ 回寫 dict（供後續 stage ranking 使用）
                        shooter_stages[sid][stage_num]["stage_score"] = stage_score
                    else:
                        if sid in shooter_stages and stage_num in shooter_stages.get(sid, {}):
                            shooter_stages[sid][stage_num]["stage_score"] = 0
                shooter_totals[sid] = round(total, 4)
        else:
            # 冇 stage 數據：直接用 shooters.total_score
            for s in shooters:
                shooter_totals[s["id"]] = float(s["total_score"] or 0)

        # 7. 清除舊 *ALL* 排名
        cursor.execute("DELETE FROM rankings WHERE match_id = ? AND division = ?", (match_id, division))

        # === OVERALL ===
        def all_sort_key(sid):
            return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
        ranked = sorted(shooter_ids, key=all_sort_key)
        top_score = 0
        for sid in ranked:
            if shooter_totals.get(sid, 0) > 0:
                top_score = shooter_totals[sid]
                break
        for place_counter, sid in enumerate(ranked, 1):
            ts = shooter_totals.get(sid, 0)
            if ts > 0:
                place = sum(1 for s2 in ranked[:place_counter] if shooter_totals.get(s2, 0) > 0)
            else:
                place = 0
            pct = round((ts / top_score) * 100, 2) if top_score > 0 and ts > 0 else 0
            s = shooter_map[sid]
            cursor.execute(
                "INSERT OR REPLACE INTO rankings (match_id, division, rank_type, group_key, competitor_number, place, total_score, score_percent) VALUES (?,?,?,?,?,?,?,?)",
                (match_id, division, "overall", None, s["competitor_number"], place, ts, pct))

        # === CATEGORY ===
        cat_shooters = {}
        for s in shooters:
            cat = s["category"].strip() if s["category"] else ""
            if cat:
                cat_shooters.setdefault(cat, []).append(s["id"])
        for cat, sids in cat_shooters.items():
            def cat_sort(sid):
                return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
            rc = sorted(sids, key=cat_sort)
            tc = 0
            for sid in rc:
                if shooter_totals.get(sid, 0) > 0:
                    tc = shooter_totals[sid]
                    break
            for pc, sid in enumerate(rc, 1):
                ts = shooter_totals.get(sid, 0)
                if ts > 0:
                    place_cat = sum(1 for s2 in rc[:pc] if shooter_totals.get(s2, 0) > 0)
                else:
                    place_cat = 0
                pct = round((ts / tc) * 100, 2) if tc > 0 and ts > 0 else 0
                s = shooter_map[sid]
                cursor.execute(
                    "INSERT OR REPLACE INTO rankings (match_id, division, rank_type, group_key, competitor_number, place, total_score, score_percent) VALUES (?,?,?,?,?,?,?,?)",
                    (match_id, division, "category", cat, s["competitor_number"], place_cat, ts, pct))

        # === CLASS ===
        cls_shooters = {}
        for s in shooters:
            cls = s["class"].strip() if s["class"] else "U"
            cls_shooters.setdefault(cls, []).append(s["id"])
        for cls, sids in cls_shooters.items():
            def cls_sort(sid):
                return (0 if shooter_totals.get(sid, 0) > 0 else 1, -(shooter_totals.get(sid, 0)))
            rcls = sorted(sids, key=cls_sort)
            tcls = 0
            for sid in rcls:
                if shooter_totals.get(sid, 0) > 0:
                    tcls = shooter_totals[sid]
                    break
            for pc, sid in enumerate(rcls, 1):
                ts = shooter_totals.get(sid, 0)
                if ts > 0:
                    place_cls = sum(1 for s2 in rcls[:pc] if shooter_totals.get(s2, 0) > 0)
                else:
                    place_cls = 0
                pct = round((ts / tcls) * 100, 2) if tcls > 0 and ts > 0 else 0
                s = shooter_map[sid]
                cursor.execute(
                    "INSERT OR REPLACE INTO rankings (match_id, division, rank_type, group_key, competitor_number, place, total_score, score_percent) VALUES (?,?,?,?,?,?,?,?)",
                    (match_id, division, "class", cls, s["competitor_number"], place_cls, ts, pct))

        # === STAGE ===
        for stage_num in all_stages:
            stage_key = f"STAGE {stage_num:02d}"
            stage_data = [(sid,
                           shooter_stages.get(sid, {}).get(stage_num, {}).get("hit_factor", 0),
                           shooter_stages.get(sid, {}).get(stage_num, {}).get("stage_score", 0))
                          for sid in shooter_ids]
            stage_data.sort(key=lambda x: x[1], reverse=True)
            for place, (sid, hf, ss_score) in enumerate(stage_data, 1):
                s = shooter_map[sid]
                cursor.execute(
                    "INSERT OR REPLACE INTO rankings (match_id, division, rank_type, group_key, competitor_number, place, total_score, score_percent) VALUES (?,?,?,?,?,?,?,?)",
                    (match_id, division, "stage", stage_key, s["competitor_number"], place, round(ss_score, 4), round(hf, 4)))

    db.commit()
    db.close()
    print(f"[RANK] 比賽 #{match_id}: *ALL* 跨 Division 排名完成")
    print(f"[RANK] 比賽 #{match_id}: 所有 Division + *ALL* 排名完成")


if __name__ == "__main__":
    import sys
    match_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if match_id:
        calculate_all_rankings(match_id)
    else:
        print("請提供 match_id")
