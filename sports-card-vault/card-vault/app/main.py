import base64
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import photos

app = FastAPI(title="Card Vault - 球星卡私人档案库")
logger = logging.getLogger("cardvault")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """未捕获异常一律折叠成 JSON，而不是 Starlette 默认的纯文本。

    前端所有请求都走 res.json()。500 若是纯文本 "Internal Server Error"，
    前端只能抛 "Unexpected token 'I' ... is not valid JSON"，真实故障被彻底遮住——
    线上为此白排查了两轮（aspect KeyError、auto_detect_borders AttributeError）。
    改成 JSON 之后，前端能直接把 detail 显出来。
    """
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务端内部错误：%s: %s" % (type(exc).__name__, exc)},
    )

# 允许的图片扩展名。白名单之外一律按 .jpg 处理，避免用户文件名带来奇怪后缀。
ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/app/uploads")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory="app/templates")

DB_PATH = os.path.join(DATA_DIR, "cards.db")
if not os.path.exists(DB_PATH):
    mock_db = os.path.join(DATA_DIR, "cards.mock.db")
    if os.path.exists(mock_db):
        import shutil
        try:
            shutil.copy(mock_db, DB_PATH)
        except Exception:
            DB_PATH = mock_db

# ---- 与雷达容器交换数据的两个目录（刻意提前定义）----
# 这两个常量原先定义在雷达看板那一段（1000 行之后），但 init_db() 在模块导入时
# 就要把在库清单导出去给雷达，那时还读不到下面的定义。放到最前面避免顺序陷阱。
#
# RADAR_DIR：雷达写、我们**只读**（docker-compose 里是 :ro 挂载）
# RADAR_CTL_DIR：我们写、雷达**只读**（挂在雷达容器里是 /app/data/vault:ro）
# 方向刻意对等 —— 谁都不需要对方的数据写权限。
RADAR_DIR = os.getenv("RADAR_DIR", "/app/data/radar")
RADAR_CTL_DIR = os.path.join(DATA_DIR, "radar_ctl")
os.makedirs(RADAR_CTL_DIR, exist_ok=True)

# 在库询价清单：我们写（给雷达读）
VAULT_INVENTORY = os.path.join(RADAR_CTL_DIR, "vault_inventory.json")
# 手动询价请求：我们写（给雷达读）
VAULT_QUOTE_REQUEST = os.path.join(RADAR_CTL_DIR, "vault_quote_req.json")
# 雷达回传的行情：雷达写（我们只读）
VAULT_QUOTES = os.path.join(RADAR_DIR, "vault_quotes.json")

# 手动询价的冷却秒数。雷达一轮 68 关键词要 4 分钟，库存询价也要半分钟以上，
# 连点会把队列点成雪崩，所以给一个硬冷却。
QUOTE_COOLDOWN_SEC = float(os.getenv("QUOTE_COOLDOWN_SEC", "60"))

# Scrapling 助手服务地址（提供卡淘大模型解析与代理）
SCRAPLING_API_BASE = os.getenv("SCRAPLING_API_BASE", "http://scrapling-crawler:8090")

# 按日热备保留份数
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "10"))


def now_str() -> str:
    """统一用 UTC 存储时间戳，与早期建表时 CURRENT_TIMESTAMP 的语义保持一致；
    前端展示时再换算成浏览器本地时间。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# 轻量迁移表：老库缺哪列补哪列，只做新增，永不删列
MIGRATIONS = {
    "updated_at": "TEXT",
    "photo_mode": "TEXT",
    "rotate_front": "INTEGER DEFAULT 0",
    "rotate_back": "INTEGER DEFAULT 0",
    "img_front_orig": "TEXT",
    "img_back_orig": "TEXT",
    # 库存行情询价用的搜索词。留空时由 build_search_query() 按字段自动拼，
    # 用户可以在编辑弹窗里改成自己觉得更准的写法（卖家常用简称等）。
    "search_query": "TEXT",
}


# ============================ 库存行情：搜索词与清单导出 ============================
# 目的：让雷达容器定期帮在库卡片查一次"同款现在值多少"，结果回传给前端对比。
# 两个子系统之间**只通过文件交换**，谁也拿不到对方的写权限（见文件顶部注释）。

# 发卡**母品牌**。它们几乎出现在每一条标题里，单独留着对召回没有帮助；
# 但系列名（Prizm / Optic / Chrome / Immaculate…）恰好相反——那是卖家最先写的词，
# 绝不能当噪声剔掉。所以这里只列真正的母品牌。
_SERIES_BRANDS = {
    "panini", "topps", "bowman", "upper", "deck", "upperdeck",
    "fleer", "leaf", "score", "skybox", "donruss",
}
# 通用噪声词：运动名、卡片泛称。出现在查询里只会稀释召回。
_SERIES_GENERIC = {
    "baseball", "basketball", "football", "soccer", "hockey", "tennis",
    "trading", "card", "cards", "series",
    "系列", "球星卡", "收藏卡", "卡",
}

# 年份只认 19xx / 20xx —— 编数分母里也常出现四位数（如 /2026），
# 所以年份必须从"年份/系列"字段里找，而不是从编数里找。
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_DENOM_RE = re.compile(r"/\s*(\d{1,5})")


def _series_core(year_set: str) -> str:
    """从"年份+系列"里取系列核心词（最多 2 个）。

    例：`2026 Topps Series 1 Baseball` → `Topps`
        `2019 Panini Prizm`            → `Prizm`
        `Panini Immaculate Collection` → `Immaculate Collection`
        `2022 Panini Prizm World Cup`  → `World Cup`

    规则是"取最后 2 个非母品牌词"：
    - 系列名越靠右越具体（`Panini Prizm World Cup` 里 `World Cup` 才是区分点）；
    - 母品牌只在没有别的词可选时才保留（只写 `Topps` 的卡）。
    查询宁可短。搜索是两个进程之间的弱约定，词越多越可能一条都搜不到。
    """
    text = _YEAR_RE.sub(" ", str(year_set or ""))
    tokens = [t for t in re.split(r"[\s/|,，、\-]+", text) if t]
    tokens = [t for t in tokens if t.lower() not in _SERIES_GENERIC and not t.isdigit()]
    distinctive = [t for t in tokens if t.lower() not in _SERIES_BRANDS]
    if distinctive:
        return " ".join(distinctive[-2:])
    if tokens:
        return tokens[-1]
    return ""


def _player_short(player: str) -> str:
    """取一个卖家真会写的球员名。

    - 括号里有英文：`克里斯蒂亚诺·罗纳尔多 (Cristiano Ronaldo)` → `Ronaldo`
      （卡淘卖家写英文时基本只写姓氏，全名反而搜不到）
    - 括号里是中文：`Carlos Alcaraz (阿尔卡拉斯)` → `阿尔卡拉斯`
      （卡淘绝大多数标题用中文译名，英文名只是备注，拿英文去搜会漏一片）
    - 中文带分隔点：`卢卡·东契奇` → `东契奇`（中文译名的姓氏在最后一段）
    - 其余原样返回
    """
    text = str(player or "").strip()
    if not text:
        return ""
    match = re.search(r"[\(（]([^)）]+)[\)）]", text)
    if match:
        english = match.group(1).strip()
        parts = re.split(r"\s+", english)
        if parts and parts[-1]:
            return parts[-1]
    for sep in ("·", "•", "・"):
        if sep in text:
            tail = text.split(sep)[-1].strip()
            if tail:
                return tail
    return text


def _serial_token(serial: str) -> str:
    """编数分母。`0149/2026` → 若是四位数年份（1900–2099）就当成年份，返回空串；
    `/99` → `/99`；`1/1` → `1/1`。

    为什么不一律输出 `/2026`：那是**产品年份**而不是限量，
    卖家标题里的年份本来就会写，重复一次只会把搜索词撑长、拉低召回。
    真正的限量编（/99、/25）才值得占一个位置。
    """
    match = _DENOM_RE.search(str(serial or ""))
    if not match:
        return ""
    den = int(match.group(1))
    if den <= 1:
        # 1 编的写法五花八门（1/1、one of one），统一成 1/1 最容易被搜到
        return "1/1"
    if 1900 <= den <= 2099:
        return ""
    return "/%d" % den


def build_search_query(sport: str = "", player: str = "",
                       year_set: str = "", card_type: str = "",
                       serial: str = "") -> str:
    """没填 search_query 时自动拼一个高召回搜索词。

    顺序：年份 → 系列核心词 → 球员名 → 限量编分母。
    故意**不放卡种/平行版本**（`Gold Refractor RC` 这类）：平行名在卖家标题里写法
    极不统一（金折 / Gold / Refractor / 折射），放进查询会显著拉低召回。
    宁可多召回几十条让雷达去筛，也不要一条都搜不到。
    """
    parts = []

    year_match = _YEAR_RE.search(str(year_set or ""))
    if year_match:
        parts.append(year_match.group(0))

    core = _series_core(year_set)
    if core:
        parts.append(core)

    name = _player_short(player)
    if name:
        parts.append(name)

    denom = _serial_token(serial)
    if denom:
        parts.append(denom)

    return " ".join(p for p in parts if p).strip()


def _atomic_write_json(path: str, payload) -> None:
    """原子写 JSON：先写同目录临时文件，fsync 后 os.replace。
    雷达是另一个进程在只读地轮询这些文件，绝对不能让它读到半截 JSON。"""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _export_vault_inventory() -> int:
    """把在库卡片清单导出给雷达，返回导出的卡片数。

    `status != '已售出'` 的都要询价——送评中、代拍中的卡同样需要知道行情。
    已售出的卡不再占用雷达的抓取预算。

    失败绝不能影响主流程：这是"顺手给雷达递个清单"，
    它挂了不该让用户录入卡片失败。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT card_id, sport, player, year_set, card_type, serial, "
            "       est_raw_price, search_query, notes, img_back FROM cards "
            " WHERE COALESCE(status, '') != '已售出' ORDER BY id ASC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0

    items = []
    for row in rows:
        item = dict(row)
        # notes 是用户自己写的版本描述（「首发年1st标金折/50」「02/10巴黎奥运会
        # 正赛比赛服国旗刺绣特大Patch双联书卡」），比任何自动识别都准。
        # 雷达拿它当**权威规格**做同款判定 —— 库里写明的，成交样本必须也满足。
        item["notes"] = item.get("notes") or ""
        # img_back 只递"有没有背面图"这个事实，不递文件名（雷达打开不了卡图）
        item["has_back"] = bool((item.get("img_back") or "").strip())
        item.pop("img_back", None)
        # 空 search_query 用自动拼的兜底，让雷达那头永远拿到一个可用的词
        if not (item.get("search_query") or "").strip():
            item["search_query"] = build_search_query(
                item.get("sport"), item.get("player"),
                item.get("year_set"), item.get("card_type"), item.get("serial"))
        items.append(item)

    payload = {
        "exported_ts": int(time.time()),
        "count": len(items),
        "items": items,
    }
    try:
        _atomic_write_json(VAULT_INVENTORY, payload)
    except OSError:
        return 0
    return len(items)


