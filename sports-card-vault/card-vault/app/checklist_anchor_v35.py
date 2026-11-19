# -*- coding: utf-8 -*-
"""Checklist 真值锚定 v3.5 —— Card Vault 生产环境的**外挂模块**。

为什么要做成外挂模块而不是直接改 main.py
----------------------------------------
线上 main.py 有 6300+ 行，直接改十几处函数，任何一处 anchor 对不上就是线上 500，
而且回滚要靠 diff 还原。做成外挂模块后：

    main.py 末尾只多 6 行  →  import 本模块 + 调 attach(globals())
    全部逻辑在本文件      →  出问题删掉本文件、还原 main.py 那 6 行即可

attach() 做四件事：
  1. 补齐 checklists 建表 / 两列标准词 / 索引，cards 补 tier_key（幂等 ALTER）
  2. 给 checklists 真值表加三个接口：seed-json（幂等入库）/ lookup（查锚点）/ tier 回写
  3. 包一层 build_search_query：命中真值表就用人工校准词，否则走原正则
  4. 包一层 revalue_card：重估前先用真值表把 search_query / parallel 补准
  5. 把真值快照导出给雷达容器（单向文件交换，与 vault_inventory 同套路）

铁律：任何一步失败都只打印告警，绝不让服务起不来。
"""
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata

logger = logging.getLogger("checklist_anchor_v35")

MARK_VERSION = "v35"

# checklists 表的后加列（老库增量补，绝不重建表）
CHECKLIST_COLUMNS_EXTRA = {
    "canonical_query_cn": "TEXT DEFAULT ''",
    "canonical_query_en": "TEXT DEFAULT ''",
}

# 雷达与 market_base 在用的档位键（p_250_399 雷达在用，market_base 暂未收录）
TIER_KEYS = {
    "p_1_10", "s_1_10", "p_15_25", "s_15_25", "p_35_50", "s_35_50",
    "p_75_99", "s_75_99", "p_125_199", "s_125_199", "p_250_399", "s_250_399",
    "base_auto", "case_hit",
    # 2026-11 用户核对后引入的两个语义档位：无编平行 / 纯 base
    "base", "parallel_unnumbered",
}

CHECKLIST_DDL = """
    CREATE TABLE IF NOT EXISTS checklists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT,
        year TEXT,
        brand TEXT,
        series TEXT,
        card_number TEXT,
        player_en TEXT,
        player_cn TEXT,
        team TEXT,
        subset TEXT,
        is_rookie INTEGER DEFAULT 0,
        is_auto INTEGER DEFAULT 0,
        is_relic INTEGER DEFAULT 0,
        serial_max INTEGER,
        parallels TEXT,
        source TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

SEED_COLUMNS = (
    "sport", "year", "brand", "series", "card_number", "player_en", "player_cn",
    "team", "subset", "is_rookie", "is_auto", "is_relic", "serial_max",
    "parallels", "source", "notes", "canonical_query_cn", "canonical_query_en",
)

_DENOM_RE = re.compile(r"/\s*(\d{1,5})")
_NORM_RE = re.compile(r"[\s\-_·•・.,，、/|()（）\[\]【】]+")


# ---------------------------------------------------------------- 基础设施
class Ctx:
    """把 main.py 里的连接方式 / 路径 / app 收集起来，避免到处传参。"""

    def __init__(self, ns):
        self.ns = ns
        self.db_path = ns.get("DB_PATH") or ""
        self.data_dir = ns.get("DATA_DIR") or os.path.dirname(self.db_path)
        self.radar_ctl = ns.get("RADAR_CTL_DIR") or os.path.join(self.data_dir, "radar_ctl")
        self.truth_path = os.path.join(self.radar_ctl, "checklists_truth.json")
        self.vault_inventory = ns.get("VAULT_INVENTORY") or os.path.join(
            self.radar_ctl, "vault_inventory.json")

    def conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn


def _get(row, key, default=""):
    if row is None:
        return default
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(key, default) if isinstance(row, dict) else default
    return row[key] if key in keys else default


def _norm(text):
    """归一化：折叠变音符（Dončić→Doncic）+ 去分隔符 + 小写。

    cards.player 里存的是 `卢卡·东契奇 (Luka Dončić)` 这种带变音符的写法，
    checklists.player_en 是 `Luka Doncic`——不做 Unicode 折叠永远对不上。
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _NORM_RE.sub("", s).lower()


