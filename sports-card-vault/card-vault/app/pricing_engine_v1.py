# -*- coding: utf-8 -*-
"""pricing_engine_v1.py —— Card Vault 智能定价引擎 v1（挂在外挂层，不改 main.py）。

四个模块全在这里：
  1) 严格锚点过滤 + 硬边界 + IQR + **Exact SKU Bucket 内 90 天加权中位数**
  2) market_multiplier_matrix 学习 + 跨卡横向推演（无成交时的降级路径）
  3) 人工纠偏：/api/cards/{id}/verify、反馈日志、canonical 反写、噪音黑名单聚合
  4) case_hit / SSP 词表驱动，禁止与普通 Insert 混算

核心铁律（用户拍板）：
  · 计算中位数前**必须**按 (Year_Set + Player + Card_Number + Parallel +
    Serial_Denominator + Grade_Status) 六维严格分桶，严禁跨桶混算；
  · 同桶样本 < 3 → **绝不跨桶硬凑**，降级矩阵推演或保留人工锚点；
  · is_verified=1 且 price_lock_floor>0 时，低于保护下限的估值不覆盖。
"""
import json
import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger("pricing_engine_v1")

# ---------------------------------------------------------------- 常量
SERIAL_BUCKETS = ((1, 1, "1_1"), (2, 10, "1_10"), (11, 25, "15_25"), (26, 50, "35_50"),
                  (51, 99, "75_99"), (100, 199, "125_199"), (200, 399, "250_399"))

# 无矩阵数据时的兜底阶梯（相对 base 的倍数，经验值，会被学习结果覆盖）
DEFAULT_LADDER = {"1_1": 14.0, "1_10": 8.0, "15_25": 5.0, "35_50": 3.2,
                  "75_99": 1.9, "125_199": 1.3, "250_399": 1.0, "base": 0.45}

W_30, W_90, W_OLD = 1.0, 0.6, 0.3
MIN_BUCKET_N = 3          # 同桶样本 >=3 才算「高置信度·同款实盘」
LOW_PRICE_RATIO = 0.20    # 低于 est 的 20% → 噪音
HIGH_PRICE_RATIO = 3.00   # 高于 est 的 300% → 噪音
LOW_CONF_SWING = 0.40     # 样本<2 且波动 >±40% → 不覆盖

PARALLEL_TITLE_TOKENS = {
    "refractor": ("refractor", "折射", "银折", "x-fractor"),
    "silver": ("silver", "银折", "银"), "gold": ("gold", "金折", "金"),
    "pink": ("pink", "粉折", "粉冰", "粉"), "blue": ("blue", "蓝折", "蓝"),
    "purple": ("purple", "紫折", "紫"), "green": ("green", "绿折", "绿"),
    "red": ("red", "红折", "红"), "orange": ("orange", "橙折", "橙"),
    "black": ("black", "黑折", "黑"), "aqua": ("aqua", "水蓝"),
    "prizm": ("prizm", "波纹"), "sapphire": ("sapphire", "蓝宝石"),
    "platinum": ("platinum", "白金"), "blue_porcelain": ("青花瓷", "porcelain"),
    "masterpiece": ("masterpiece", "杰作"), "metal": ("metal", "金属"),
    "sketch": ("sketch", "手绘"), "booklet": ("booklet", "小书", "书卡"),
    "rr": ("rr",),
}

_NORM_RE = re.compile(r"[\s\-_·•・.,，、/|()（）\[\]【】]+")


def _norm(text):
    return _NORM_RE.sub("", str(text or "")).lower()


def _get(row, key, default=""):
    if row is None:
        return default
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(key, default) if isinstance(row, dict) else default
    return row[key] if key in keys else default


# ---------------------------------------------------------------- 桶
def _denom_of(card):
    """编数分母（1/1 → 1；/250 → 250；无编 → None）。"""
    s = str(_get(card, "serial") or "")
    if re.search(r"one\s*of\s*one|1\s*of\s*1", s, re.I):
        return 1
    m = re.search(r"(\d{1,4})\s*/\s*(\d{1,4})", s)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            pass
    m = re.search(r"/\s*(\d{1,4})", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,4})\s*编", s)
    if m:
        return int(m.group(1))
    return None


def _denom_bucket(den):
    if den is None:
        return "base"
    for lo, hi, label in SERIAL_BUCKETS:
        if lo <= den <= hi:
            return label
    return "over_399"