def _backup_db() -> str:
    """按日热备数据库，返回备份文件名（失败返回空串）。

    用 sqlite3 的 `.backup()` API 而不是复制文件——数据库可能正被写入，
    直接 cp 会拿到撕裂的副本（WAL 里还有没落盘的数据）。
    同一天多次写入只覆盖同一个文件，最多保留最近 BACKUP_KEEP 份。
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = "cards_%s.db" % datetime.now().strftime("%Y%m%d")
        target = os.path.join(BACKUP_DIR, name)
        tmp = "%s.tmp-%d" % (target, os.getpid())
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(tmp, target)
    except (sqlite3.Error, OSError):
        return ""

    # 轮转：文件名是 cards_YYYYMMDD.db，字典序即日期序
    try:
        backups = sorted(n for n in os.listdir(BACKUP_DIR)
                         if n.startswith("cards_") and n.endswith(".db"))
        for stale in backups[:-BACKUP_KEEP] if len(backups) > BACKUP_KEEP else []:
            try:
                os.remove(os.path.join(BACKUP_DIR, stale))
            except OSError:
                pass
    except OSError:
        pass
    return name


# 行情缓存的有效期。超过这个小时的行情仍然展示（比没有强），但前端会标「已过期」。
QUOTE_TTL_HOURS = float(os.getenv("QUOTE_TTL_HOURS", "168"))


def _load_vault_quotes() -> dict:
    """读雷达回传的库存行情。

    返回 `{"quotes": {card_id: {...}}, "scan_ts": int, "age_hours": float, "stale": bool}`。
    文件缺失/半截/坏格式一律当"没有行情"，绝不让它把卡片列表打成 500。
    """
    empty = {"quotes": {}, "scan_ts": 0, "age_hours": 0.0, "stale": True}
    if not os.path.isfile(VAULT_QUOTES):
        return empty
    try:
        with open(VAULT_QUOTES, "r", encoding="utf-8") as handle:
            payload = json.load(handle) or {}
    except (OSError, ValueError):
        return empty

    quotes = payload.get("quotes")
    if not isinstance(quotes, dict):
        return empty
    scan_ts = int(payload.get("scan_ts") or 0)
    age_hours = max(0.0, (time.time() - scan_ts) / 3600.0) if scan_ts else 0.0
    return {
        "quotes": quotes,
        "scan_ts": scan_ts,
        "age_hours": round(age_hours, 1),
        "stale": bool(scan_ts and age_hours > QUOTE_TTL_HOURS),
    }


def _quote_for(quotes: dict, card_id: str) -> Optional[dict]:
    """取某张卡的行情，并把明显不可用的（中位数 ≤ 0 / 没样本）过滤掉。"""
    row = quotes.get(card_id)
    if not isinstance(row, dict):
        return None
    try:
        median = float(row.get("median_rmb") or 0)
    except (TypeError, ValueError):
        return None
    if median <= 0:
        return None
    samples = row.get("samples")
    return {
        "median_rmb": round(median, 2),
        "sample_count": int(row.get("sample_count") or 0),
        "updated_ts": int(row.get("updated_ts") or 0),
        # basis 说明这个中位数的来源口径：
        #   sold       = 同评级档的**真实成交**中位数（硬，正常染色）
        #   sold_mixed = 同档成交不足 2 笔，混入了别的评级档（软，前端只做中性展示）
        # 2026-10-25 起不再产生 active / start 口径 —— 在拍现价不能当行情。
        "basis": str(row.get("basis") or ""),
        # 用户自己那张卡的评级档（gem/nine/low/raw），前端可据此说明"样本是否同级"
        "grade_bucket": str(row.get("grade_bucket") or ""),
        # 成交样本的时间窗（天），前端展示"近 N 天成交"
        "window_days": int(row.get("window_days") or 0),
        # ⚠️ 这几个字段必须透传，否则前端"最近一笔当主数字"的逻辑全部失效
        #（踩过：_quote_for 是白名单，漏字段时页面看着正常，但主数字悄悄退回中位数）。
        "latest_rmb": int(row.get("latest_rmb") or 0),
        "latest_closed": str(row.get("latest_closed") or ""),
        "recent_rmb": int(row.get("recent_rmb") or 0),
        # 最近一笔与中位数偏离过大（后端判脏）→ 前端改用 recent_rmb 当主数字并打 ⚠
        "latest_outlier": bool(row.get("latest_outlier")),
        # 样本是否按版本规格（颜色 / 中文子系列）严格筛过
        "spec_applied": bool(row.get("spec_applied")),
        "samples": samples if isinstance(samples, list) else [],
    }


def _attach_quotes(cards: list) -> None:
    """就地把行情挂到每张卡的 `market_quote` 上（没有行情就是 None，不编数字）。"""
    loaded = _load_vault_quotes()
    quotes = loaded["quotes"]
    for card in cards:
        card["market_quote"] = _quote_for(quotes, card.get("card_id", ""))


def _backfill_search_queries(conn) -> int:
    """给 search_query 还是空的老卡补一条默认询价词，返回补了几条。

    为什么要有这一段：`search_query` 是本次升级才加的列，111 张既有卡全是空值。
    没有回填的话，用户要逐张打开详情点保存才会生成询价词——而雷达那头看到的
    永远是「清单里 search_query 为空」，等于整个行情功能对老卡沉默失效。

    安全约束：
      · 只写 `search_query` 一列，其它字段（尤其 cost_price / est_raw_price）一律不碰；
      · UPDATE 的 WHERE 里**再判一次空**，所以并发的「用户刚填好」不会被覆盖；
      · 幂等：已经填过的卡不进候选集，重启多少次都只写第一遍。
    """
    rows = conn.execute(
        "SELECT card_id, sport, player, year_set, card_type, serial FROM cards "
        " WHERE search_query IS NULL OR TRIM(search_query) = ''"
    ).fetchall()
    if not rows:
        return 0
    updates = []
    for card_id, sport, player, year_set, card_type, serial in rows:
        query = build_search_query(sport, player, year_set, card_type, serial)
        if query:
            updates.append((query, card_id))
    if updates:
        conn.executemany(
            "UPDATE cards SET search_query = ? WHERE card_id = ? "
            "  AND (search_query IS NULL OR TRIM(search_query) = '')", updates)
        conn.commit()
    return len(updates)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT UNIQUE,
            sport TEXT,
            player TEXT,
            year_set TEXT,
            card_type TEXT,
            serial TEXT,
            est_raw_price REAL DEFAULT 0,
            cost_price REAL DEFAULT 0,
            status TEXT DEFAULT '在库',
            grading_verdict TEXT,
            notes TEXT,
            img_front TEXT,
            img_back TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("PRAGMA table_info(cards)")
    cols = {row[1] for row in cursor.fetchall()}
    for col, decl in MIGRATIONS.items():
        if col not in cols:
            cursor.execute(f"ALTER TABLE cards ADD COLUMN {col} {decl}")

    # 关键字段索引优化，加速品类筛选与搜索
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_sport_status ON cards(sport, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_player ON cards(player)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_created ON cards(created_at)")

    # 新加的 search_query 列在老数据上是空的，这里一次性补全（幂等）
    try:
        filled = _backfill_search_queries(conn)
        if filled:
            print(f"[init] 已为 {filled} 张老卡补全默认询价词")
    except sqlite3.Error as exc:          # 回填失败也不能拦住启动
        print(f"[init] 询价词回填跳过：{exc}")

    # 捡漏雷达的"已处理/已忽略"标记。独立建表，绝不动 cards 表——
    # 库里是真实收藏数据，任何雷达相关功能都只能旁挂，不能侵入。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS radar_marks (
            item_id TEXT PRIMARY KEY,
            state TEXT,
            note TEXT,
            updated_at TEXT
        )
    """)

    # 行情历史时序表：每次询价/应用行情时留存轨迹，支撑价格走势分析
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            median_rmb REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            grade_bucket TEXT DEFAULT '',
            window_days INTEGER DEFAULT 0,
            basis TEXT DEFAULT ''
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cph_card_id ON card_price_history(card_id, timestamp)")

    conn.commit()
    conn.close()

    # 建表/迁移完成后立刻把在库清单递给雷达一次，否则容器重启后雷达要等到
    # 下一次用户增删改才会拿到最新清单。
    _export_vault_inventory()


init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


TEXT_FIELDS = ("card_id", "sport", "player", "year_set", "card_type", "serial",
               "status", "grading_verdict", "notes", "img_front", "img_back",
               "img_front_orig", "img_back_orig", "photo_mode", "search_query",
               "created_at", "updated_at")
NUM_FIELDS = ("est_raw_price", "cost_price")
INT_FIELDS = ("rotate_front", "rotate_back")


def row_to_dict(row) -> dict:
    """把 NULL 归一成前端友好的默认值，避免 JS 对 null 取值报错。"""
    data = dict(row)
    for key in TEXT_FIELDS:
        if data.get(key) is None:
            data[key] = ""
    for key in NUM_FIELDS:
        if data.get(key) is None:
            data[key] = 0.0
    for key in INT_FIELDS:
        try:
            data[key] = int(data.get(key) or 0)
        except (TypeError, ValueError):
            data[key] = 0
    return data


# ============================ 实拍图落盘 ============================

def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(str(filename or ""))[1].lower()
    return ext if ext in ALLOWED_EXT else ".jpg"


def _write(name: str, data: bytes) -> str:
    """写入 uploads 目录。basename 兜底，杜绝任何路径成分。"""
    base = os.path.basename(name)
    with open(os.path.join(UPLOAD_DIR, base), "wb") as handle:
        handle.write(data)
    return base


def _write_atomic(name: str, data: bytes) -> str:
    """先写临时文件、fsync、再 os.replace 覆盖，返回最终文件名。

    为什么必须这样：`<img src>` 指向的是固定文件名，直接 `open(..., "wb")`
    会有「文件被截断成 0 字节但已被覆盖」的瞬间——此时任何一次页面刷新
    都会拿到半张图甚至破图（浏览器的缓存策略救不了，因为 Content-Length 变了）。
    先写 `xxx.jpg.tmp-<pid>` 再 replace，等于让展示图永远只在新旧两个完整状态之间跳。

    临时名同样以 `{card_id}_{side}` 开头，所以万一进程在 replace 前被杀，
    残留的 .tmp-* 会被后续的前缀清理顺手带走，不会变成永久孤儿。
    """
    base = os.path.basename(name)
    final = os.path.join(UPLOAD_DIR, base)
    tmp = "%s.tmp-%d" % (final, os.getpid())
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, final)
    return base


def _side_files(card_id: str, suffix: str) -> list:
    """列出该卡该面（front/back）在 uploads 下的全部文件。
    前缀匹配天然覆盖 `_orig` 备份与历史扩展名残留。
    注意 card_id 形如 `足球_20261013182448`，与 `..._2` 这类去重后缀不会互相误伤
    （`A_2_front.jpg` 不以 `A_front` 开头）。"""
    prefix = f"{card_id}_{suffix}"
    try:
        names = os.listdir(UPLOAD_DIR)
    except OSError:
        return []
    return [n for n in names
            if n.startswith(prefix) and os.path.isfile(os.path.join(UPLOAD_DIR, n))]


def clear_side(card_id: str, suffix: str, keep=()) -> list:
    """清掉该面所有历史文件（`keep` 里的名字保留）。
    替换图片、重新处理之前都要先调它，否则换扩展名或改档位后会留孤儿文件。"""
    keep = {os.path.basename(str(k)) for k in keep if k}
    removed = []
    for name in _side_files(card_id, suffix):
        if name in keep:
            continue
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
            removed.append(name)
        except OSError:
            pass
    return removed