def _player_aliases(player):
    """把 `中文名 (English Name)` 拆成一堆可比对的写法。

    cards.player 的实际存量写法：
        卢卡·东契奇 (Luka Dončić)
        托尼·克罗斯 (Toni Kroos)
        Giulio Zeppieri          ← 只有英文
    而 checklists 是 player_cn / player_en 分列。所以要同时产出：
        整串 / 括号外中文 / 中文姓氏 / 括号内英文 / 英文姓氏
    任何一条能命中就算球员对上。
    """
    text = str(player or "").strip()
    if not text:
        return []
    out = []

    def push(value, minlen=2):
        key = _norm(value)
        if len(key) >= minlen and key not in out:
            out.append(key)

    m = re.search(r"[（(]([^）)]+)[）)]", text)
    chinese = re.sub(r"[（(][^）)]*[）)]", "", text).strip() or text
    english = m.group(1).strip() if m else ""

    push(chinese)
    for sep in ("·", "•", "・"):
        if sep in chinese:
            push(chinese.split(sep)[-1])
    if english:
        push(english, 3)
        parts = english.split()
        if len(parts) > 1:
            push(parts[-1], 4)
    push(text, 3)
    return out


def _serial_token(serial):
    """编数分母：返回 '1/1' / '/99' / ''（四位数年份不算限量）。"""
    m = _DENOM_RE.search(str(serial or ""))
    if not m:
        return ""
    try:
        den = int(m.group(1))
    except (TypeError, ValueError):
        return ""
    if den <= 1:
        return "1/1"
    if 1900 <= den <= 2099:
        return ""
    return "/%d" % den


# ---------------------------------------------------------------- 1. 建表
def ensure_schema(ctx):
    """补齐 checklists 表/列/索引与 cards.tier_key。幂等，失败只告警。"""
    report = {}
    conn = ctx.conn()
    try:
        cur = conn.cursor()
        cur.execute(CHECKLIST_DDL)
        cur.execute("PRAGMA table_info(checklists)")
        have = {row[1] for row in cur.fetchall()}
        added = []
        for col, decl in CHECKLIST_COLUMNS_EXTRA.items():
            if col not in have:
                cur.execute("ALTER TABLE checklists ADD COLUMN %s %s" % (col, decl))
                added.append(col)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_checklist_search "
                    "ON checklists(series, player_en, player_cn, card_number)")
        # 非唯一索引：线上老数据存在 series+card_number+player_en 重复行，
        # 建 UNIQUE 会让服务直接起不来。幂等去重在 seed-json 里做。
        cur.execute("CREATE INDEX IF NOT EXISTS idx_checklist_anchor "
                    "ON checklists(series, card_number, player_en)")

        # cards.tier_key（老库没有就补）
        cur.execute("PRAGMA table_info(cards)")
        card_cols = {row[1] for row in cur.fetchall()}
        if "tier_key" not in card_cols:
            cur.execute("ALTER TABLE cards ADD COLUMN tier_key TEXT DEFAULT ''")
            report["cards_tier_key_added"] = True
        conn.commit()
        report["checklist_columns_added"] = added
    finally:
        conn.close()
    return report


# ---------------------------------------------------------------- 2. 真值查询
def _player_sql_terms(player):
    """给 SQL 预筛用的**原文**片段（不能折变音符，LIKE 要对着库里的原值打）。

    `卢卡·东契奇 (Luka Dončić)` → ['卢卡·东契奇', '东契奇', 'Luka Dončić', 'Dončić']
    """
    text = str(player or "").strip()
    if not text:
        return []
    terms = []

    def push(value):
        v = str(value or "").strip()
        if len(v) >= 2 and v not in terms:
            terms.append(v)

    m = re.search(r"[（(]([^）)]+)[）)]", text)
    chinese = re.sub(r"[（(][^）)]*[）)]", "", text).strip() or text
    english = m.group(1).strip() if m else ""
    push(chinese)
    for sep in ("·", "•", "・"):
        if sep in chinese:
            push(chinese.split(sep)[-1])
    if english:
        push(english)
        parts = english.split()
        if len(parts) > 1:
            push(parts[-1])
    return terms