def _grade_bucket(card):
    grader = str(_get(card, "grader") or "").strip().upper()
    try:
        score = float(_get(card, "grade_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if not grader and score <= 0:
        return "raw"
    return "%s%s" % (grader or "GRADED", ("%g" % score) if score else "")


def _parallel_family(parallel):
    p = str(parallel or "").strip().lower()
    return "base" if p in ("", "base", "普卡") else p


def bucket_key(card):
    """六维严格桶键。同桶才允许算中位数。"""
    player = _norm(re.sub(r"[（(][^）)]*[）)]", "", str(_get(card, "player") or "")))
    return (
        _norm(_get(card, "year_set")),
        player,
        str(_get(card, "card_number") or "").strip().lower(),
        _parallel_family(_get(card, "parallel")),
        _denom_bucket(_denom_of(card)),
        _grade_bucket(card),
    )


# ---------------------------------------------------------------- 样本处理
def _sale_age_days(sold_date):
    text = str(sold_date or "").strip()
    if not text:
        return 9999.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = time.mktime(time.strptime(text[:len(time.strftime(fmt))], fmt))
            return max(0.0, (time.time() - t) / 86400.0)
        except (ValueError, OverflowError):
            continue
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if m:
        try:
            t = time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0, 0, 0, -1))
            return max(0.0, (time.time() - t) / 86400.0)
        except (ValueError, OverflowError):
            pass
    return 9999.0


def age_weight(days):
    if days <= 30:
        return W_30
    if days <= 90:
        return W_90
    return W_OLD


def weighted_median(pairs):
    """pairs: [(value, weight)] → 加权中位数。"""
    items = sorted((float(v), float(w)) for v, w in pairs if v and v > 0 and w > 0)
    if not items:
        return 0.0
    total = sum(w for _, w in items)
    half = total / 2.0
    acc = 0.0
    for value, weight in items:
        acc += weight
        if acc >= half:
            return value
    return items[-1][0]