def run_process(raw: bytes, mode: str, rotate) -> dict:
    """调用处理模块。失败绝不吞掉上传：退回原图字节，宁可不好看也不能丢图。"""
    try:
        return photos.process(raw, mode=mode, rotate=rotate)
    except Exception as exc:          # noqa: BLE001 - 兜底，任何异常都要退化成原图
        return {"data": raw, "mode": photos.MODE_ORIGINAL, "width": 0, "height": 0,
                "cropped": False, "tightened": False, "rotated": False,
                "auto_rotated": False, "exif_fixed": False, "how": "-",
                "aspect": 0.0, "detect": {}, "note": f"处理失败已退回原图（{exc}）"}


def store_photo(raw: bytes, card_id: str, suffix: str, ext: str,
                mode: str, rotate, keep_orig: bool = True) -> tuple:
    """把一张实拍图落盘，返回 (展示文件名, 原图备份名, 处理结果)。

    命名规则（全部以 card_id 开头，删卡片时按前缀一并清理）：
      {card_id}_{side}.jpg        处理后的展示图（非 original 档恒为 jpg）
      {card_id}_{side}{ext}       original 档的展示图，保持原格式
      {card_id}_{side}_orig{ext}  原图备份，供「重新处理 / 保留原图」回退

    `keep_orig=False` 时不写备份（展示图即原图），用于用户明确表示
    「本地已有原图、不需要 NAS 再存一份」的场景。

    ⚠️ 顺序是刻意的：**先处理、先落盘，最后才清理旧文件**。
    旧版一进来就 `clear_side()`，等于「先删掉唯一一张图，再去算新图」——
    只要处理或写盘中途出事，用户那张卡的图就彻底没了（前端表现为死链）。
    现在最坏情况是同时存在新旧两套文件（多占几 MB），但**永远有一张能显示**。
    """
    result = run_process(raw, mode, rotate)

    if result.get("mode") == photos.MODE_ORIGINAL:
        # 展示图就是原图本身，再存一份备份纯属浪费
        img = _write_atomic(f"{card_id}_{suffix}{ext}", result["data"])
        clear_side(card_id, suffix, keep=(img,))
        return img, "", result

    img = _write_atomic(f"{card_id}_{suffix}.jpg", result["data"])
    orig = _write_atomic(f"{card_id}_{suffix}_orig{ext}", raw) if keep_orig else ""
    clear_side(card_id, suffix, keep=(img, orig))
    return img, orig, result


async def save_upload(upload: Optional[UploadFile], card_id: str, suffix: str,
                      mode: str, rotate, keep_orig: bool = True) -> tuple:
    """保存上传的实拍图，返回 (展示文件名, 原图备份名)。没传文件时返回 ("", "")。"""
    if upload is None or not upload.filename:
        return "", ""
    raw = await upload.read()
    if not raw:
        return "", ""
    img, orig, _ = store_photo(raw, card_id, suffix, _safe_ext(upload.filename),
                               mode, rotate, keep_orig)
    return img, orig


# ============================ 基础接口 ============================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 注意：Starlette >= 0.29 推荐使用关键字参数形式；
    # Starlette 1.x 已移除旧的 TemplateResponse(name, {"request": request}) 位置参数写法。
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/radar", response_class=HTMLResponse)
async def radar_page(request: Request):
    """捡漏雷达看板。样式与 index 完全同源，导航互通。"""
    return templates.TemplateResponse(request=request, name="radar.html")


# 排序白名单。前端下拉的 value 必须落在这些键里，**绝不把用户输入拼进 SQL**。
# 每个值都带 `, id DESC` 兜底，保证同价时不出现「刷新一次顺序就变」的抖动。
#
# 盈亏排序用 `CASE WHEN cost_price > 0` ：没填买入成本的卡按 NULL 处理，
# SQLite 里 NULL 在 DESC 时排最后、ASC 时排最前——正好是「没有盈亏数据」该待的位置，
# 不会拿 cost=0 算出的假暴利去霸占榜首。
SORTS = {
    "price_desc": "估值 高→低",
    "price_asc": "估值 低→高",
    "pnl_desc": "盈亏 高→低",
    "pnl_asc": "盈亏 低→高",
    "cost_desc": "成本 高→低",
    "newest": "最新录入",
    "oldest": "最早录入",
}

_SORT_SQL = {
    "price_desc": "COALESCE(est_raw_price, 0) DESC, id DESC",
    "price_asc": "COALESCE(est_raw_price, 0) ASC, id DESC",
    "pnl_desc": "(CASE WHEN cost_price > 0 THEN COALESCE(est_raw_price, 0) - cost_price END) DESC, id DESC",
    "pnl_asc": "(CASE WHEN cost_price > 0 THEN COALESCE(est_raw_price, 0) - cost_price END) ASC, id DESC",
    "cost_desc": "COALESCE(cost_price, 0) DESC, id DESC",
    "newest": "id DESC",
    "oldest": "id ASC",
}

DEFAULT_SORT = "price_desc"


def sort_sql(name: Optional[str]) -> tuple:
    """返回 (排序键, ORDER BY 片段)。未知值一律回落到默认排序，不报错。"""
    key = name if name in _SORT_SQL else DEFAULT_SORT
    return key, _SORT_SQL[key]


@app.get("/api/cards")
async def list_cards(status: Optional[str] = None, sport: Optional[str] = None,
                     sort: Optional[str] = None):
    sort_key, order_by = sort_sql(sort)
    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM cards WHERE 1=1"
    params = []
    if status and status != "全部":
        query += " AND status = ?"
        params.append(status)
    if sport and sport != "全部":
        query += " AND sport = ?"
        params.append(sport)
    query += " ORDER BY " + order_by
    cursor.execute(query, params)
    rows = [row_to_dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT COUNT(*), SUM(est_raw_price), SUM(cost_price) FROM cards")
    total_count, total_val, total_cost = cursor.fetchone()
    conn.close()

    # 行情只挂在内存里的这份副本上，**绝不写回数据库**——
    # 采纳与否必须由用户点按钮决定（红线）。
    _attach_quotes(rows)
    market = _load_vault_quotes()

    return {
        "cards": rows,
        "sort": sort_key,
        "market": {
            "scan_ts": market["scan_ts"],
            "age_hours": market["age_hours"],
            "stale": market["stale"],
            "quoted": sum(1 for c in rows if c.get("market_quote")),
        },
        "stats": {
            "total_count": total_count or 0,
            "total_val": total_val or 0,
            "total_cost": total_cost or 0
        }
    }


@app.get("/healthz")
async def healthz():
    """容器健康检查专用端点。

    为什么不直接拿 /api/sorts 当健康检查：
    - 那是业务接口，语义会随业务变化（将来改返回结构，健康检查会莫名其妙变红）；
    - 但健康检查**必须真的摸一次数据库** —— 只回 200 的浅检查会在
      cards.db 损坏/被锁时依然显示健康，等于把故障藏起来。
    所以这里做的是「独立端点 + 真实 DB 探针」：能查通才算健康。
    """
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="数据库不可用: %s" % exc)
    return {"status": "ok"}


@app.get("/api/sorts")
async def list_sorts():
    """排序选项。前端下拉是静态写死的，这里给同一份清单做交叉校验用。"""
    return {"sorts": [{"value": k, "label": v} for k, v in SORTS.items()],
            "default": DEFAULT_SORT}


# ⚠️ 这个路由必须声明在 `/api/cards/{card_id}` **之前**。
# FastAPI 按声明顺序匹配，`POST /api/cards/refresh-quotes` 会被
# `POST /api/cards/{card_id}`（更新卡片）先吃掉，card_id 变成 "refresh-quotes"。
@app.post("/api/cards/refresh-quotes")
async def refresh_quotes():
    """点「刷新库存行情」→ 投递一份询价请求给雷达，雷达 20 秒内取走执行。

    行情查询是走卡淘搜索接口的，一轮要几十秒，不可能在 HTTP 请求里同步等。
    所以这里只负责"投递请求 + 告诉用户已排队"，结果靠雷达写回 vault_quotes.json。
    """
    if not os.path.isdir(RADAR_DIR):
        return {"status": "unavailable",
                "message": "雷达数据目录没挂上，请检查 docker-compose 的只读挂载"}

    previous = {}
    if os.path.isfile(VAULT_QUOTE_REQUEST):
        try:
            with open(VAULT_QUOTE_REQUEST, "r", encoding="utf-8") as handle:
                previous = json.load(handle) or {}
        except (OSError, ValueError):
            previous = {}

    now = time.time()
    requested_ts = float(previous.get("requested_ts") or 0)
    if requested_ts and now - requested_ts < QUOTE_COOLDOWN_SEC:
        wait = int(QUOTE_COOLDOWN_SEC - (now - requested_ts))
        return {"status": "cooldown",
                "message": "刚刚已经投递过询价请求，%d 秒后可再试" % max(wait, 1),
                "requested_ts": int(requested_ts)}

    count = _export_vault_inventory()          # 先保证清单是最新的
    req = {
        "req_id": "%d" % int(now),
        "requested_ts": int(now),
        "count": count,
    }
    try:
        _atomic_write_json(VAULT_QUOTE_REQUEST, req)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="写入询价请求失败：%s" % exc)

    return {"status": "queued", "req_id": req["req_id"], "count": count,
            "message": "已通知雷达查询 %d 张在库卡片的行情，稍后刷新页面看结果" % count}