def lookup_anchor(ctx, player="", card_number="", series=""):
    """在 checklists 里找这张卡的锚点。打分 + **球员硬否决**，规则同本地实现。"""
    player = (player or "").strip()
    card_number = str(card_number or "").strip()
    series = (series or "").strip()
    if not player and not card_number:
        return None

    conn = ctx.conn()
    try:
        # 预筛要同时兜住「中英合体写法」：用原文片段分别 LIKE，而不是拿整串去匹配
        terms = _player_sql_terms(player)
        clauses, params = [], []
        if card_number:
            clauses.append("TRIM(card_number) = ?")
            params.append(card_number)
        for term in terms:
            clauses.append("(LOWER(player_en) LIKE LOWER(?) OR LOWER(player_cn) LIKE LOWER(?))")
            params.extend(["%%%s%%" % term, "%%%s%%" % term])
        if series:
            # 必须先排除空系列行：否则 `LOWER(?) LIKE '%' || '' || '%'` = LIKE '%%'
            # 会把 463 条无系列行全部命中，60 行配额被吃光，真正的行反而被 LIMIT 截断。
            clauses.append("(TRIM(COALESCE(series,'')) <> '' AND "
                           "(LOWER(series) LIKE LOWER(?) OR LOWER(?) LIKE '%' || LOWER(series) || '%'))")
            params.extend(["%%%s%%" % series, "%%%s%%" % series])
        if not clauses:
            return None
        rows = conn.execute(
            "SELECT * FROM checklists WHERE " + " OR ".join(clauses) +
            " ORDER BY CASE WHEN TRIM(card_number) = ? THEN 0 ELSE 1 END, id ASC "
            "LIMIT 3000", params + [card_number]).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    aliases = _player_aliases(player)
    ys_norm = _norm(series)

    best, best_score = None, 0
    for row in rows or []:
        row_num = str(_get(row, "card_number") or "").strip()
        num_hit = bool(card_number and row_num == card_number)

        row_player_norm = _norm("%s %s" % (_get(row, "player_en"), _get(row, "player_cn")))
        player_hit = bool(aliases) and any(a in row_player_norm for a in aliases)

        # 硬否决：给了球员、行里也有球员名，就是比不上 → 整行作废
        if player and row_player_norm and not player_hit:
            continue

        score = (3 if num_hit else 0) + (2 if player_hit else 0)
        row_series = _norm(_get(row, "series"))
        if ys_norm and row_series and (row_series in ys_norm or ys_norm in row_series):
            score += 1
        if score > best_score:
            best, best_score = row, score
    return best if best_score >= 2 else None


def canonical_query(row, lang="cn"):
    """优先人工校准的 canonical_query_*，没有就按结构化列拼（不解析自由文本）。"""
    if row is None:
        return ""
    key = "canonical_query_en" if lang == "en" else "canonical_query_cn"
    exact = str(_get(row, key) or "").strip()
    if exact:
        return exact
    if lang == "en":
        parts = [_get(row, "year"), _get(row, "series"), _get(row, "player_en"),
                 _get(row, "card_number")]
    else:
        parts = [_get(row, "year"), _get(row, "series"),
                 _get(row, "player_cn") or _get(row, "player_en"), _get(row, "card_number")]
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def parallel_hint(row, serial=""):
    """按编数在平行阶梯里对号入座；撞多条返回空（不猜）。"""
    raw = _get(row, "parallels")
    if not raw:
        return ""
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return ""
    token = _serial_token(serial)
    if not token or not items:
        return ""
    hits = [str(x) for x in items if token in str(x)]
    if len(hits) != 1:
        return ""
    label = hits[0]
    m = re.search(r"[（(]([^）)]+)[）)]", label)
    return (m.group(1) if m else label).strip()