def iqr_filter(prices):
    vals = sorted(float(p) for p in prices if p and float(p) > 0)
    if len(vals) < 4:
        return vals
    n = len(vals)
    q1, q3 = vals[n // 4], vals[(3 * n) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return vals
    lo, hi = max(0.5, q1 - 1.5 * iqr), q3 + 1.5 * iqr
    kept = [v for v in vals if lo <= v <= hi]
    return kept or vals


def _title_has_number(title, number):
    if not number:
        return False
    t = str(title or "")
    num = str(number).strip()
    patterns = [r"#\s*%s\b", r"\bNO\.?\s*%s\b", r"\b%s\b"]
    for p in patterns:
        try:
            if re.search(p % re.escape(num), t, re.I):
                return True
        except re.error:
            continue
    return False


def _title_has_serial(title, denom):
    if not denom:
        return False
    t = str(title or "")
    return bool(re.search(r"/\s*%d\b" % denom, t) or re.search(r"\b%d\s*编" % denom, t)
                or re.search(r"\b\d{1,4}\s*/\s*%d\b" % denom, t))


def _title_has_parallel(title, parallel):
    low = str(title or "").lower()
    if not low:
        return False
    return any(tok and tok in low for tok in
               PARALLEL_TITLE_TOKENS.get(str(parallel or "").strip().lower(), ()))


def anchor_check(card, sale, case_tokens):
    """严格锚点：卡号 / 编数分母 / 平行词 / 特卡词，至少命中一个。

    返回 (ok, reason)。reason 用于 anchor_reject_log。
    """
    title = str(sale.get("title") or "")
    number = str(_get(card, "card_number") or "").strip()
    denom = _denom_of(card)
    par = _parallel_family(_get(card, "parallel"))
    tier = str(_get(card, "tier_key") or "")

    hits = []
    if _title_has_number(title, number):
        hits.append("number")
    if _title_has_serial(title, denom):
        hits.append("serial")
    if par not in ("base", "") and _title_has_parallel(title, par):
        hits.append("parallel")
    if tier == "case_hit":
        low = title.lower()
        if any(t and t in low for t in case_tokens):
            hits.append("case_hit")

    if hits:
        # 有平行但标题全无折/编字样 → 视为纸卡，剔除
        if par not in ("base", "") and not _title_has_parallel(title, par) and not _title_has_serial(title, denom):
            return False, "parallel_noise"
        return True, "+".join(hits)
    if tier == "case_hit":
        return False, "no_case_hit"
    if par not in ("base", ""):
        return False, "no_parallel"
    if denom:
        return False, "no_serial"
    if number:
        return False, "no_card_number"
    return True, "no_anchor_required"


# ---------------------------------------------------------------- 模块二：矩阵
def _sale_title_tokens(title):
    return _norm(title)


def bind_sales_card_ids(conn, limit=20000):
    """把 market_sales 里 card_id 为空的成交按「六维桶键」规则回填到卡片。

    匹配强度（从严到宽，只接受命中即停的最强一条）：
      1) 球员命中 + 卡号命中 + 编数分母命中
      2) 球员命中 + 卡号命中
      3) 球员命中 + 编数分母命中 + 系列词命中
    只要候选多于 1 张卡就放弃（宁可漏，不可错绑）。
    """
    try:
        cards = [dict(r) for r in conn.execute(
            "SELECT card_id, player, year_set, card_number, parallel, serial,"
            "       grader, grade_score FROM cards")]
    except sqlite3.Error:
        return {"bound": 0, "scanned": 0, "ambiguous": 0}

    idx = []
    for c in cards:
        aliases = []
        name = str(c.get("player") or "")
        plain = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
        for part in (plain, name):
            part = part.strip()
            if len(part) >= 2:
                aliases.append(_norm(part))
        m = re.search(r"[（(]([^）)]+)[）)]", name)
        if m:  # 括号里的英文名
            for token in re.split(r"[\s/]+", m.group(1)):
                if len(token) >= 3:
                    aliases.append(_norm(token))
        idx.append({
            "card_id": c["card_id"], "aliases": [a for a in aliases if a],
            "number": str(c.get("card_number") or "").strip().lower(),
            "denom": _denom_of(c), "series": _norm(c.get("year_set")),
        })

    try:
        rows = conn.execute(
            "SELECT id, title FROM market_sales"
            " WHERE card_id IS NULL OR TRIM(COALESCE(card_id,''))='' LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.Error:
        return {"bound": 0, "scanned": 0, "ambiguous": 0}

    bound = ambiguous = 0
    for r in rows:
        sid = r[0]
        title = str(r[1] or "")
        if not title:
            continue
        tnorm = _sale_title_tokens(title)
        cands = []
        for rec in idx:
            if not rec["aliases"]:
                continue
            if not any(a and a in tnorm for a in rec["aliases"]):
                continue
            has_num = bool(rec["number"]) and _title_has_number(title, rec["number"])
            has_ser = _title_has_serial(title, rec["denom"])
            series_hit = bool(rec["series"]) and all(
                w in tnorm for w in [rec["series"][:6]] if w)
            score = (3 if has_num else 0) + (2 if has_ser else 0) + (1 if series_hit else 0)
            if score >= 2:
                cands.append((score, rec["card_id"]))
        if not cands:
            continue
        cands.sort(reverse=True)
        if len({c[1] for c in cands}) > 1 and cands[0][0] == cands[1][0]:
            ambiguous += 1
            continue
        conn.execute("UPDATE market_sales SET card_id=? WHERE id=?", (cands[0][1], sid))
        bound += 1
    conn.commit()
    return {"bound": bound, "scanned": len(rows), "ambiguous": ambiguous}


def learn_matrix(conn):
    """从 market_sales ⋈ cards 学「系列 × 档位」倍数（90 天加权中位数 ÷ base）。

    学习前先自动绑定 card_id，否则 JOIN 命中率极低（实测 <5%），矩阵覆盖上不去。
    """
    try:
        bind_info = bind_sales_card_ids(conn)
    except Exception as exc:
        logger.debug("自动绑定失败（不阻塞学习）：%s", exc)
        bind_info = {"bound": 0}
    rows = conn.execute(
        "SELECT c.year_set AS ys, c.tier_key AS tk, c.parallel AS par, c.serial AS ser,"
        "       m.price AS price, m.sold_date AS d"
        " FROM market_sales m JOIN cards c ON c.card_id = m.card_id"
        " WHERE m.price > 0").fetchall()
    buckets = {}
    for r in rows:
        ys = _norm(_get(r, "ys"))
        tier = str(_get(r, "tk") or "").strip()
        if not ys or not tier:
            continue
        days = _sale_age_days(_get(r, "d"))
        buckets.setdefault((ys, tier), []).append((float(_get(r, "price")), age_weight(days)))

    med = {}
    for (ys, tier), pairs in buckets.items():
        med[(ys, tier)] = (weighted_median(pairs), len(pairs))

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    for (ys, tier), (value, n) in med.items():
        base_val = med.get((ys, "base"), (None, 0))[0]
        if not base_val or base_val <= 0:
            continue
        mult = round(value / base_val, 3)
        if not (0.05 <= mult <= 60):
            continue
        conn.execute(
            "INSERT INTO market_multiplier_matrix (series, tier_key, multiplier, sample_n, basis, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(series, tier_key) DO UPDATE SET"
            " multiplier=excluded.multiplier, sample_n=excluded.sample_n,"
            " basis=excluded.basis, updated_at=excluded.updated_at",
            (ys, tier, mult, n, "learned_90d_wmedian" if n >= 3 else "learned_thin", now))
        written += 1
    conn.commit()
    return {"series_tier_pairs": written, "source_rows": len(rows),
            "sales_bound": bind_info.get("bound", 0)}


def matrix_lookup(conn, series_norm, tier):
    row = conn.execute(
        "SELECT multiplier, sample_n FROM market_multiplier_matrix WHERE series=? AND tier_key=?",
        (series_norm, tier)).fetchone()
    if row and float(row[0] or 0) > 0:
        return float(row[0]), int(row[1] or 0), "matrix"
    return None, 0, "none"


def _ladder_multiplier(bucket_label):
    return DEFAULT_LADDER.get(bucket_label, 1.0)


def matrix_infer(conn, card):
    """无同桶样本时的横向推演：同球员+同系列其它档位 × 阶梯倍数。"""
    player = _norm(re.sub(r"[（(][^）)]*[）)]", "", str(_get(card, "player") or "")))
    ys_norm = _norm(_get(card, "year_set"))
    target_bucket = _denom_bucket(_denom_of(card))
    if not player or not ys_norm:
        return None
    try:
        rows = conn.execute(
            "SELECT c.serial AS ser, m.price AS price, m.sold_date AS d"
            " FROM market_sales m JOIN cards c ON c.card_id = m.card_id"
            " WHERE m.price > 0 AND c.card_id <> ?"
            "   AND LOWER(REPLACE(COALESCE(c.year_set,''),' ','')) = ?"
            "   AND LOWER(COALESCE(c.player,'')) LIKE ?",
            (str(_get(card, "card_id") or ""), ys_norm.replace(" ", ""),
             "%" + str(_get(card, "player") or "").split("(")[0].strip().lower() + "%")).fetchall()
    except sqlite3.Error:
        rows = []

    by_bucket = {}
    for r in rows:
        den = _denom_of({"serial": _get(r, "ser")})
        label = _denom_bucket(den)
        by_bucket.setdefault(label, []).append(
            (float(_get(r, "price")), age_weight(_sale_age_days(_get(r, "d")))))
    if not by_bucket:
        return None

    # 兜底：market_sales 绑定 card_id 的比例很低（实测 <5%），
    # 改用「同球员 + 同系列」在库其它卡的现估值当参照，覆盖面大得多。
    if not by_bucket:
        try:
            others = conn.execute(
                "SELECT card_id, year_set, player, serial, est_raw_price FROM cards"
                " WHERE card_id <> ? AND COALESCE(status,'')!='已售出' AND est_raw_price > 0",
                (str(_get(card, "card_id") or ""),)).fetchall()
        except sqlite3.Error:
            others = []
        for r in others:
            if _norm(_get(r, "year_set")) != ys_norm:
                continue
            if player not in _norm(re.sub(r"[（(][^）)]*[）)]", "", str(_get(r, "player") or ""))):
                continue
            label = _denom_bucket(_denom_of({"serial": _get(r, "serial")}))
            by_bucket.setdefault(label, []).append((float(_get(r, "est_raw_price") or 0), 1.0))
        if by_bucket:
            logger.info("矩阵推演兜底：用同球员同系列在库卡估值作参照（%d 个档位）", len(by_bucket))

    target_mult = _ladder_multiplier(target_bucket)
    best = None
    for label, pairs in by_bucket.items():
        ref_mult = _ladder_multiplier(label)
        if not ref_mult or ref_mult <= 0:
            continue
        ref_val = weighted_median(pairs)
        if ref_val <= 0:
            continue
        est = ref_val * target_mult / ref_mult
        if best is None or pairs.__len__() > best["ref_n"]:
            best = {"value": round(est, 2), "ref_bucket": label, "ref_value": ref_val,
                    "ref_n": len(pairs), "multiplier": round(target_mult / ref_mult, 3)}
    return best


# ---------------------------------------------------------------- 模块三：接口
def build_router(ctx, fastapi):
    APIRouter = fastapi.APIRouter
    Form = fastapi.Form
    HTTPException = fastapi.HTTPException
    router = APIRouter()

    @router.post("/api/cards/{card_id}/verify")
    def verify_card(card_id: str, price_lock_floor: float = Form(-1.0),
                    note: str = Form("")):
        """人工确认无误：打 is_verified=1；floor>=0 时更新保护下限。

        不改变 est_raw_price（如需改价请走「采纳」）。
        """
        conn = ctx.conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="卡片不存在")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            floor = float(_get(row, "price_lock_floor") or 0)
            if price_lock_floor is not None and price_lock_floor >= 0:
                floor = float(price_lock_floor)
            conn.execute("UPDATE cards SET is_verified=1, price_lock_floor=?, verified_at=?,"
                         " low_confidence=0, updated_at=? WHERE card_id=?",
                         (floor, now, now, card_id))
            conn.execute(
                "INSERT INTO valuation_feedback_log (card_id, field, old_value, new_value,"
                " radar_value, final_value, source) VALUES (?,?,?,?,?,?,?)",
                (card_id, "verify", "", str(floor),
                 float(_get(row, "est_raw_price") or 0),
                 float(_get(row, "est_raw_price") or 0), "verify"))
            conn.commit()
        finally:
            conn.close()
        return {"status": "success", "card_id": card_id, "is_verified": 1,
                "price_lock_floor": floor, "note": note}

    @router.post("/api/maintenance/learn-matrix")
    def api_learn_matrix():
        conn = ctx.conn()
        try:
            return {"status": "success", **learn_matrix(conn)}
        finally:
            conn.close()

    @router.post("/api/maintenance/aggregate-noise")
    def api_aggregate_noise(min_hits: int = 20, min_cards: int = 3):
        conn = ctx.conn()
        try:
            return {"status": "success", **aggregate_noise(conn, min_hits, min_cards)}
        finally:
            conn.close()

    @router.get("/api/pricing/sku-bucket")
    def api_sku_bucket(card_id: str = ""):
        conn = ctx.conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="卡片不存在")
            return {"status": "success", "card_id": card_id,
                    "bucket": list(bucket_key(row)),
                    "denominator": _denom_of(row),
                    "grade": _grade_bucket(row)}
        finally:
            conn.close()

    @router.get("/api/pricing/source-map")
    def api_source_map():
        """给前端用的「判定来源」映射：card_id → {label, note, verified}。"""
        conn = ctx.conn()
        try:
            rows = conn.execute(
                "SELECT card_id, valuation_note, valuation_status, is_verified,"
                "       price_lock_floor FROM cards WHERE COALESCE(status,'')!='已售出'"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        out = {}
        for r in rows:
            note = str(_get(r, "valuation_note") or "")
            out[str(_get(r, "card_id"))] = {
                "label": source_label(note, str(_get(r, "valuation_status") or "")),
                "note": note[:120],
                "verified": int(_get(r, "is_verified") or 0),
                "floor": float(_get(r, "price_lock_floor") or 0),
            }
        return {"status": "success", "count": len(out), "items": out}

    @router.post("/api/maintenance/bind-sales")
    def api_bind_sales(limit: int = 20000):
        conn = ctx.conn()
        try:
            return {"status": "success", **bind_sales_card_ids(conn, limit)}
        finally:
            conn.close()

    @router.get("/api/maintenance/status")
    def api_maintenance_status():
        conn = ctx.conn()
        try:
            st = _state_get(conn, "last_weekly_run")
            return {"status": "success", "last_weekly_run": st,
                    "matrix_rows": conn.execute(
                        "SELECT COUNT(*) FROM market_multiplier_matrix").fetchone()[0],
                    "noise_words": conn.execute(
                        "SELECT COUNT(*) FROM noise_blacklist WHERE active=1").fetchone()[0]}
        finally:
            conn.close()

    return router


NOISE_HINTS = ("卡砖", "空盒", "卡膜", "代购定金", "运费", "链接", "定金", "包邮",
               "保护壳", "卡册", "空包", "众筹", "团购", "求购", "回收", "拼单",
               "预定", "押金", "补差价", "无卡", "仅包装", "包装盒", "壳", "贴纸",
               "海报", "画册", "手办", "模型", "球衣", "签名照")


def aggregate_noise(conn, min_hits=5, min_cards=2):
    """从 anchor_reject_log 聚合高频噪音词 → noise_blacklist。"""
    try:
        rows = conn.execute(
            "SELECT title, card_id FROM anchor_reject_log").fetchall()
    except sqlite3.Error:
        return {"added": 0, "scanned": 0}
    stat = {}
    for r in rows:
        title = str(_get(r, "title") or "")
        cid = str(_get(r, "card_id") or "")
        for word in NOISE_HINTS:
            if word in title:
                s = stat.setdefault(word, {"hits": 0, "cards": set()})
                s["hits"] += 1
                s["cards"].add(cid)
    added = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for word, s in stat.items():
        if s["hits"] >= min_hits and len(s["cards"]) >= min_cards:
            conn.execute(
                "INSERT INTO noise_blacklist (word, reason, hit_count, active, updated_at)"
                " VALUES (?,?,?,1,?) ON CONFLICT(word) DO UPDATE SET"
                " hit_count=excluded.hit_count, updated_at=excluded.updated_at",
                (word, "anchor_reject_auto", s["hits"], now))
            added += 1
    conn.commit()
    return {"added": added, "scanned": len(rows)}


# ---------------------------------------------------------------- 估值管线
def _confidence_of(n, source):
    if source == "sku_bucket":
        return "high" if n >= MIN_BUCKET_N else "medium"
    if source in ("matrix_infer", "manual_anchor"):
        return "low"
    return "medium"


def process_samples(conn, card, raw_sales, case_tokens):
    """模块一主流程：锚点 → 硬边界 → IQR → 分桶 → 加权中位数。"""
    est = float(_get(card, "est_raw_price") or 0)
    rejects = []
    rejected_titles = []   # 存原始标题，供周更黑名单提炼噪音词
    kept = []
    for s in raw_sales or []:
        ok, reason = anchor_check(card, s, case_tokens)
        if not ok:
            rejects.append(reason)
            rejected_titles.append((reason, str(s.get("title") or "")[:80]))
            continue
        price = float(s.get("price") or 0)
        if price <= 0:
            continue
        if est > 0 and not (est * LOW_PRICE_RATIO <= price <= est * HIGH_PRICE_RATIO):
            rejects.append("price_out_of_band")
            continue
        kept.append(s)

    # 每个原因记录最多 3 条真实标题：只存"被剔除 N 条"这种汇总，
    # 周更黑名单就永远提炼不出噪音词（上一版就是这样，noise_blacklist 一直是 0）。
    cid_str = str(_get(card, "card_id") or "")
    per_reason = {}
    for reason, title in rejected_titles:
        lst = per_reason.setdefault(reason, [])
        if title and title not in lst and len(lst) < 3:
            lst.append(title)
    for reason, titles in per_reason.items():
        try:
            for title in titles:
                conn.execute(
                    "INSERT INTO anchor_reject_log (card_id, title, reason) VALUES (?,?,?)",
                    (cid_str, title, reason))
            if not titles:
                conn.execute(
                    "INSERT INTO anchor_reject_log (card_id, title, reason) VALUES (?,?,?)",
                    (cid_str, "%d 条（无标题）" % rejects.count(reason), reason))
        except sqlite3.Error:
            pass

    prices = [float(s.get("price") or 0) for s in kept]
    if prices:
        valid = iqr_filter(prices)
        allowed = set(valid)
        kept = [s for s in kept if float(s.get("price") or 0) in allowed]

    pairs = [(float(s.get("price") or 0), age_weight(_sale_age_days(s.get("sold_date"))))
             for s in kept]
    n = len(pairs)
    if n >= MIN_BUCKET_N:
        return {"value": round(weighted_median(pairs), 2), "sample": n,
                "source": "sku_bucket", "confidence": "high",
                "note": "同款实盘加权中位数（30/90/更早 = 1.0/0.6/0.3，n=%d）" % n,
                "samples": kept, "rejects": len(rejects)}

    infer = matrix_infer(conn, card)
    if infer and infer["value"] > 0:
        return {"value": infer["value"], "sample": n, "source": "matrix_infer",
                "confidence": "low",
                "note": "同桶样本 %d <3，按同系列阶梯推演（参照 %s ¥%.0f × %.2f）"
                        % (n, infer["ref_bucket"], infer["ref_value"], infer["multiplier"]),
                "samples": kept, "rejects": len(rejects)}
    return None


def wrap_refresh_card_value(ctx, orig, ns):
    """替换估值出口：过滤样本 + 重算，并施加锁定保护。

    返回结构保持与 main.py 原函数一致（value/sample/source/confidence/note/samples…），
    这样 revalue_card 不需要改一行。
    """
    def _refresh_card_value(conn, card_id, player_hint, keyword, raw_sales, tokens,
                            parallel="", card_type="", notes="", year_set="", serial="",
                            grader="", grade_score=0, is_auto=None):
        td = orig(conn, card_id, player_hint, keyword, raw_sales, tokens,
                  parallel=parallel, card_type=card_type, notes=notes, year_set=year_set,
                  serial=serial, grader=grader, grade_score=grade_score, is_auto=is_auto)
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None or not raw_sales:
                return td
            case_tokens = _case_tokens(conn)
            result = process_samples(conn, row, raw_sales, case_tokens)
            if not result:
                # 铁律：同桶样本 <3 且无法推演 → **保留人工锚点**。
                # 绝不能把 main.py 旧口径的「单条最近成交」放回去——那正是
                # 贾巴尔 ¥5500 被一条 ¥29、弗拉格 ¥1050 被一条 ¥25 覆盖的原因。
                logger.info("[同桶不足] %s 无可用同款样本，保留人工锚定估值", card_id)
                return {**td, "value": 0, "sample": 0, "earliest": "",
                        "source": "insufficient_bucket", "confidence": "low",
                        "note": "严格同桶有效样本 <3 且无法推演，保留人工锚定估值",
                        "samples": []}

            est = float(_get(row, "est_raw_price") or 0)
            verified = int(_get(row, "is_verified") or 0) == 1
            floor = float(_get(row, "price_lock_floor") or 0)
            value = float(result["value"])

            # 保护下限：已核验 + 有下限 + 新估值低于下限 → 不覆盖
            if verified and floor > 0 and value < floor:
                logger.info("[锁定保护] %s 新估值 ¥%.0f < 保护下限 ¥%.0f → 不覆盖",
                            card_id, value, floor)
                return {**td, "value": 0, "sample": result["sample"],
                        "source": "lock_floor_blocked", "confidence": "low",
                        "note": "自动估值 ¥%.0f 低于人工保护下限 ¥%.0f，已拦截" % (value, floor),
                        "samples": result.get("samples", [])[:20]}

            # 低置信度保护：样本<2 且波动 >±40% → 不覆盖
            if result["sample"] < 2 and est > 0 and abs(value - est) / est > LOW_CONF_SWING:
                logger.info("[低置信度] %s 样本 %d，波动 %.0f%% → 标低置信度不覆盖",
                            card_id, result["sample"], abs(value - est) / est * 100)
                try:
                    conn.execute("UPDATE cards SET low_confidence=1, valuation_status='low_confidence',"
                                 " valuation_note=? WHERE card_id=?",
                                 (result["note"], card_id))
                    conn.commit()
                except sqlite3.Error:
                    pass
                return {**td, "value": 0, "sample": result["sample"],
                        "source": "low_confidence", "confidence": "low",
                        "note": result["note"], "samples": result.get("samples", [])[:20]}

            return {**td,
                    "value": value,
                    "sample": result["sample"],
                    "source": result["source"],
                    "confidence": result["confidence"],
                    "note": result["note"],
                    "samples": [{"title": s.get("title") or "",
                                 "price": float(s.get("price") or 0),
                                 "closed_str": str(s.get("sold_date") or "")}
                                for s in (result.get("samples") or [])[:20]]}
        except Exception as exc:
            logger.warning("定价引擎异常，回退原逻辑：%s", exc)
            return td

    _refresh_card_value._pricing_v1 = True
    return _refresh_card_value


def _case_tokens(conn):
    try:
        return [str(r[0]).lower() for r in conn.execute(
            "SELECT token FROM case_hit_tokens WHERE weight > 0")]
    except sqlite3.Error:
        return ["all kings", "solus", "alter ego", "moment in time", "white sparkle"]


# ---------------------------------------------------------------- 导出 + 安装
def export_for_radar(ctx):
    """把特卡词表与噪音黑名单导出给雷达（它读不到 DB）。"""
    out = {}
    conn = ctx.conn()
    try:
        tokens = [{"token": r[0], "weight": r[1], "is_ssp": r[2]}
                  for r in conn.execute("SELECT token, weight, is_ssp FROM case_hit_tokens")]
        noise = [r[0] for r in conn.execute("SELECT word FROM noise_blacklist WHERE active=1")]
    except sqlite3.Error:
        tokens, noise = [], []
    finally:
        conn.close()
    folder = getattr(ctx, "radar_ctl", None)
    if not folder:
        return {}
    os.makedirs(folder, exist_ok=True)
    for name, payload in (("case_hit_tokens.json", {"tokens": tokens}),
                          ("noise_blacklist.json", {"words": noise})):
        path = os.path.join(folder, name)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        out[name] = len(tokens) if "case_hit" in name else len(noise)
    return out


SOURCE_LABELS = (
    ("同款实盘加权中位数", "实盘中位数"),
    ("阶梯推演", "阶梯推演"),
    ("保留人工锚定", "人工锚定"),
    ("保护下限", "锁定保护"),
    ("最近同款成交", "最近同款"),
)


def source_label(note, status=""):
    note = note or ""
    for key, label in SOURCE_LABELS:
        if key in note:
            return label
    if status == "insufficient":
        return "样本不足"
    if status == "low_confidence":
        return "低置信度"
    return "—"


def _state_get(conn, key):
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS maintenance_state ("
                     "key TEXT PRIMARY KEY, value TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        row = conn.execute("SELECT value FROM maintenance_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def _state_set(conn, key, value):
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS maintenance_state ("
                     "key TEXT PRIMARY KEY, value TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        conn.execute("INSERT INTO maintenance_state (key, value, updated_at) VALUES (?,?,?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                     " updated_at=excluded.updated_at",
                     (key, str(value), time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.Error:
        pass


def run_weekly(ctx, force=False):
    """周更：自动绑定 → 学矩阵 → 聚合黑名单。应用内自调度用。"""
    conn = ctx.conn()
    try:
        last = _state_get(conn, "last_weekly_run")
        if last and not force:
            try:
                age = (time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))) / 86400
                if age < 7:
                    return {"skipped": True, "last": last, "age_days": round(age, 1)}
            except (ValueError, OverflowError):
                pass
        bind = bind_sales_card_ids(conn)
        mat = learn_matrix(conn)
        noise = aggregate_noise(conn)
        _state_set(conn, "last_weekly_run", time.strftime("%Y-%m-%d %H:%M:%S"))
        out = {"skipped": False, "bind": bind, "matrix": mat, "noise": noise}
        logger.info("周更完成：%s", out)
        return out
    finally:
        conn.close()


def start_weekly_scheduler(ctx, interval_sec=1800):
    """轻量自调度：每 30 分钟醒一次，距上次 >7 天就跑周更。

    不依赖 APScheduler，也不需要在群晖里配 cron；DSM 任务计划仍可用作双保险。
    """
    import threading

    def loop():
        while True:
            try:
                run_weekly(ctx)
            except Exception as exc:
                logger.warning("周更调度异常（下次再试）：%s", exc)
            time.sleep(interval_sec)

    t = threading.Thread(target=loop, name="pricing-weekly", daemon=True)
    t.start()
    return {"started": True, "interval_sec": interval_sec}


def install(ns, ctx, fastapi=None):
    """挂载到 main.py 命名空间。幂等，不抛异常。"""
    report = {"version": "v1", "steps": {}}
    try:
        orig = ns.get("_refresh_card_value")
        if orig is None:
            report["steps"]["refresh"] = "missing"
        elif getattr(orig, "_pricing_v1", False):
            report["steps"]["refresh"] = "already"
        else:
            ns["_refresh_card_value"] = wrap_refresh_card_value(ctx, orig, ns)
            report["steps"]["refresh"] = "installed"

        app = ns.get("app")
        if app is not None:
            existing = {getattr(r, "path", "") for r in app.routes}
            if "/api/cards/{card_id}/verify" in existing:
                report["steps"]["routes"] = "already"
            else:
                if fastapi is None:
                    import fastapi as fastapi  # noqa: F811
                app.include_router(build_router(ctx, fastapi))
                report["steps"]["routes"] = "installed"

        report["steps"]["export"] = export_for_radar(ctx)
        if os.getenv("PRICING_WEEKLY_SCHEDULER", "1") != "0":
            try:
                report["steps"]["weekly_scheduler"] = start_weekly_scheduler(ctx)
            except Exception as exc:
                report["steps"]["weekly_scheduler"] = {"error": str(exc)}
        report["status"] = "ok"
        logger.info("定价引擎 v1 已挂载：%s", report["steps"])
        return report
    except Exception as exc:
        logger.warning("定价引擎 v1 挂载失败（回退原有逻辑）：%s", exc)
        report["status"] = "error"
        report["error"] = str(exc)
        return report