@app.post("/api/cards/{card_id}/search-query")
async def set_search_query(card_id: str, search_query: str = Form("")):
    """只改行情搜索词。单独开一个接口，是为了不必把整张卡的表单字段重发一遍——
    重发就会有"用户 A 改了搜索词，把用户 B 刚改的估值覆盖回去"的风险，
    虽然只有我一个人用，但把写入面收窄本身就是免费的保险。
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT sport, player, year_set, card_type, serial FROM cards WHERE card_id = ?",
                   (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    text = (search_query or "").strip()
    if not text:
        # 清空 = 让后端按当前字段重新拼，别留一个再也匹配不到东西的旧词
        text = build_search_query(row["sport"], row["player"], row["year_set"],
                                  row["card_type"], row["serial"])

    cursor.execute("UPDATE cards SET search_query = ?, updated_at = ? WHERE card_id = ?",
                   (text, now_str(), card_id))
    conn.commit()
    conn.close()

    _export_vault_inventory()
    return {"status": "success", "card_id": card_id, "search_query": text}


@app.get("/api/cards/{card_id}")
async def get_card(card_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="卡片不存在")
    card = row_to_dict(row)
    _attach_quotes([card])
    return card


@app.get("/api/cards/{card_id}/price-history")
async def get_card_price_history(card_id: str):
    """获取单卡历史估值轨迹及卡淘成交 comps 散点，供走势图使用"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, median_rmb, sample_count, grade_bucket, basis
        FROM card_price_history
        WHERE card_id = ?
        ORDER BY id ASC
    """, (card_id,))
    rows = cursor.fetchall()

    cursor.execute("SELECT est_raw_price FROM cards WHERE card_id = ?", (card_id,))
    card_row = cursor.fetchone()
    conn.close()

    cur_est = float(card_row["est_raw_price"] or 0) if card_row else 0.0

    history = [
        {
            "timestamp": r["timestamp"],
            "median_rmb": float(r["median_rmb"] or 0),
            "sample_count": int(r["sample_count"] or 0),
            "basis": r["basis"] or ""
        }
        for r in rows
    ]

    vault_quotes = _load_vault_quotes()
    card_quote = _quote_for(vault_quotes.get("quotes", {}), card_id)
    comps = []
    if card_quote and isinstance(card_quote.get("samples"), list):
        for s in card_quote["samples"]:
            try:
                p = float(s.get("price") or 0)
                if p > 0:
                    comps.append({
                        "price": p,
                        "date": s.get("closed_str") or "",
                        "title": s.get("title") or ""
                    })
            except Exception:
                pass

    return {
        "status": "success",
        "card_id": card_id,
        "current_est": cur_est,
        "history": history,
        "comps": comps
    }


@app.post("/api/cards/{card_id}/apply-quote")
async def apply_quote(card_id: str, price: float = Form(...)):
    """「⚡ 采纳为当前估值」——**这是全系统唯一一处由行情改写 est_raw_price 的入口**，
    而且必须由用户在界面上点一下才会调用。雷达自己永远不会写这张表。

    只改 est_raw_price 一个字段：用户点的是"这个价格我认"，不是"把这张卡改成别的样子"。
    """
    if not (price > 0) or price != price or price in (float("inf"), float("-inf")):
        raise HTTPException(status_code=400, detail="估价必须是大于 0 的数字")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT est_raw_price FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")
    old_price = float(row["est_raw_price"] or 0)

    cursor.execute("UPDATE cards SET est_raw_price = ?, updated_at = ? WHERE card_id = ?",
                   (round(float(price), 2), now_str(), card_id))
    # 顺带留存行情历史记录
    try:
        cursor.execute("""
            INSERT INTO card_price_history (card_id, timestamp, median_rmb, sample_count, basis)
            VALUES (?, ?, ?, 1, 'applied_quote')
        """, (card_id, now_str(), round(float(price), 2)))
    except Exception:
        pass
    conn.commit()
    conn.close()

    _export_vault_inventory()
    _backup_db()
    return {"status": "success", "card_id": card_id,
            "old_price": old_price, "est_raw_price": round(float(price), 2)}


@app.post("/api/ai/parse-title")
async def parse_title(request: Request):
    """调用 Scrapling 容器 (8090) 的 DeepSeek AI 服务解析球卡长标题，返回结构化字段"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = (data.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "标题不能为空"}

    import urllib.request
    import urllib.error

    url = f"{SCRAPLING_API_BASE.rstrip('/')}/api/ai/parse-card"
    req_body = json.dumps({"title": title}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return {"status": "error", "message": f"AI 解析服务响应错误 (HTTP {e.code}): {err_msg[:200]}"}
    except urllib.error.URLError as e:
        return {"status": "error", "message": f"无法连接 AI 解析服务 ({e.reason})，请确认 8090 容器及 PC 网关运行中"}
    except Exception as e:
        return {"status": "error", "message": f"AI 解析异常: {str(e)}"}


@app.post("/api/cards")
async def add_card(
    sport: str = Form(...),
    player: str = Form(...),
    year_set: str = Form(...),
    card_type: str = Form(""),
    serial: str = Form(""),
    est_raw_price: float = Form(0.0),
    cost_price: float = Form(0.0),
    status: str = Form("在库"),
    grading_verdict: str = Form(""),
    notes: str = Form(""),
    photo_mode: str = Form(photos.MODE_AUTO),
    search_query: str = Form(""),
    img_front: Optional[UploadFile] = File(None),
    img_back: Optional[UploadFile] = File(None)
):
    if photo_mode not in photos.MODES:
        photo_mode = photos.MODE_AUTO

    # 没填就在服务端补一个默认搜索词。**兜底只在后端做一份**——
    # 前后端各写一套拼装逻辑必然漂移，而这里没有任何理由需要前端知道规则。
    search_query = (search_query or "").strip() or build_search_query(
        sport, player, year_set, card_type, serial)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base_id = f"{sport[:2]}_{timestamp}"

    conn = get_db()
    cursor = conn.cursor()

    # 同一秒内连续录入时，避免 card_id 撞唯一约束
    card_id = base_id
    suffix = 1
    cursor.execute("SELECT 1 FROM cards WHERE card_id = ?", (card_id,))
    while cursor.fetchone():
        suffix += 1
        card_id = f"{base_id}_{suffix}"
        cursor.execute("SELECT 1 FROM cards WHERE card_id = ?", (card_id,))

    front_filename, front_orig = await save_upload(img_front, card_id, "front", photo_mode, 0)
    back_filename, back_orig = await save_upload(img_back, card_id, "back", photo_mode, 0)
    stamp = now_str()

    cursor.execute("""
        INSERT INTO cards (
            card_id, sport, player, year_set, card_type, serial,
            est_raw_price, cost_price, status, grading_verdict, notes,
            img_front, img_back, img_front_orig, img_back_orig, photo_mode,
            search_query, rotate_front, rotate_back, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
    """, (
        card_id, sport, player, year_set, card_type, serial,
        est_raw_price, cost_price, status, grading_verdict, notes,
        front_filename, back_filename, front_orig, back_orig, photo_mode,
        search_query, stamp, stamp
    ))
    conn.commit()
    conn.close()

    # 新增后立刻把最新在库清单递给雷达（否则新卡要等到下一次增删改才进询价名单）
    _export_vault_inventory()
    _backup_db()
    return {"status": "success", "card_id": card_id, "search_query": search_query}


@app.api_route("/api/cards/{card_id}", methods=["POST", "PUT"])
async def update_card(
    card_id: str,
    sport: str = Form(...),
    player: str = Form(...),
    year_set: str = Form(...),
    card_type: str = Form(""),
    serial: str = Form(""),
    est_raw_price: float = Form(0.0),
    cost_price: float = Form(0.0),
    status: str = Form("在库"),
    grading_verdict: str = Form(""),
    notes: str = Form(""),
    photo_mode: str = Form(""),
    search_query: str = Form(""),
    img_front: Optional[UploadFile] = File(None),
    img_back: Optional[UploadFile] = File(None)
):
    """更新卡片详情。实拍图为选传替换：不传文件则保留原图。
    注意：旋转角度不在这里改——对已有图旋转请走 /reprocess，
    否则「编辑文字」这个动作会莫名其妙地重算图片。"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    existing = cursor.fetchone()
    if existing is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    mode = photo_mode if photo_mode in photos.MODES else (existing["photo_mode"] or photos.MODE_AUTO)

    front_filename, front_orig = await save_upload(img_front, card_id, "front", mode, 0)
    back_filename, back_orig = await save_upload(img_back, card_id, "back", mode, 0)

    if not front_filename:
        front_filename, front_orig = (existing["img_front"] or ""), (existing["img_front_orig"] or "")
    if not back_filename:
        back_filename, back_orig = (existing["img_back"] or ""), (existing["img_back_orig"] or "")

    # 编辑时留空 = 重新按新字段自动拼（用户改了系列或编数，旧搜索词就不对了）
    search_query = (search_query or "").strip() or build_search_query(
        sport, player, year_set, card_type, serial)

    cursor.execute("""
        UPDATE cards SET
            sport = ?, player = ?, year_set = ?, card_type = ?, serial = ?,
            est_raw_price = ?, cost_price = ?, status = ?, grading_verdict = ?,
            notes = ?, img_front = ?, img_back = ?, img_front_orig = ?,
            img_back_orig = ?, photo_mode = ?, search_query = ?, updated_at = ?
        WHERE card_id = ?
    """, (
        sport, player, year_set, card_type, serial,
        est_raw_price, cost_price, status, grading_verdict,
        notes, front_filename, back_filename, front_orig, back_orig, mode,
        search_query, now_str(), card_id
    ))
    conn.commit()
    conn.close()

    _export_vault_inventory()
    _backup_db()
    return {
        "status": "success",
        "card_id": card_id,
        "img_front": front_filename,
        "img_back": back_filename,
        "photo_mode": mode,
        "search_query": search_query
    }


@app.post("/api/cards/{card_id}/status")
async def update_status(card_id: str, status: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE cards SET status = ?, updated_at = ? WHERE card_id = ?",
                   (status, now_str(), card_id))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if changed == 0:
        raise HTTPException(status_code=404, detail="卡片不存在")
    # 状态会影响"是否在库"，所以在库清单必须跟着变（售出的卡不再询价）
    _export_vault_inventory()
    return {"status": "success"}


# ============================ 图片处理接口 ============================

@app.get("/api/photo-modes")
async def photo_modes():
    """前端下拉用。同时把 opencv 是否可用透出来，方便判断自动裁切能不能跑。"""
    return {
        "modes": [{"value": m, "label": photos.MODE_LABELS[m]} for m in photos.MODES],
        "default": photos.MODE_AUTO,
        "opencv": bool(photos.HAS_CV2),
        "max_edge": photos.MAX_EDGE,
        "quality": photos.JPEG_Q,
    }


@app.post("/api/photo/preview")
async def photo_preview(
    file: UploadFile = File(...),
    mode: str = Form(photos.MODE_AUTO),
    rotate: int = Form(0)
):
    """干跑一次：把上传的手机实拍按指定档位处理，把结果以 data URL 回给前端预览。
    不落盘、不写库，纯计算。用 data URL 而不是裸图片流，是为了能把中文的
    处理说明一起带回去（HTTP 头里塞中文会 Latin-1 编码报错）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    result = run_process(raw, mode, rotate)
    data = result.get("data") or raw
    media = "image/jpeg"
    if result.get("mode") == photos.MODE_ORIGINAL:
        media = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    return {
        "status": "success",
        "note": result.get("note", ""),
        "mode": result.get("mode"),
        "cropped": bool(result.get("cropped")),
        "rotated": bool(result.get("rotated")),
        "exif_fixed": bool(result.get("exif_fixed")),
        "aspect": result.get("aspect", 0.0),
        "width": result.get("width", 0),
        "height": result.get("height", 0),
        "src_bytes": len(raw),
        "out_bytes": len(data),
        "data_url": f"data:{media};base64," + base64.b64encode(data).decode("ascii"),
    }


def _reprocess_one(card_id: str, side: str, mode: str, rotate, keep_orig: bool = True) -> dict:
    """用原图（没有原图备份就用当前展示图）重新处理某一面。返回结果 dict。"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    img_col, orig_col, rot_col = f"img_{side}", f"img_{side}_orig", f"rotate_{side}"
    source = row[orig_col] or row[img_col]
    if not source:
        conn.close()
        raise HTTPException(status_code=400, detail="该面还没有实拍图")

    path = os.path.join(UPLOAD_DIR, os.path.basename(str(source)))
    if not os.path.isfile(path):
        conn.close()
        raise HTTPException(status_code=404, detail=f"源图文件已丢失：{os.path.basename(str(source))}")

    with open(path, "rb") as handle:
        raw = handle.read()
    ext = os.path.splitext(path)[1].lower()

    rotate = int(rotate or 0) % 360
    img, orig, result = store_photo(raw, card_id, side, ext, mode, rotate, keep_orig)

    cursor.execute(
        f"UPDATE cards SET {img_col} = ?, {orig_col} = ?, {rot_col} = ?, "
        f"photo_mode = ?, updated_at = ? WHERE card_id = ?",
        (img, orig, rotate, result.get("mode"), now_str(), card_id))
    conn.commit()
    conn.close()

    return {
        "card_id": card_id, "side": side, "mode": result.get("mode"),
        "rotate": rotate, "note": result.get("note", ""),
        "cropped": bool(result.get("cropped")), "rotated": bool(result.get("rotated")),
        "aspect": result.get("aspect", 0.0),
        "width": result.get("width", 0), "height": result.get("height", 0),
        "src_bytes": len(raw), "out_bytes": os.path.getsize(os.path.join(UPLOAD_DIR, img)),
        "img": img, "img_orig": orig,
    }


@app.post("/api/cards/{card_id}/reprocess")
async def reprocess_photo(
    card_id: str,
    side: str = Form(...),
    mode: str = Form(photos.MODE_AUTO),
    rotate: int = Form(0),
    keep_orig: bool = Form(True)
):
    """用原图重新处理某一面。

    三个用途：
    - 换个档位重来（auto / straighten / original）；
    - 手动旋转：rotate 传 90 / 180 / 270（顺时针）；
    - 「保留原图」：mode=original，展示图直接退回原图字节。
    原图备份默认保留，所以任何一步都可以再改回去。
    """
    side = (side or "").strip().lower()
    if side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="side 只能是 front 或 back")
    if mode not in photos.MODES:
        mode = photos.MODE_AUTO
    return _reprocess_one(card_id, side, mode, rotate, keep_orig)


@app.post("/api/maintenance/reprocess-all")
async def reprocess_all(
    mode: str = Form(photos.MODE_AUTO),
    rotate: int = Form(0),
    keep_orig: bool = Form(True),
    confirm: str = Form("no")
):
    """把库里所有实拍图按指定档位统一重做一遍（用原图作源，不会越做越糊）。
    刻意要求 confirm=yes，避免手滑把 100 张图全重算。
    结果同时写一份到 data/last_reprocess.json，方便连接中断后仍能查看。"""
    if (confirm or "").strip().lower() not in ("yes", "y", "true", "1"):
        raise HTTPException(status_code=400, detail="需要 confirm=yes 才执行")
    if mode not in photos.MODES:
        mode = photos.MODE_AUTO

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT card_id, img_front, img_back FROM cards ORDER BY id")
    rows = cursor.fetchall()
    conn.close()

    report, src_total, out_total, errors = [], 0, 0, []
    for row in rows:
        for side, has in (("front", row["img_front"]), ("back", row["img_back"])):
            if not has:
                continue
            try:
                info = _reprocess_one(row["card_id"], side, mode, rotate, keep_orig)
                report.append(info)
                src_total += info["src_bytes"]
                out_total += info["out_bytes"]
            except HTTPException as exc:
                errors.append({"card_id": row["card_id"], "side": side, "error": str(exc.detail)})
            except Exception as exc:                     # noqa: BLE001
                errors.append({"card_id": row["card_id"], "side": side, "error": repr(exc)})

    summary = {
        "status": "success", "mode": mode, "rotate": rotate, "keep_orig": keep_orig,
        "total": len(report), "errors": errors,
        "src_bytes": src_total, "out_bytes": out_total,
        "ratio": round(out_total / src_total, 4) if src_total else 0,
        "cropped": sum(1 for r in report if r["cropped"]),
        "detected_fail": [f"{r['card_id']}_{r['side']}" for r in report if not r["cropped"]],
        "items": report,
    }
    try:
        with open(os.path.join(DATA_DIR, "last_reprocess.json"), "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return summary


@app.post("/api/maintenance/drop-originals")
async def drop_originals(confirm: str = Form("no")):
    """删掉 uploads 下的 `_orig` 原图备份，并清空对应数据库列。
    只在用户确认「本地已另有原图」时使用——删完就没法再从 NAS 回退到原图了。
    仍然保留三重保护：basename、必须以 `_orig` 结尾、必须是 uploads 下的普通文件。"""
    if (confirm or "").strip().lower() not in ("yes", "y", "true", "1"):
        raise HTTPException(status_code=400, detail="需要 confirm=yes 才执行")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT card_id, img_front_orig, img_back_orig FROM cards")
    rows = cursor.fetchall()

    removed, freed, missing = [], 0, []
    for row in rows:
        for col in ("img_front_orig", "img_back_orig"):
            name = row[col]
            if not name:
                continue
            base = os.path.basename(str(name))
            if not base.endswith("_orig" + os.path.splitext(base)[1]):
                continue                      # 只认 _orig 备份，别的一律不碰
            path = os.path.join(UPLOAD_DIR, base)
            if not os.path.isfile(path):
                missing.append(base)
                continue
            size = os.path.getsize(path)
            try:
                os.remove(path)
                removed.append(base)
                freed += size
            except OSError:
                pass

    cursor.execute("UPDATE cards SET img_front_orig = '', img_back_orig = ''")
    conn.commit()
    conn.close()
    return {"status": "success", "removed": removed, "removed_count": len(removed),
            "freed_bytes": freed, "already_missing": missing}


# ============================ 删除卡片 ============================

def safe_remove_uploads(card_id: str, filenames) -> list:
    """删除该卡片的实拍图，三重保护避免误删：
    1) os.path.basename() 剥掉任何路径成分，杜绝目录穿越；
    2) 文件名必须以 {card_id}_front / {card_id}_back 开头，只认本卡片的命名规则
       （`_orig` 备份天然落在这个前缀里，会一并清掉）；
    3) 只处理 uploads 目录下真实存在的普通文件。
    除显式传入的文件名外，再按前缀扫一遍目录，清掉换过扩展名/档位的历史残留。
    """
    targets = {os.path.basename(str(n)) for n in filenames if n}
    for name in _side_files(card_id, "front") + _side_files(card_id, "back"):
        targets.add(name)

    prefixes = (f"{card_id}_front", f"{card_id}_back")
    removed = []
    for base in sorted(targets):
        if not base.startswith(prefixes):
            continue
        path = os.path.join(UPLOAD_DIR, base)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(base)
        except OSError:
            pass  # 删除失败不影响数据库记录，仅跳过
    return removed


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    cursor.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
    conn.commit()
    conn.close()

    # 数据库删除成功后再清理实拍图，避免 DB 失败时把图片先删没了
    removed = safe_remove_uploads(card_id, [row["img_front"], row["img_back"]])
    _export_vault_inventory()   # 删掉的卡必须立刻从雷达的询价名单里消失
    _backup_db()
    return {"status": "success", "removed_images": removed}


# ============================ 捡漏雷达看板（只读对接） ============================
# 数据来源：cardhobby-radar 容器写出的 JSONL 流水，以**只读**方式挂载进来。
# 本模块对雷达数据只读不写；唯一的写操作是 radar_marks（已处理/已忽略标记），
# 存在 Card Vault 自己的 cards.db 里，与雷达互不干扰。
#
# 注意 RADAR_DIR / RADAR_CTL_DIR 已在文件顶部定义（init_db 要提前用到），这里只挂文件名。

RADAR_HITS = os.path.join(RADAR_DIR, "hits.jsonl")
RADAR_STATUS = os.path.join(RADAR_DIR, "radar_status.json")
# 雷达写出的「正在扫描 / 已结束」状态，网页据此显示进度而不是干等
RADAR_STATE = os.path.join(RADAR_DIR, "radar_state.json")

# ---- 成交回填与学习闭环（都是雷达写的，这里只读）----
# closed_fills.jsonl：截标后回抓的真实成交价，是唯一能验证参照价的真相来源
RADAR_FILLS = os.path.join(RADAR_DIR, "closed_fills.jsonl")
# calibration.json：按 (球员, 档位) 聚合的实盘倍数
RADAR_CALIB = os.path.join(RADAR_DIR, "calibration.json")
# learned_rules.json：从「有误」反馈提炼的拦截名单与排除词建议
RADAR_LEARNED = os.path.join(RADAR_DIR, "learned_rules.json")
# watch_track.json：已登记但还没截标的追踪表，用于告诉用户"这张在等成交价"
RADAR_TRACK = os.path.join(RADAR_DIR, "watch_track.json")

RESCAN_REQUEST = os.path.join(RADAR_CTL_DIR, "radar_rescan.json")
RESCAN_COOLDOWN_SEC = float(os.getenv("RESCAN_COOLDOWN_SEC", "45"))

# 超过这个小时数的命中不再展示（默认 24h：一天的轮次叠加已经够看，
# 留太久会让页面被两三天前的卡撑爆，而那些早已截标）
RADAR_KEEP_HOURS = float(os.getenv("RADAR_KEEP_HOURS", "24"))

# 成交回填行的保留窗口。比命中窗口长得多：命中行是「待抢的卡」，过期就没用；
# 成交行是「市场成交记录」，放三天都还有校准价值——L3 校准要按档位攒样本，
# 样本刚过期就没人看了，学习闭环等于断在展示层。
RADAR_FILLS_KEEP_HOURS = float(os.getenv("RADAR_FILLS_KEEP_HOURS", "72"))
# 成交行最多补这么多条，防止跑了几周后把页面刷成成交流水
RADAR_FILLS_MAX = int(os.getenv("RADAR_FILLS_MAX", "80"))
# 成交流水里没存链接，按规范模板拼；和雷达侧的 CARDHOBBY_ITEM_URL_TPL 保持一致
RADAR_ITEM_URL_TPL = os.getenv(
    "CARDHOBBY_ITEM_URL_TPL",
    "https://www.cardhobby.com.cn/market/item/{item_id}",
)

MARK_STATES = ("done", "ignore")


def _read_jsonl(path: str) -> list:
    """逐行读 JSONL，坏行静默跳过。
    雷达是边扫边 append 的，读到的最后一行完全可能是半截 JSON——
    一条脏数据不该让整页 500。"""
    if not os.path.isfile(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records


def _radar_status() -> dict:
    if not os.path.isfile(RADAR_STATUS):
        return {}
    try:
        with open(RADAR_STATUS, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def _read_json_file(path: str) -> dict:
    """读一个可选 JSON 文件，缺失/半截/坏格式一律当空字典。"""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def _radar_scan_state() -> dict:
    """雷达的实时扫描状态 + 请求投递情况，供前端显示进度。"""
    state = _read_json_file(RADAR_STATE)
    req = _read_json_file(RESCAN_REQUEST)
    requested_ts = int(req.get("requested_ts") or 0)
    return {
        "running": bool(state.get("running")),
        "mode": state.get("mode", ""),
        "mode_label": state.get("mode_label", ""),
        "started_ts": int(state.get("started_ts") or 0),
        "finished_ts": int(state.get("finished_ts") or 0),
        "req_id": state.get("req_id", ""),
        "request_ts": requested_ts,
        # 请求已投递但雷达还没动手：容器没起 / 挂载没生效 / 正在跑定时扫描。
        # 注意要和 finished_ts 比大小 —— 否则请求完成后这个标记会永远为真。
        "request_pending": bool(requested_ts) and not bool(state.get("running"))
                           and int(state.get("finished_ts") or 0) < requested_ts,
    }


def _write_rescan_request() -> str:
    """原子写一份刷新请求。req_id 用毫秒时间戳，天然去重。"""
    req_id = "web-%d" % int(time.time() * 1000)
    payload = {
        "req_id": req_id,
        "requested_ts": int(time.time()),
        "requested_by": "card-vault",
    }
    tmp = RESCAN_REQUEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, RESCAN_REQUEST)
    return req_id


def _load_marks() -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT item_id, state, note, updated_at FROM radar_marks")
    rows = {row["item_id"]: dict(row) for row in cursor.fetchall()}
    conn.close()
    return rows


def _radar_hits_view() -> list:
    """把流水折成"每张卡一行"：同一条拍品 17:00 / 19:20 各扫一次，
    只保留最新一次（价格、剩余时间都更新过），旧记录已经完成历史使命。

    并且**只认最新一代扫描**（scan_id 最大那批）。hits.jsonl 是 append-only 流水，
    如果只按 item_id 取最新，被规则拦掉的卡会因为"这一代不再写入"而永远沿用上一代
    的旧记录挂在页面上——规则收紧了、页面上的 321 倍幻觉却怎么刷都刷不掉。
    """
    now = time.time()
    floor_ts = now - RADAR_KEEP_HOURS * 3600

    latest = {}
    newest_gen = 0
    for rec in _read_jsonl(RADAR_HITS):
        item_id = str(rec.get("item_id") or "").strip()
        if not item_id:
            continue
        gen = int(rec.get("scan_id") or 0)
        if gen > newest_gen:
            newest_gen = gen
        prev = latest.get(item_id)
        if prev is None or int(rec.get("scan_ts") or 0) >= int(prev.get("scan_ts") or 0):
            latest[item_id] = rec

    # 无 scan_id 的老记录（升级前写入）在最新一代存在时一律不展示
    if newest_gen:
        latest = {k: v for k, v in latest.items()
                  if int(v.get("scan_id") or 0) == newest_gen}

    marks = _load_marks()

    # 真实成交价：同一张卡可能被回填多次（首抓 + 延时竞价后复核），取最后一次
    fills = {}
    for rec in _read_jsonl(RADAR_FILLS):
        iid = str(rec.get("item_id") or "").strip()
        if not iid:
            continue
        prev = fills.get(iid)
        if prev is None or int(rec.get("closed_at") or 0) >= int(prev.get("closed_at") or 0):
            fills[iid] = rec

    # 还在等成交价的（已登记未回填），前端显示「等成交价」而不是空白
    tracked = {}
    for iid, rec in ((_read_json_file(RADAR_TRACK).get("items") or {}).items()):
        if not rec.get("fill_done"):
            tracked[str(iid)] = rec

    hits = []
    for item_id, rec in latest.items():
        scan_ts = int(rec.get("scan_ts") or 0)
        remain_min = int(rec.get("remaining_minutes") or 0)
        # 截标时刻 = 扫描时刻 + 当时剩余分钟数。比直接用 end_time_str（只有 HH:MM）
        # 可靠得多：跨天时不会把 00:30 算成已过期。
        deadline_ts = scan_ts + remain_min * 60
        if deadline_ts < floor_ts:
            continue

        mark = marks.get(item_id) or {}
        hits.append({
            "item_id": item_id,
            "player_cn": rec.get("player_cn", ""),
            "player_en": rec.get("player_en", ""),
            "sport": rec.get("sport", ""),
            "title": rec.get("title", ""),
            "consignor": rec.get("consignor", ""),
            "tier_desc": rec.get("tier_desc", ""),
            "tier_key": rec.get("tier_key", ""),
            # 签字轨 / 折射轨 —— v3 双轨定价的关键信息，网页要标出来
            "tier_basis": rec.get("tier_basis", ""),
            # steal(真漏) / near_miss(准漏) / blind(¥1 起拍 0 出价，需盲拍)
            "opportunity_type": rec.get("opportunity_type") or "steal",
            "gap_rmb": rec.get("gap_rmb", 0),
            # 按红线买入还能赚多少，前端排序与「强烈推荐」都用它
            "profit_at_red": rec.get("profit_at_red", 0),
            "start_price": rec.get("start_price", rec.get("lowest_price", 0)),
            "serial_badge": rec.get("serial_badge", ""),
            "bonus_str": rec.get("bonus_str", ""),
            "current_price": rec.get("current_price", 0),
            "lowest_price": rec.get("lowest_price", 0),
            "bid_count": rec.get("bid_count", 0),
            "out_count": rec.get("out_count", 0),
            "shipping_rmb": rec.get("shipping_rmb", 0),
            "safe_snipe_bid": rec.get("safe_snipe_bid", 0),
            "combined_snipe_bid": rec.get("combined_snipe_bid", 0),
            "max_buy_rmb": rec.get("max_buy_rmb", 0),
            "ebay_ref_rmb": rec.get("ebay_ref_rmb", 0),
            "realizable_rmb": rec.get("realizable_rmb", 0),
            "roi_multiple": rec.get("roi_multiple", 0),
            "competition_level": rec.get("competition_level", ""),
            "competition_label": rec.get("competition_label", ""),
            "condition_flag": bool(rec.get("condition_flag")),
            "condition_note": rec.get("condition_note", ""),
            "seller_name": rec.get("seller_name", ""),
            "seller_review": rec.get("seller_review", 0),
            "seller_risk": rec.get("seller_risk", ""),
            "remaining_minutes": remain_min,
            "end_time_str": rec.get("end_time_str", ""),
            "end_time_full": rec.get("end_time_full", ""),
            "url": rec.get("url", ""),
            "image_url": rec.get("image_url", ""),
            "scan_mode": rec.get("scan_mode", ""),
            "scan_time": rec.get("scan_time", ""),
            "scan_ts": scan_ts,
            "deadline_ts": deadline_ts,
            # 严格以截标时刻为准，不留宽限期：
            # 一旦给 expired 加缓冲，就会出现「倒计时写着已结束、卡却还在待抢页」
            # 的矛盾状态，比偶尔少看一张更伤信任。刚截标的卡仍留在「全部/已截标」里可回看。
            "expired": deadline_ts <= now,
            "mark": mark.get("state") or "",
            "mark_time": mark.get("updated_at") or "",
            # ---- 真实成交价（截标后回填）----
            # fill_price 有值 = 这张已经截标且抓到了成交价，比任何估算都可靠
            "fill_price": (fills.get(item_id) or {}).get("final_price"),
            "fill_bid_count": (fills.get(item_id) or {}).get("bid_count_final"),
            "fill_unsold": bool((fills.get(item_id) or {}).get("is_unsold")),
            "fill_closed_ts": (fills.get(item_id) or {}).get("closed_at"),
            # 追踪中：已登记、还没到回填时间
            "fill_pending": item_id in tracked and item_id not in fills,
            "from_fill": False,
        })

    # ---- 补一行：已截标、有真实成交价，但已经掉出最新一代流水的卡 ----
    # hits.jsonl 是「这一代扫到了什么」的快照，下一轮扫描后上一代就整批消失。
    # 于是出现最讽刺的局面：恰恰是**没抢到的那些卡**（已经离场）成交价最看不到，
    # 而留在榜上的都是还没截标的卡 —— filled 恒等于 0。
    # 但 closed_fills.jsonl 记的是**事实**（真实截标价），不是判断，
    # 不受 scan_id 代次隔离的约束，可以放心补回来。它是唯一能攒下市场感的样本池。
    have_ids = {h["item_id"] for h in hits}
    fill_floor = now - RADAR_FILLS_KEEP_HOURS * 3600
    extra = []
    for iid, rec in fills.items():
        if iid in have_ids:
            continue
        closed_ts = int(rec.get("closed_at") or 0)
        if closed_ts < fill_floor:
            continue
        # 没抓到成交价的记录没有任何展示价值（页面上就是个「已结束」空壳）
        if rec.get("final_price") is None:
            continue
        extra.append((closed_ts, iid, rec))
    # 时间新的优先，超量就只留最近的
    extra.sort(key=lambda x: -x[0])
    for closed_ts, iid, rec in extra[:RADAR_FILLS_MAX]:
        final_price = rec.get("final_price")
        deadline_ts = int(rec.get("deadline_ts") or closed_ts or now)
        mark = marks.get(iid) or {}
        ref_rmb = float(rec.get("ref_ebay_rmb") or 0)
        hits.append({
            "item_id": iid,
            "player_cn": rec.get("player_cn", ""),
            "player_en": rec.get("player_en", ""),
            "sport": rec.get("sport", ""),
            "title": rec.get("title", ""),
            # 成交流水里没存代卖商家，留空而不是编一个，前端合邮统计会自动跳过
            "consignor": "",
            "tier_desc": rec.get("tier_desc", ""),
            "tier_key": rec.get("tier_key", ""),
            "tier_basis": rec.get("tier_basis", ""),
            "opportunity_type": rec.get("opportunity_type") or "steal",
            "gap_rmb": 0,
            "profit_at_red": 0,
            # 已截标的卡「现价」就是终局价，别再显示扫描那一刻的旧价
            "start_price": rec.get("price_at_scan", 0),
            "serial_badge": "",
            "bonus_str": "",
            "current_price": final_price if final_price is not None else rec.get("price_at_scan", 0),
            "lowest_price": final_price if final_price is not None else rec.get("price_at_scan", 0),
            "bid_count": rec.get("bid_count_final") or rec.get("bid_at_scan", 0),
            "out_count": 0,
            "shipping_rmb": 0,
            "safe_snipe_bid": rec.get("max_buy_rmb", 0),
            "combined_snipe_bid": rec.get("max_buy_rmb", 0),
            "max_buy_rmb": rec.get("max_buy_rmb", 0),
            "ebay_ref_rmb": ref_rmb,
            "realizable_rmb": 0,
            "roi_multiple": 0,
            "competition_level": "",
            "competition_label": "",
            "condition_flag": False,
            "condition_note": "",
            "seller_name": "",
            "seller_review": 0,
            "seller_risk": "",
            "remaining_minutes": 0,
            "end_time_str": "",
            "end_time_full": "",
            "url": RADAR_ITEM_URL_TPL.format(item_id=iid),
            # 流水里没存图；成交价才是这张行存在的理由，缩略图位空着不影响判断
            "image_url": "",
            "scan_mode": "fill",
            "scan_time": "",
            "scan_ts": deadline_ts,
            "deadline_ts": deadline_ts,
            "expired": True,
            "mark": mark.get("state") or "",
            "mark_time": mark.get("updated_at") or "",
            "fill_price": final_price,
            "fill_bid_count": rec.get("bid_count_final"),
            "fill_unsold": bool(rec.get("is_unsold")),
            "fill_closed_ts": closed_ts,
            "fill_pending": False,
            "from_fill": True,
        })

    # 同一代卖商家今晚命中 >= 2 张 → 合邮。雷达的 enrich 在写流水之后才跑，
    # 所以这里按 consignor 重新统计一遍，网页端才不会丢掉这个省钱提示。
    # 已截标的卡不参与统计：它已经抢不了了，算进去只会虚报合邮收益。
    seller_count = {}
    for hit in hits:
        if hit["expired"]:
            continue
        name = hit["consignor"]
        if name and name != "独立卖家":
            seller_count[name] = seller_count.get(name, 0) + 1
    for hit in hits:
        hit["same_seller_count"] = 0 if hit["expired"] else seller_count.get(hit["consignor"], 1)

    # 待抢的排前面；真漏 → 准漏 → 起拍观察，同组内按变现倍数从高到低
    type_rank = {"steal": 0, "near_miss": 1, "blind": 2}
    hits.sort(key=lambda h: (h["expired"], h["mark"] in MARK_STATES,
                             type_rank.get(h["opportunity_type"], 3),
                             -float(h["roi_multiple"] or 0)))
    return hits


@app.get("/api/radar/hits")
async def api_radar_hits():
    hits = _radar_hits_view()
    status = _radar_status()
    now = time.time()

    # 「待抢」只算真能出手的：起拍观察（¥1/0 出价）要盲拍，单独计数
    actionable = [h for h in hits if not h["expired"] and h["mark"] not in MARK_STATES]
    open_hits = [h for h in actionable if h["opportunity_type"] != "blind"]
    fresh = [h for h in open_hits if h["competition_level"] in ("cold", "watching")]
    steals = [h for h in open_hits if h["opportunity_type"] != "near_miss"]
    near = [h for h in open_hits if h["opportunity_type"] == "near_miss"]
    blind = [h for h in actionable if h["opportunity_type"] == "blind"]

    return {
        "hits": hits,
        "status": status,
        "scan_state": _radar_scan_state(),
        "status_age_min": int((now - status["scan_ts"]) / 60) if status.get("scan_ts") else None,
        "counts": {
            "open": len(open_hits),
            "steal": len(steals),                # 真漏：出价过，且现价仍低于红线
            "near_miss": len(near),              # 准漏：差一点点，价格一松就成漏
            "blind": len(blind),                 # 起拍观察：¥1 起拍 0 出价，要盲拍
            "fresh": len(fresh),                 # 无人问津 / 有人围观但没出价，才是真机会
            "expired": sum(1 for h in hits if h["expired"]),
            "done": sum(1 for h in hits if h["mark"] == "done"),
            "ignore": sum(1 for h in hits if h["mark"] == "ignore"),
            "total": len(hits),
            # 已抓到真实成交价的张数 —— 这是「雷达准不准」的样本量
            "filled": sum(1 for h in hits if h["fill_price"] is not None),
        },
        "learning": _radar_learning_view(),
        "server_ts": int(now),
        "radar_dir_ready": os.path.isdir(RADAR_DIR),
    }


def _radar_learning_view() -> dict:
    """把「我学到了什么」摊开给用户看，而不是黑箱。

    包含：反馈条数、已生效拦截数、待确认的排除词建议、实盘校准档位数。
    """
    learned = _read_json_file(RADAR_LEARNED)
    calib = _read_json_file(RADAR_CALIB)
    approvals = _load_approvals()
    approved = approvals.get("approved") or {}
    rejected = approvals.get("rejected") or {}

    # 建议列表：过滤掉已确认/已拒绝的，只留待处理
    pending = []
    for player_cn, items in (learned.get("suggest_exclude") or {}).items():
        done = set(approved.get(player_cn) or []) | set(rejected.get(player_cn) or [])
        for item in items:
            if item.get("word") in done:
                continue
            pending.append({"player_cn": player_cn, "word": item.get("word"),
                            "count": item.get("count", 0)})
    pending.sort(key=lambda x: -int(x.get("count") or 0))

    tiers = calib.get("tiers") or {}
    g = calib.get("global") or {}

    # 非签字名单（全局）：待确认 + 已生效
    non_auto_pending = learned.get("suggest_non_auto") or []
    non_auto_approved = approvals.get("approved_non_auto") or []

    # 人工「档位偏高」：已经真的乘到参照价上，所以必须让人看见乘了多少
    penalties = sorted((learned.get("tier_penalty") or {}).values(),
                       key=lambda t: -int(t.get("count") or 0))

    return {
        "feedback_total": int(learned.get("feedback_total") or 0),
        "blocked_count": int(learned.get("blocked_count") or 0),
        "pending_exclude": pending[:12],
        "pending_total": len(pending),
        "approved_exclude": approved,
        "fills_total": int(calib.get("fills_total") or 0),
        "fills_used": int(calib.get("fills_used") or 0),
        "calib_tiers": sorted(tiers.values(), key=lambda t: -int(t.get("samples") or 0))[:8],
        "calib_min_samples": int(calib.get("min_samples") or 3),
        # 全局折价：跨档位合并出来的平台级倍数（档位样本不足时兜底）
        "calib_global": {
            "factor": g.get("factor"),
            "samples": int(g.get("samples") or 0),
            "min_samples": int(g.get("min_samples") or 4),
            "floor": g.get("floor"),
            "median_ratio": g.get("median_final_over_ref"),
        },
        # ---- 特卡冒充签字（全局名单）----
        "non_auto_pending": [w for w in non_auto_pending
                             if w.get("word") not in non_auto_approved][:8],
        "non_auto_approved": non_auto_approved,
        # ---- 人工判定档位偏高 ----
        "tier_penalties": penalties[:8],
        "tier_penalty_total": len(penalties),
        # ---- 编号存疑（供人工看规律）----
        "serial_suspect": (learned.get("serial_suspect") or [])[-6:],
        "serial_suspect_total": len(learned.get("serial_suspect") or []),
        "reasons": list(FEEDBACK_REASONS),
        "reason_hints": dict(REASON_HINTS),
        "default_reason_hint": DEFAULT_REASON_HINT,
    }


@app.post("/api/radar/rescan")
async def api_radar_rescan():
    """网页点「刷新」→ 真的让雷达重新抓一遍。

    原来的刷新只是把旧 JSONL 再读一次，雷达一天只扫 17:00 / 19:20 两轮，
    价格早就脱节了。这里投递一份请求文件，雷达守护进程 20 秒内取走并跑一轮
    manual 扫描（manual 只更新网页、不推微信，免得刷一次响一次）。
    """
    if not os.path.isdir(RADAR_DIR):
        return {"status": "unavailable",
                "message": "雷达数据目录没挂上，请检查 docker-compose 的只读挂载"}

    state = _radar_scan_state()
    now = time.time()

    if state["running"]:
        elapsed = int(now - state["started_ts"]) if state["started_ts"] else 0
        return {"status": "busy", "message": "雷达正在扫描中（已 %.0f 秒）" % elapsed,
                "scan_state": state}

    # 连点保护：45 秒内不重复投递，否则 68 人的抓取队列会被点成雪崩
    if state["request_ts"] and now - state["request_ts"] < RESCAN_COOLDOWN_SEC:
        wait = int(RESCAN_COOLDOWN_SEC - (now - state["request_ts"]))
        return {"status": "cooldown",
                "message": "刚刚已经投递过刷新请求，%d 秒后可再试" % max(wait, 1),
                "scan_state": state}

    try:
        req_id = _write_rescan_request()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="写入刷新请求失败：%s" % exc)

    return {"status": "queued", "req_id": req_id, "message": "已通知雷达，正在抓取…",
            "scan_state": _radar_scan_state()}


@app.post("/api/radar/mark")
async def api_radar_mark(item_id: str = Form(...), state: str = Form("")):
    """标记拍品状态。state 为空 = 清除标记。
    只写 radar_marks 表，绝不触碰 cards 表。"""
    item_id = (item_id or "").strip()
    state = (state or "").strip().lower()
    if not item_id:
        raise HTTPException(status_code=400, detail="缺少 item_id")
    if state and state not in MARK_STATES:
        raise HTTPException(status_code=400, detail="state 只能是 done / ignore / 空")

    conn = get_db()
    cursor = conn.cursor()
    if state:
        cursor.execute("""
            INSERT INTO radar_marks (item_id, state, note, updated_at)
            VALUES (?, ?, '', ?)
            ON CONFLICT(item_id) DO UPDATE SET state = excluded.state,
                                               updated_at = excluded.updated_at
        """, (item_id, state, now_str()))
    else:
        cursor.execute("DELETE FROM radar_marks WHERE item_id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "item_id": item_id, "state": state}


# ------------------------------------------------------------------ 反馈学习
# 「有误」按钮落库到 radar_ctl/feedback.jsonl，雷达以只读方式取走并分级学习。
# 与 radar_marks 分开：marks 是"我怎么处置这张卡"（已处理/忽略），
# feedback 是"雷达判断错了"（喂给算法）。混在一起会让两者语义互相污染。

FEEDBACK_FILE = os.path.join(RADAR_CTL_DIR, "feedback.jsonl")
APPROVALS_FILE = os.path.join(RADAR_CTL_DIR, "approvals.json")

# 与雷达端 FEEDBACK_REASONS 保持一致。
# 每一类都必须有**真实后果**，否则点了跟没点一样 —— 那比不提供选项更伤信任。
# 「档位偏高」原来只是个记录项，用户抱怨"很多时候只能选它"，说明它必须真的生效：
# 现在按 (球员, 档位) 累计人工投票，每次 ×0.9，地板 0.60。
FEEDBACK_REASONS = ("认错人", "不是单卡", "不是签字卡", "编号认错",
                    "档位偏高", "重复/已下架")

# 弹层里每类原因后面跟的一句"会发生什么"，让人知道自己点下去换来的是什么
REASON_HINTS = {
    "认错人": "提炼该球员的排除词，下次就能自动拦住同姓不同人",
    "不是单卡": "记为整包/凑套，不再按单卡估价",
    "不是签字卡": "把它当成特卡冒充签字，学出这类系列名，以后只按折射轨估",
    "编号认错": "记进编号存疑清单（连解析片段一起），用来修解析规则",
    "档位偏高": "这个球员这个档位的参照价从此每次 -10%，最低降到 60%",
    "重复/已下架": "这张永不再报",
}
DEFAULT_REASON_HINT = "这张永不再报"


def _append_jsonl(path: str, payload: dict) -> None:
    """append-only 写入。加换行边界，避免两个请求交错时拼出半截 JSON。"""
    line = json.dumps(payload, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


@app.post("/api/radar/report")
async def api_radar_report(item_id: str = Form(...), reason: str = Form(...),
                           title: str = Form(""), player_cn: str = Form(""),
                           tier_desc: str = Form(""), tier_key: str = Form(""),
                           note: str = Form("")):
    """把一张误判写进反馈流水。雷达下一轮扫描时读取并学习。

    只写 radar_ctl 下的反馈文件，绝不触碰雷达数据目录（那边是 :ro 挂载）。
    """
    item_id = (item_id or "").strip()
    reason = (reason or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="缺少 item_id")
    if reason not in FEEDBACK_REASONS:
        raise HTTPException(status_code=400, detail="reason 不合法：%s" % reason)

    # 同一张卡重复点只保留最新一条，避免把学习信号刷成噪音
    existing = []
    if os.path.isfile(FEEDBACK_FILE):
        for rec in _read_jsonl(FEEDBACK_FILE):
            if str(rec.get("item_id") or "") != item_id:
                existing.append(rec)
    existing.append({
        "ts": int(time.time()),
        "item_id": item_id,
        "reason": reason,
        "title": (title or "")[:200],
        "player_cn": (player_cn or "")[:40],
        "tier_desc": (tier_desc or "")[:60],
        # tier_key 是「档位偏高」的聚合维度，缺了它人工惩罚就没法落到具体档上
        "tier_key": (tier_key or "")[:40],
        "note": (note or "")[:200],
        "source": "web",
    })
    tmp = FEEDBACK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for rec in existing:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, FEEDBACK_FILE)
    return {"status": "success", "item_id": item_id, "reason": reason,
            "feedback_total": len(existing)}


def _load_approvals() -> dict:
    if not os.path.isfile(APPROVALS_FILE):
        return {"approved": {}, "rejected": {}, "approved_non_auto": []}
    try:
        with open(APPROVALS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        return {"approved": {}, "rejected": {}, "approved_non_auto": []}
    data.setdefault("approved", {})
    data.setdefault("rejected", {})
    data.setdefault("approved_non_auto", [])
    return data


@app.post("/api/radar/approve")
async def api_radar_approve(player_cn: str = Form(""), word: str = Form(...),
                            action: str = Form("approve"), kind: str = Form("exclude")):
    """确认/拒绝一条学习建议。

    kind="exclude"  → 该球员的排除词（认错人 / 不是单卡）
    kind="non_auto" → 全局「特卡冒充签字」名单（不是签字卡）
                     系列名不隶属某位球员，所以单独一个全局列表；
                     采纳后这类卡照常报，只是不走签字价 —— 降档不拦卡。
    """
    player_cn = (player_cn or "").strip()
    word = (word or "").strip().lower()
    if not word:
        raise HTTPException(status_code=400, detail="缺少 word")
    if kind not in ("exclude", "non_auto"):
        raise HTTPException(status_code=400, detail="kind 只能是 exclude / non_auto")
    if kind == "exclude" and not player_cn:
        raise HTTPException(status_code=400, detail="缺少 player_cn")
    if action not in ("approve", "reject", "undo"):
        raise HTTPException(status_code=400, detail="action 只能是 approve/reject/undo")

    data = _load_approvals()

    if kind == "non_auto":
        bucket = data["approved_non_auto"]
        if word in bucket:
            bucket.remove(word)
        if action in ("approve", "reject"):
            # reject 与 undo 都表现为"不在 approved_non_auto 里"：
            # 建议列表由雷达每次重建，拒绝结果靠 approvals 的 rejected 侧留痕没必要，
            # 只要不落地生效即可。
            if action == "approve":
                bucket.append(word)
        data["approved_non_auto"] = bucket
        tmp = APPROVALS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, APPROVALS_FILE)
        return {"status": "success", "kind": kind, "word": word, "action": action,
                "approved_non_auto": data["approved_non_auto"]}

    for bucket_name in ("approved", "rejected"):
        words = data[bucket_name].get(player_cn) or []
        if word in words:
            words.remove(word)
        if words:
            data[bucket_name][player_cn] = words
        else:
            data[bucket_name].pop(player_cn, None)

    if action in ("approve", "reject"):
        bucket_name = "approved" if action == "approve" else "rejected"
        data[bucket_name].setdefault(player_cn, [])
        if word not in data[bucket_name][player_cn]:
            data[bucket_name][player_cn].append(word)

    tmp = APPROVALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, APPROVALS_FILE)
    return {"status": "success", "kind": kind, "player_cn": player_cn, "word": word,
            "action": action, "approved": data["approved"]}

# ============================ 视觉识图建档与交互式拉正/居中度 API ============================

@app.post("/api/ai/vision-archive")
async def vision_archive(file: UploadFile = File(...)):
    """上传卡片正面照片，调用 Scrapling 容器 (8090) 的视觉模型进行卡面全要素智能建档识别"""
    raw = await file.read()
    if not raw:
        return {"status": "error", "message": "上传文件为空"}

    b64_str = base64.b64encode(raw).decode("utf-8")

    import urllib.request
    import urllib.error

    url = f"{SCRAPLING_API_BASE.rstrip('/')}/api/ai/vision-card"
    req_body = json.dumps({"image_b64": b64_str}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8")
            res_json = json.loads(content)
            parsed = res_json.get("parsed") or {}

            sport = parsed.get("sport") or "篮球"
            if any(k in sport for k in ["篮球", "Basket"]):
                sport = "篮球"
            elif any(k in sport for k in ["足球", "Foot", "Soccer"]):
                sport = "足球"
            elif any(k in sport for k in ["网球", "Tennis"]):
                sport = "网球"
            elif any(k in sport for k in ["棒球", "Base"]):
                sport = "棒球"

            player = parsed.get("player") or ""
            year = str(parsed.get("year") or "").strip()
            set_name = str(parsed.get("set_name") or "").strip()
            year_set = f"{year} {set_name}".strip()

            card_set = str(parsed.get("card_set") or "").strip()
            parallel = str(parsed.get("parallel") or "").strip()
            is_auto = bool(parsed.get("is_auto"))
            is_patch = bool(parsed.get("is_patch"))
            card_type_parts = []
            if card_set and card_set.lower() != "base":
                card_type_parts.append(card_set)
            if parallel and parallel not in card_type_parts:
                card_type_parts.append(parallel)
            if is_auto:
                card_type_parts.append("亲签")
            if is_patch:
                card_type_parts.append("物料切片")
            card_type = " ".join(card_type_parts)

            s_num = parsed.get("serial_num")
            s_total = parsed.get("serial_total")
            serial_str = ""
            if s_total:
                serial_str = f"/{s_total}"
                if s_num:
                    serial_str = f"{s_num}/{s_total}"
            elif s_num:
                serial_str = f"#{s_num}"

            graded = str(parsed.get("graded") or "Raw").strip()
            verdict = ""
            if graded and graded.lower() != "raw":
                verdict = f"【评级卡】{graded}"

            query = parsed.get("search_query") or build_search_query(sport, player, year_set, card_type, serial_str)

            return {
                "status": "success",
                "raw_parsed": parsed,
                "fields": {
                    "sport": sport,
                    "player": player,
                    "year_set": year_set,
                    "card_type": card_type,
                    "serial": serial_str,
                    "grading_verdict": verdict,
                    "search_query": query
                }
            }
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return {"status": "error", "message": f"视觉模型响应错误 (HTTP {e.code}): {err_msg[:200]}"}
    except urllib.error.URLError as e:
        return {"status": "error", "message": f"无法连接视觉解析服务 ({e.reason})，请确认 8090 容器运行中"}
    except Exception as e:
        return {"status": "error", "message": f"视觉解析异常: {str(e)}"}


@app.get("/api/cards/{card_id}/corners")
def get_card_corners(card_id: str, side: str = "front"):
    """获取卡片原图尺寸及自动检测的 4 个角点坐标，用于前端交互式四角拉正"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="卡片不存在")

    side = "back" if str(side).lower() == "back" else "front"
    img_col, orig_col = f"img_{side}", f"img_{side}_orig"
    source = row[orig_col] or row[img_col]
    if not source:
        raise HTTPException(status_code=400, detail="该面还没有实拍图")

    path = os.path.join(UPLOAD_DIR, os.path.basename(str(source)))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="原图文件未找到")

    with open(path, "rb") as handle:
        raw = handle.read()

    try:
        import importlib
        importlib.reload(photos)
    except Exception:
        pass
    corners_info = photos.detect_corners(raw)
    return {
        "status": "success",
        "card_id": card_id,
        "side": side,
        "source_url": f"/uploads/{os.path.basename(str(source))}",
        **corners_info
    }