# ---------------------------------------------------------------- 3. 真值快照
def export_truth(ctx):
    """把 checklists 导出给雷达（单向文件交换，和 vault_inventory 同套路）。"""
    conn = ctx.conn()
    try:
        try:
            rows = conn.execute(
                "SELECT player_en, player_cn, card_number, series, year, brand, sport,"
                " subset, is_auto, is_rookie, serial_max, parallels, canonical_query_cn,"
                " canonical_query_en FROM checklists").fetchall()
        except sqlite3.Error:
            rows = []
    finally:
        conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["parallels"] = item.get("parallels") or "[]"
        item["canonical_query_cn"] = item.get("canonical_query_cn") or ""
        item["canonical_query_en"] = item.get("canonical_query_en") or ""
        items.append(item)

    payload = {"exported_ts": int(time.time()), "count": len(items), "items": items}
    os.makedirs(os.path.dirname(ctx.truth_path), exist_ok=True)
    tmp = "%s.tmp-%d" % (ctx.truth_path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, ctx.truth_path)
    return len(items)


# ---------------------------------------------------------------- 4. 接口
def build_router(ctx, fastapi):
    """构造 /api/checklists/seed-json、/lookup 与 /api/cards/{id}/tier 三个接口。"""
    APIRouter = fastapi.APIRouter
    Form = fastapi.Form
    Request = fastapi.Request
    HTTPException = fastapi.HTTPException
    router = APIRouter()

    @router.post("/api/checklists/seed-json")
    async def seed_checklists_json(request: Request):
        """粘贴 JSON 批量入库真值表，按 series+card_number+player_en 幂等去重。"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = body.get("items")
            if items is None:
                items = body.get("cards")
            if isinstance(items, dict):
                items = [items]
        else:
            items = None
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="items 必须是数组")

        conn = ctx.conn()
        inserted = skipped = updated = 0
        try:
            for it in items:
                if not isinstance(it, dict):
                    skipped += 1
                    continue
                series = str(it.get("series") or "").strip()
                card_number = str(it.get("card_number") or "").strip()
                player_en = str(it.get("player_en") or "").strip()
                player_cn = str(it.get("player_cn") or "").strip()
                if not series or not (card_number or player_en or player_cn):
                    skipped += 1
                    continue

                parallels = it.get("parallels")
                if parallels is None:
                    parallels_json = "[]"
                elif isinstance(parallels, str):
                    parallels_json = parallels
                else:
                    parallels_json = json.dumps(parallels, ensure_ascii=False)

                exists = conn.execute(
                    "SELECT id FROM checklists WHERE series = ? AND card_number = ? AND player_en = ?",
                    (series, card_number, player_en)).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO checklists (%s) VALUES (%s)"
                        % (", ".join(SEED_COLUMNS), ", ".join("?" * len(SEED_COLUMNS))),
                        (
                            str(it.get("sport") or "").strip(),
                            str(it.get("year") or "").strip(),
                            str(it.get("brand") or "").strip(),
                            series, card_number, player_en, player_cn,
                            str(it.get("team") or "").strip(),
                            str(it.get("subset") or "Base").strip(),
                            1 if it.get("is_rookie") else 0,
                            1 if it.get("is_auto") else 0,
                            1 if it.get("is_relic") else 0,
                            it.get("serial_max"),
                            parallels_json,
                            str(it.get("source") or "manual_json").strip(),
                            str(it.get("notes") or "").strip(),
                            str(it.get("canonical_query_cn") or "").strip(),
                            str(it.get("canonical_query_en") or "").strip(),
                        ))
                    inserted += 1
                    continue

                q_cn = str(it.get("canonical_query_cn") or "").strip()
                q_en = str(it.get("canonical_query_en") or "").strip()
                if q_cn or q_en:
                    conn.execute(
                        "UPDATE checklists SET "
                        "canonical_query_cn = CASE WHEN ? != '' THEN ? ELSE canonical_query_cn END, "
                        "canonical_query_en = CASE WHEN ? != '' THEN ? ELSE canonical_query_en END "
                        "WHERE id = ?",
                        (q_cn, q_cn, q_en, q_en, exists["id"]))
                    updated += 1
                skipped += 1
            conn.commit()
        finally:
            conn.close()

        if inserted or updated:
            try:
                export_truth(ctx)
            except Exception as exc:
                logger.warning("真值快照导出失败：%s", exc)

        return {"status": "success", "received": len(items),
                "inserted": inserted, "skipped": skipped, "updated": updated}

    @router.get("/api/checklists/lookup")
    def lookup_checklist(player: str = "", card_number: str = "", series: str = ""):
        row = lookup_anchor(ctx, player=player, card_number=card_number, series=series)
        if row is None:
            return {"status": "success", "matched": False, "record": None}
        record = dict(row)
        record["canonical_query_cn"] = canonical_query(row, "cn")
        record["canonical_query_en"] = canonical_query(row, "en")
        return {"status": "success", "matched": True, "record": record}

    @router.post("/api/cards/{card_id}/tier")
    def set_card_tier(card_id: str,
                      tier_key: str = Form(""), tier_desc: str = Form(""),
                      tier_basis: str = Form(""), parallel: str = Form(""),
                      card_number: str = Form(""), is_auto: str = Form("")):
        """窄接口：只更新档位四列（雷达判档后回写用），绝不整行 UPDATE。"""
        key = str(tier_key or "").strip()
        if key and key not in TIER_KEYS:
            raise HTTPException(status_code=400,
                                detail="未知档位键 %r" % key)

        conn = ctx.conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="卡片不存在")

            sets, params, changed = [], [], []

            def consider(col, new_val):
                text = "" if new_val is None else str(new_val).strip()
                if not text:
                    return
                old = "" if _get(row, col) is None else str(_get(row, col)).strip()
                if old == text:
                    return
                sets.append("%s = ?" % col)
                params.append(text)
                changed.append(col)

            consider("tier_key", key)
            consider("parallel", parallel)
            consider("card_number", str(card_number or "").strip().lstrip("#"))
            if str(is_auto).strip() != "":
                try:
                    auto_val = 1 if int(str(is_auto).strip()) else 0
                except (TypeError, ValueError):
                    auto_val = None
                if auto_val is not None and int(_get(row, "is_auto") or 0) != auto_val:
                    sets.append("is_auto = ?")
                    params.append(auto_val)
                    changed.append("is_auto")

            if sets:
                # updated_at 是后加的列：极端老的库可能还没有，别因此 500
                if "updated_at" in row.keys():
                    params.extend([time.strftime("%Y-%m-%d %H:%M:%S"), card_id])
                    conn.execute(
                        "UPDATE cards SET %s, updated_at = ? WHERE card_id = ?" % ", ".join(sets),
                        params)
                else:
                    params.append(card_id)
                    conn.execute(
                        "UPDATE cards SET %s WHERE card_id = ?" % ", ".join(sets), params)
                conn.commit()
            out = dict(conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone())
        finally:
            conn.close()

        if changed:
            try:
                exporter = ctx.ns.get("_export_vault_inventory")
                if callable(exporter):
                    exporter()
            except Exception:
                pass
        return {"status": "success", "card_id": card_id, "changed": changed,
                "tier_desc": str(tier_desc or ""), "tier_basis": str(tier_basis or ""),
                "card": {"card_id": out.get("card_id"),
                         "tier_key": out.get("tier_key") or "",
                         "parallel": out.get("parallel") or "",
                         "card_number": out.get("card_number") or "",
                         "is_auto": out.get("is_auto") or 0}}

    return router


# ---------------------------------------------------------------- 5. 包装
PARALLEL_CN = {
    "refractor": "银折", "silver": "银折", "gold": "金折", "pink": "粉折",
    "blue": "蓝折", "purple": "紫折", "green": "绿折", "red": "红折",
    "orange": "橙折", "black": "黑折", "aqua": "水蓝折", "prizm": "波纹折",
    "sapphire": "蓝宝石折", "platinum": "白金折", "blue_porcelain": "青花瓷折",
}


def _parallel_cn(parallel):
    """把内部版型值翻成卡淘标题里真实出现的中文词。"""
    return PARALLEL_CN.get(str(parallel or "").strip().lower(), "")


def _augment_with_parallel(query, parallel, tier_key):
    """无编折射卡：把中文折射词补进搜索词，并把 "Base" 去掉。

    为什么必须这么做：tier=parallel_unnumbered 的卡（如 2025 Topps Chrome #251
    Base Refractor RC）如果只搜 "Flagg Base Refractor"，卡淘返回的 51 条样本
    全是 Base 普卡，估值直接从 ¥1,544 掉到 ¥25。带上「银折」才是同一张卡。
    """
    q = " ".join(str(query or "").split())
    if not q:
        return q
    if str(tier_key or "").strip() != "parallel_unnumbered":
        return q
    par = str(parallel or "").strip().lower()
    if not par or par in ("base", "serialized"):
        return q
    token = _parallel_cn(par)
    if not token:
        return q
    q = q.replace(" Base ", " ")
    q = " ".join(q.split())
    if token not in q:
        q = (q + " " + token).strip()
    return q


def wrap_build_search_query(ctx, orig):
    """命中真值表就用人工标准词，否则原样调原函数（签名完全兼容）。

    v3.6：无编折射卡（tier=parallel_unnumbered）会把中文折射词（银折/金折…）
    补进搜索词，并把 "Base" 去掉——否则卡淘返回的全是普卡样本。
    """

    def build_search_query(sport="", player="", year_set="", card_type="",
                           serial="", card_number="", conn=None,
                           parallel="", tier_key=""):
        try:
            anchor = lookup_anchor(ctx, player=player, card_number=card_number,
                                   series=year_set)
            if anchor is not None:
                exact = canonical_query(anchor, "cn")
                if exact:
                    denom = _serial_token(serial)
                    if denom and denom not in exact:
                        exact = (exact + " " + denom).strip()
                    if exact:
                        return _augment_with_parallel(exact, parallel, tier_key)
        except Exception as exc:
            logger.debug("真值查词失败，走正则：%s", exc)
        base = orig(sport, player, year_set, card_type, serial)
        return _augment_with_parallel(base, parallel, tier_key)

    build_search_query._cv35_wrapped = True
    build_search_query.__wrapped__ = orig
    return build_search_query


def wrap_revalue_card(ctx, orig):
    """重估前先用真值表把 search_query / parallel 补准，再交给原逻辑。"""

    def prepare(card_id):
        conn = ctx.conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            if row is None:
                return []
            anchor = lookup_anchor(ctx, player=_get(row, "player"),
                                   card_number=_get(row, "card_number"),
                                   series=_get(row, "year_set"))
            if anchor is None:
                return []
            sets, params, changed = [], [], []
            cur_query = str(_get(row, "search_query") or "").strip()
            if not cur_query:
                cur_query = canonical_query(anchor, "cn")
            # v3.6：无编折射卡补中文折射词（银折/金折…），否则样本会掉进普卡池
            want_query = _augment_with_parallel(cur_query, _get(row, "parallel"),
                                                _get(row, "tier_key"))
            if want_query and want_query != str(_get(row, "search_query") or "").strip():
                sets.append("search_query = ?")
                params.append(want_query)
                changed.append("search_query")
            if not str(_get(row, "parallel") or "").strip():
                hint = parallel_hint(anchor, _get(row, "serial"))
                if hint:
                    sets.append("parallel = ?")
                    params.append(hint)
                    changed.append("parallel")
            if sets:
                params.append(card_id)
                conn.execute("UPDATE cards SET %s WHERE card_id = ?" % ", ".join(sets), params)
                conn.commit()
            return changed
        finally:
            conn.close()

    def revalue_card(card_id, apply=True, deep=False):
        try:
            prepare(card_id)
        except Exception as exc:
            logger.debug("真值预处理失败（不影响重估）：%s", exc)
        return orig(card_id, apply=apply, deep=deep)

    revalue_card._cv35_wrapped = True
    revalue_card.__wrapped__ = orig
    return revalue_card


# 版型 → 标题里可能出现的词（中英都收，宁可严一点也不要放普卡进来）
PARALLEL_TITLE_TOKENS = {
    "refractor": ("refractor", "折射", "银折", "x-fractor", "xfractor"),
    "silver": ("silver", "银折", "银"),
    "gold": ("gold", "金折", "金"),
    "pink": ("pink", "粉折", "粉冰", "粉"),
    "blue": ("blue", "蓝折", "蓝"),
    "purple": ("purple", "紫折", "紫"),
    "green": ("green", "绿折", "绿"),
    "red": ("red", "红折", "红"),
    "orange": ("orange", "橙折", "橙"),
    "black": ("black", "黑折", "黑"),
    "aqua": ("aqua", "水蓝"),
    "prizm": ("prizm", "波纹"),
    "sapphire": ("sapphire", "蓝宝石"),
    "platinum": ("platinum", "白金"),
    "blue_porcelain": ("青花瓷", "porcelain"),
    "rr": ("rr", "rate"),
    "masterpiece": ("masterpiece", "杰作"),
    "metal": ("metal", "金属"),
    "sketch": ("sketch", "手绘"),
    "booklet": ("booklet", "小书", "书卡"),
}


def _sales_title_hit_parallel(title, parallel):
    """标题是否真的属于该版次。

    不复用 main.py 的 `_filter_sales_by_parallel`：它 strict=True 只作用于最后一档
    回退，中间的 `parallel_any`（命中任意折射词即放行）仍会把 Base 普卡放进来，
    实测 58 条样本一条都没被剔掉。
    """
    low = str(title or "").lower()
    if not low:
        return False
    for token in PARALLEL_TITLE_TOKENS.get(str(parallel or "").strip().lower(), ()):
        if token and token in low:
            return True
    return False


def _strict_parallel_filter(sales, parallel):
    """只留同版次成交；一条不剩就返回 []（交给下游报样本不足）。"""
    kept = [s for s in (sales or []) if _sales_title_hit_parallel(s.get("title"), parallel)]
    return kept


def wrap_refresh_card_value(ctx, orig, ns):
    """估值前对「无编折射卡」做**严格**平行过滤。

    为什么必须加：main.py 里 `_filter_sales_by_parallel(..., strict=False)` 在
    没有同版成交时会**回退到全样本**，于是 2025 Topps Chrome #251 Base Refractor
    这种卡会拿 58 条 Base 普卡成交估值（¥1,544 → ¥25）。估值路径本身也没调这个
    过滤函数（只有展示路径 strict=True 调过）。

    本包装按用户拍板的口径：
      · tier_key == 'parallel_unnumbered' 且 parallel 有明确版型（非 base）
        → 严格只保留标题含该版次别名的成交；
      · 一条都不剩 → 传空样本下去，让下游如实报「样本不足」，
        绝不把普卡价混进来。
    """
    log = logging.getLogger("checklist_anchor_v35")

    def _refresh_card_value(conn, card_id, player_hint, keyword, raw_sales, tokens,
                            parallel="", card_type="", notes="", year_set="", serial="",
                            grader="", grade_score=0, is_auto=None):
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            tier = str(_get(row, "tier_key") or "").strip()
            par = str(parallel or _get(row, "parallel") or "").strip()
            if (tier == "parallel_unnumbered"
                    and par.lower() not in ("", "base", "serialized")
                    and raw_sales):
                kept = _strict_parallel_filter(list(raw_sales), par)
                if kept:
                    log.info("[平行过滤] %s 版型=%s 保留 %d/%d 条",
                             card_id, par, len(kept), len(raw_sales))
                    raw_sales = kept
                else:
                    log.info("[平行过滤] %s 版型=%s 无同版成交 → 报样本不足"
                             "（剔除 %d 条 Base/异版干扰）", card_id, par, len(raw_sales))
                    raw_sales = []
        except Exception as exc:
            log.debug("平行过滤异常，按原样本继续：%s", exc)
        return orig(conn, card_id, player_hint, keyword, raw_sales, tokens,
                    parallel=parallel, card_type=card_type, notes=notes,
                    year_set=year_set, serial=serial, grader=grader,
                    grade_score=grade_score, is_auto=is_auto)

    _refresh_card_value._cv35_wrapped = True
    return _refresh_card_value


# ---------------------------------------------------------------- 入口
def attach(ns, fastapi=None):
    """把 v3.5 全部能力挂到 main.py 的命名空间上。幂等，绝不抛异常。"""
    report = {"version": MARK_VERSION, "steps": {}}
    try:
        ctx = Ctx(ns)
        if not ctx.db_path:
            report["status"] = "error"
            report["error"] = "命名空间里没有 DB_PATH"
            return report

        # 1. 建表/迁移
        try:
            report["steps"]["schema"] = ensure_schema(ctx)
        except Exception as exc:
            logger.warning("schema 迁移失败：%s", exc)
            report["steps"]["schema"] = {"error": str(exc)}

        # 2. 接口（已注册过就跳过）
        try:
            app = ns.get("app")
            if app is None:
                raise RuntimeError("命名空间里没有 app")
            existing = {getattr(r, "path", "") for r in app.routes}
            wanted = {"/api/checklists/seed-json", "/api/checklists/lookup"}
            if wanted & existing:
                report["steps"]["routes"] = "already"
            else:
                if fastapi is None:
                    import fastapi as fastapi          # noqa: F811
                app.include_router(build_router(ctx, fastapi))
                report["steps"]["routes"] = "installed"
        except Exception as exc:
            logger.warning("接口注册失败：%s", exc)
            report["steps"]["routes"] = {"error": str(exc)}

        # 3. 真值优先查词
        try:
            orig = ns.get("build_search_query")
            if orig is None:
                report["steps"]["search_query"] = "missing"
            elif getattr(orig, "_cv35_wrapped", False):
                report["steps"]["search_query"] = "already"
            else:
                ns["build_search_query"] = wrap_build_search_query(ctx, orig)
                report["steps"]["search_query"] = "installed"
        except Exception as exc:
            logger.warning("build_search_query 包装失败：%s", exc)
            report["steps"]["search_query"] = {"error": str(exc)}

        # 4. 重估前补准字段
        try:
            orig = ns.get("revalue_card")
            if orig is None:
                report["steps"]["revalue_card"] = "missing"
            elif getattr(orig, "_cv35_wrapped", False):
                report["steps"]["revalue_card"] = "already"
            else:
                ns["revalue_card"] = wrap_revalue_card(ctx, orig)
                report["steps"]["revalue_card"] = "installed"
        except Exception as exc:
            logger.warning("revalue_card 包装失败：%s", exc)
            report["steps"]["revalue_card"] = {"error": str(exc)}

        # 4.5 估值出口交给定价引擎 v1（Exact SKU Bucket / 加权中位数 / 矩阵推演 / 锁定保护）
        try:
            import pricing_engine_v1 as _pe
            fastapi_mod = None
            try:
                import fastapi as fastapi_mod  # noqa: F401
            except Exception:
                fastapi_mod = None
            report["steps"]["pricing_engine"] = _pe.install(ns, ctx, fastapi_mod)
        except Exception as exc:
            logger.warning("定价引擎 v1 安装失败，回退 v35 简化过滤：%s", exc)
            try:
                orig_rv = ns.get("_refresh_card_value")
                if orig_rv is not None and not getattr(orig_rv, "_cv35_wrapped", False):
                    ns["_refresh_card_value"] = wrap_refresh_card_value(ctx, orig_rv, ns)
                    report["steps"]["refresh_value"] = "fallback_installed"
            except Exception as exc2:
                report["steps"]["refresh_value"] = {"error": str(exc2)}

        # 5. 真值快照
        try:
            report["steps"]["export_truth"] = export_truth(ctx)
        except Exception as exc:
            logger.warning("真值快照导出失败：%s", exc)
            report["steps"]["export_truth"] = {"error": str(exc)}

        report["status"] = "ok"
        logger.info("checklist 真值锚定 v35 已挂载：%s", report["steps"])
        return report
    except Exception as exc:            # 兜底：绝不让 main.py 起不来
        logger.warning("checklist v35 挂载失败，服务按原逻辑运行：%s", exc)
        report["status"] = "error"
        report["error"] = str(exc)
        return report