@app.post("/api/cards/{card_id}/perspective-crop")
async def perspective_crop_card(card_id: str, request: Request):
    """根据前端传入的 4 个微调角点，执行精准透视矫正并保存为当前展示图"""
    try:
        data = await request.json()
    except Exception:
        data = {}

    side = "back" if str(data.get("side", "front")).lower() == "back" else "front"
    corners = data.get("corners")
    rotate = int(data.get("rotate", 0))

    if not corners or len(corners) != 4:
        raise HTTPException(status_code=400, detail="必须提供 4 个角点坐标")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    img_col, orig_col, rot_col = f"img_{side}", f"img_{side}_orig", f"rotate_{side}"
    source = row[orig_col] or row[img_col]
    if not source:
        conn.close()
        raise HTTPException(status_code=400, detail="该面还没有实拍图")

    path = os.path.join(UPLOAD_DIR, os.path.basename(str(source)))
    if not os.path.isfile(path):
        conn.close()
        raise HTTPException(status_code=404, detail="源图文件未找到")

    with open(path, "rb") as handle:
        raw = handle.read()

    try:
        try:
            import importlib
            importlib.reload(photos)
        except Exception:
            pass
        res = photos.warp_quad_manual(raw, corners, rotate=rotate)
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=400, detail=f"透视变换失败: {exc}")

    active_img_name = row[img_col] or f"{card_id}_{side}.jpg"
    final_name = _write_atomic(active_img_name, res["data"])

    cursor.execute(
        f"UPDATE cards SET {img_col} = ?, {rot_col} = ?, updated_at = ? WHERE card_id = ?",
        (final_name, rotate, now_str(), card_id)
    )
    conn.commit()
    conn.close()

    _backup_db()

    # warp_quad_manual 只保证返回 data/width/height 三个键。
    # 这里原先直接写 res["aspect"] —— 该键根本不存在（前端也从没用过），
    # 于是每一次「智能拉正」都在最后一行抛 KeyError，被 Starlette 兜成
    # 纯文本 500，前端 res.json() 只能报 "Unexpected token 'I'"。
    # 与本文件其它两处一致：用 .get 兜底，缺失时按宽高自行推算。
    _w = res.get("width") or 0
    _h = res.get("height") or 0
    _aspect_fallback = round(min(_w, _h) / max(_w, _h), 4) if (_w and _h) else 0.0
    return {
        "status": "success",
        "card_id": card_id,
        "side": side,
        "img": final_name,
        "width": res.get("width", 0),
        "height": res.get("height", 0),
        "aspect": res.get("aspect", _aspect_fallback),
        "new_url": f"/uploads/{final_name}?t={int(time.time())}"
    }


@app.get("/api/cards/{card_id}/centering")
def get_card_centering(card_id: str, side: str = "front"):
    """自动探测卡片内边框并计算居中度比例与评级预估"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="卡片不存在")

    side = "back" if str(side).lower() == "back" else "front"
    img_col = f"img_{side}"
    source = row[img_col]
    if not source:
        raise HTTPException(status_code=400, detail="该面还没有展示图")

    path = os.path.join(UPLOAD_DIR, os.path.basename(str(source)))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="展示图文件未找到")

    with open(path, "rb") as handle:
        raw = handle.read()

    centering_info = photos.auto_detect_borders(raw)
    return {
        "status": "success",
        "card_id": card_id,
        "side": side,
        "image_url": f"/uploads/{os.path.basename(str(source))}?t={int(time.time())}",
        **centering_info
    }


@app.post("/api/cards/{card_id}/calculate-centering")
async def calculate_centering_endpoint(card_id: str, request: Request):
    """根据拖拽调整后的边框像素宽度，实时重算居中度比例与评级"""
    try:
        data = await request.json()
    except Exception:
        data = {}

    left = data.get("left", 50)
    right = data.get("right", 50)
    top = data.get("top", 50)
    bottom = data.get("bottom", 50)

    res = photos.calculate_centering(left, right, top, bottom)
    return {"status": "success", **res}


@app.post("/api/cards/{card_id}/save-centering")
async def save_card_centering(card_id: str, request: Request):
    """将居中度测量结果一键写入卡片的评级建议备注 (grading_verdict)"""
    try:
        data = await request.json()
    except Exception:
        data = {}

    verdict_text = (data.get("verdict_text") or "").strip()
    if not verdict_text:
        raise HTTPException(status_code=400, detail="评级备注内容不能为空")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT grading_verdict FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="卡片不存在")

    old_verdict = (row["grading_verdict"] or "").strip()
    if old_verdict and verdict_text not in old_verdict:
        cleaned_old = re.sub(r"【居中度预评】.*?(?=(【|$))", "", old_verdict, flags=re.DOTALL).strip()
        merged = f"{verdict_text}\n{cleaned_old}".strip() if cleaned_old else verdict_text
    else:
        merged = verdict_text

    cursor.execute("UPDATE cards SET grading_verdict = ?, updated_at = ? WHERE card_id = ?",
                   (merged, now_str(), card_id))
    conn.commit()
    conn.close()

    _export_vault_inventory()
    _backup_db()
    return {"status": "success", "card_id": card_id, "grading_verdict": merged}