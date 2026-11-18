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

    历史教训：本服务连续两次因未捕获异常返回纯文本 500，前端 res.json()
    只能报 "Unexpected token 'I' ... is not valid JSON"，真实原因被彻底遮住，
    每次都要进容器翻日志才能定位。改成 JSON 后前端能直接显示 detail。
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
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="app/templates")

DB_PATH = os.path.join(DATA_DIR, "cards.db")

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
# 容器内访问部署机上的 Scrapling 助手：默认回环，部署到别的机器时用环境变量覆盖，
# 不要把某一台内网机的地址写进源码。
SCRAPLING_API_BASE = os.getenv("SCRAPLING_API_BASE", "http://127.0.0.1:8091")

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
    # ---- v2.1 评级与来源字段 ----
    # 对齐 Ludex / Card Ladder 的分级筛选维度。PSA 10 与同款裸卡常有数倍价差，
    # 不区分评级状态，公允估值就永远算不准。
    "grader": "TEXT",                  # PSA / BGS / SGC / CGC / ACE；空=裸卡
    "grade_score": "REAL DEFAULT 0",   # 10 / 9.5 / 9 ...；0=未评级
    "is_rookie": "INTEGER DEFAULT 0",  # Rookie 新秀卡
    "is_auto": "INTEGER DEFAULT 0",    # 签字卡
    "is_patch": "INTEGER DEFAULT 0",   # Relic / Patch 物料卡
    "marketplace": "TEXT",             # 来源市场：卡淘 / eBay / 其他
    # ---- v2.4 版别与卡号 ----
    # base 与银折/金折价差常有 10-40 倍，不分开就永远算不准。
    "parallel": "TEXT",                # base / silver / gold / 手绘 ...；空=未标注
    "card_number": "TEXT",             # 卡号（如 136），用于钉死 checklist
    # v3.4 估值可信度：ok / insufficient / manual（空=从未重估）
    "valuation_status": "TEXT",
    "valuation_note": "TEXT",
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
            "       est_raw_price, search_query, notes, img_back, parallel, grader, grade_score FROM cards "
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
        # 定价口径只有两种：有同款成交 → 最近一笔成交价；没有 → 保留人工估值。
        # 前端据此直接显示「最近同款成交 ¥X（时间）」或「无同款成交记录 · 保留人工估值」，
        # 不允许再显示任何中位数。
        "price_basis": ("latest_sale" if float(row.get("latest_rmb") or 0) > 0
                        else "insufficient"),
        "latest_sale_date": str(row.get("latest_closed") or ""),
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


@app.get("/healthz")
def healthz():
    """健康检查接口：探测数据库可用性。"""
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"数据库不可用: {exc}")
    return {"status": "ok"}


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


def _clean_price(raw):
    """把前端可能传来的任何价格写法清洗成 float；无法清洗返回 None。

    历史 bug：路由签名原来是 `price: Optional[float] = Form(None)`，
    FastAPI 会在进函数之前就按 float 校验表单值，于是前端一旦传
    "undefined" / "¥1,200" / 空字符串，请求直接 422，函数内的清洗逻辑
    根本没机会执行，用户看到的就是「采纳失败: 未知错误」。
    现在签名收成 Optional[str]，所有清洗都在函数内完成。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if (v == v and v not in (float("inf"), float("-inf"))) else None
    s = str(raw).strip()
    if not s or s.lower() in ("undefined", "null", "none", "nan"):
        return None
    for junk in ("¥", "￥", "$", ",", "，", "元", "RMB", "rmb",
                 " ", "\u3000", "\xa0", "：", ":"):
        s = s.replace(junk, "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


@app.post("/api/cards/{card_id}/apply-quote")
async def apply_quote(card_id: str, request: Request, price: Optional[str] = Form(None)):
    """「⚡ 采纳为当前估值」——由行情改写 est_raw_price 的入口。

    支持 Form 表单、JSON 或 Query 参数，并自动清洗数值（去 ¥ 符号、千分位符）。
    """
    val_price = _clean_price(price)
    if val_price is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                for key in ("price", "value", "amount", "est_raw_price"):
                    if key in body:
                        val_price = _clean_price(body[key])
                        if val_price is not None:
                            break
        except Exception:
            pass
    if val_price is None:
        try:
            form = await request.form()
            for key in ("price", "value", "amount", "est_raw_price"):
                if key in form:
                    val_price = _clean_price(form[key])
                    if val_price is not None:
                        break
        except Exception:
            pass
    if val_price is None:
        for key in ("price", "value", "amount"):
            raw = request.query_params.get(key)
            if raw is not None:
                val_price = _clean_price(raw)
                if val_price is not None:
                    break

    if val_price is None or not (val_price > 0):
        return JSONResponse(
            status_code=400,
            content={"status": "error",
                     "message": "估价必须是大于 0 的数字（没收到有效价格）",
                     "detail": "估价必须是大于 0 的数字"}
        )

    try:
        conn = get_db()
    except sqlite3.Error as exc:
        logger.warning("[采纳估值] 打开数据库失败 %s: %s", card_id, exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "数据库暂时不可用",
                     "detail": "数据库暂时不可用：%s" % exc}
        )

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT est_raw_price FROM cards WHERE card_id = ?", (card_id,))
        row = cursor.fetchone()
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"status": "error", "message": "卡片不存在",
                         "detail": "卡片不存在"}
            )
        old_price = float(row["est_raw_price"] or 0)

        cursor.execute(
            "UPDATE cards SET est_raw_price = ?, valuation_status = 'ok', "
            "valuation_note = '用户采纳估值', updated_at = ? WHERE card_id = ?",
            (round(float(val_price), 2), now_str(), card_id))
        # 顺带留存行情历史记录（表不存在也不影响主流程）
        try:
            cursor.execute(
                "INSERT INTO card_price_history "
                "(card_id, timestamp, median_rmb, sample_count, basis) "
                "VALUES (?, ?, ?, 1, 'applied_quote')",
                (card_id, now_str(), round(float(val_price), 2)))
        except sqlite3.Error:
            pass
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("[采纳估值] 写入失败 %s: %s", card_id, exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error",
                     "message": "采纳失败：%s" % exc,
                     "detail": "采纳失败：%s" % exc}
        )
    finally:
        conn.close()

    # 这两步都是"顺手"动作，内部各自兜住异常，不能让用户看到失败
    _export_vault_inventory()
    _backup_db()
    return {
        "status": "success",
        "message": f"已成功采纳估值 ¥{float(val_price):.2f}",
        "card_id": card_id,
        "old_price": old_price,
        "est_raw_price": round(float(val_price), 2)
    }


@app.post("/api/ai/parse-title")
async def parse_title(request: Request):
    """调用 Scrapling 容器 (8091) 的 DeepSeek AI 服务解析球卡长标题，返回结构化字段"""
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
        return {"status": "error", "message": f"无法连接 AI 解析服务 ({e.reason})，请确认 8091 容器及 PC 网关运行中"}
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
    """上传卡片正面照片，调用 Scrapling 容器 (8091) 的视觉模型进行卡面全要素智能建档识别"""
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
        return {"status": "error", "message": f"无法连接视觉解析服务 ({e.reason})，请确认 8091 容器运行中"}
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
    return {
        "status": "success",
        "card_id": card_id,
        "side": side,
        "img": final_name,
        "width": res["width"],
        "height": res["height"],
        "aspect": res.get("aspect") or (round(min(res["width"], res["height"]) / max(res["width"], res["height"]), 4) if res.get("width") and res.get("height") else 0.0),
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


# ==============================================================================
# v2.1 · 评级字段扩展 / 组合重估 / 价格提醒
# 沿用本项目既有约定：
#   1) 只做增量 —— MIGRATIONS 加列、独立建表，不改 cards 已有列语义
#   2) 写面收窄 —— 每类写入单独端点，不做整表覆盖
#   3) 失败不阻塞 —— 外部服务不可用时返回状态，不抛 500
# 参考对象：Ludex / Card Ladder 的分级筛选与组合估值（$20-25/月那部分，
# 其本质是"数量限制 + 数据深度"，自建系统无额度可言，直接实现即可）
# ==============================================================================

import urllib.request as _urlreq
import urllib.error as _urlerr


def _scrapling_post(path: str, payload: dict, timeout: int = 90):
    """同步调用 scrapling 服务。返回 (结果或 None, 错误说明)。"""
    url = SCRAPLING_API_BASE.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = _urlreq.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except _urlerr.HTTPError as exc:
        return None, "scrapling 返回 HTTP %s" % exc.code
    except _urlerr.URLError as exc:
        return None, "连不上 scrapling（%s）" % exc.reason
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


# ---------------------------- 评级状态与搜索过滤 ----------------------------

_GRADER_ALIASES = {
    "psa": "PSA", "bgs": "BGS", "sgc": "SGC", "cgc": "CGC",
    "ace": "ACE", "gma": "GMA", "mnt": "MNT", "bccg": "BCCG", "其他": "",
}


def _grade_tokens(grader, score) -> list:
    """把评级状态转成"成交标题里必须出现"的词元。

    PSA 10 与同款裸卡价差常有 3-10 倍，估值时若不区分，
    算出来的公允价会把两者混在一起，等于没算。
    """
    g = str(grader or "").strip()
    if not g:
        return []
    key = g.lower()
    if key in _GRADER_ALIASES:
        g = _GRADER_ALIASES[key]
        if not g:
            return []
    else:
        g = g.upper()
    try:
        s = float(score or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s > 0:
        return [g, ("%g" % s)]
    return [g]


def _filter_sales_by_grade(items, tokens):
    """结果层过滤：只保留标题同时命中评级公司与分数的成交。"""
    if not tokens:
        return items
    kept = []
    for it in items:
        title = str(it.get("title") or "").upper()
        if all(t.upper() in title for t in tokens):
            kept.append(it)
    return kept


def _player_tokens(player) -> list:
    """从 "中文名 (English Name)" 里抽出可用于匹配标题的球员标识。"""
    p = str(player or "").strip()
    if not p:
        return []
    tokens = []
    m = re.search(r"[（(]([A-Za-z][^）)]*)[）)]", p)
    if m:
        en = m.group(1).strip()
        parts = [x for x in re.split(r"\s+", en) if x]
        if len(parts) >= 2:
            tokens.append(" ".join(parts))
        if parts:
            tokens.append(parts[-1])
    cn = re.sub(r"[（(].*?[）)]", "", p).strip()
    if cn:
        tokens.append(cn)

    # 补上同义词（东契奇 / doncic / 卢卡·东契奇 这类）。
    # 不补的话，_filter_by_player 用的是"卢卡·东契奇"这种全串，
    # 而成交标题里通常只写"东契奇"，展示层就会把正确的样本全过滤掉。
    p_low = p.lower()
    for k, syns in _PLAYER_SYNONYMS.items():
        if k in p_low or any(s in p for s in syns):
            tokens.append(k)
            tokens.extend(syns)

    seen = set()
    out = []
    for t in tokens:
        t = (t or "").strip()
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _filter_by_player(items, tokens):
    """标题命中球员标识即保留（卡淘标题中英混排都有）。

    实测教训：搜索词 "2023 Crown Royale Miller /10" 命中的 6 条成交里，
    5 条是 Brandon Miller（黄蜂 2 号秀，10 万级），只有 1 条是本人的
    Leonard Miller（32 元）。按姓氏聚合会把估值算到 9.5 万，离谱两个数量级。

    所以：**全名只要命中 1 条就用全名**，宁可样本少也不能混人。
    只有全名一条都不中（标题只写姓氏）时才退化为姓氏匹配。
    样本不足由置信度字段（low/medium/high）如实标出，不用错误样本凑数。
    """
    if not tokens:
        return items, "none"
    for fn in [t for t in tokens if " " in t.strip()]:
        strict = [it for it in items
                  if fn.upper() in str(it.get("title") or "").upper()]
        if strict:
            return strict, "full_name"
    kept = []
    for it in items:
        up = str(it.get("title") or "").upper()
        if any(t.upper() in up for t in tokens):
            kept.append(it)
    return kept, "surname"


def _confidence(sample) -> str:
    """估值置信度：样本越少越不可信，前端要永远把这个标出来。"""
    try:
        n = int(sample or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 10:
        return "high"
    if n >= 5:
        return "medium"
    if n >= 1:
        return "low"
    return "none"


def _fallback_queries(query, player, year_set, parallel=""):
    """分层降级搜索词：精确 → 中 → 宽。

    实测 133 张卡里约 60% 用精确词在卡淘搜不到成交
    （如 "2026 Chrome Jovic /10" 四个条件同时命中太难）。
    降级到「球员+年份」或「球员名」通常就能召回，
    精度由后面的球员过滤保证，不需要靠搜索词硬卡。
    """
    out = [query]
    name = ""
    for t in _player_tokens(player):
        if " " not in t.strip() and len(t) >= 3:
            name = t
            break
    if not name:
        for t in _player_tokens(player):
            if " " in t.strip():
                name = t.split()[-1]
                break
    year = ""
    m = re.search(r"(19|20)\d{2}", str(year_set or ""))
    if m:
        year = m.group(0)

    if name and year:
        out.append(name + " " + year)
    if name:
        out.append(name)

    # 版别精确词：把颜色/折射名加回去，让"排在第 10 条之后"的高价成交浮上来。
    # 实测郑钦文金折 ¥2800 在宽词下排后列，加 "金折" 后直接第 1 条。
    p = str(parallel or "").strip().lower()
    if name and p and p not in ("base", "serialized", ""):
        alias = (_PARALLEL_ALIASES.get(p) or [])[:1]
        for a in alias:
            if year:
                out.append("%s %s %s" % (year, name, a))
            out.append("%s %s" % (name, a))
        if year:
            out.append("%s %s" % (year, name))

    seen = set()
    return [q for q in out if q and not (q in seen or seen.add(q))]


def _recompute_from_filtered(base_result, filtered):
    """用过滤后的同款样本重算「最近一笔成交价」。

    原来这里算的是样本中位数——已经被用户否掉：同球员同版次但编数/评级不同
    的成交，价格照样天差地别，混在一起求中位数就是在编数字。
    现在只回答一件事：这批同款里，最近的一笔成交价是多少。
    """
    def _date_of(item):
        for key in ("sold_date", "closed_str", "end_time", "date", "closed"):
            v = _norm_date(item.get(key) or "")
            if v:
                return v
        return ""

    valid = []
    for x in (filtered or []):
        try:
            p = float(x.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            valid.append((_date_of(x), p))
    if not valid:
        return None

    dated = sorted([x for x in valid if x[0]], key=lambda x: x[0], reverse=True)
    ordered = dated + [x for x in valid if not x[0]]
    prices = sorted(p for _, p in valid)
    return {
        "sample": len(valid),
        # latest 是新的定价依据；median 键只为兼容老前端保留，值同样是最近成交价
        "latest": round(ordered[0][1], 2),
        "median": round(ordered[0][1], 2),
        "latest_date": ordered[0][0],
        "min": round(prices[0], 2),
        "max": round(prices[-1], 2),
    }


def _write_price_history(conn, card_id, value, sample, grade_bucket, basis):
    """留档一次估值，供走势图与提醒判断使用。"""
    try:
        conn.execute(
            "INSERT INTO card_price_history "
            "(card_id, timestamp, median_rmb, sample_count, grade_bucket, window_days, basis) "
            "VALUES (?,?,?,?,?,?,?)",
            (card_id, now_str(), float(value or 0), int(sample or 0),
             str(grade_bucket or ""), 30, str(basis or "")),
        )
    except sqlite3.Error:
        pass


# ---------------------------- 评级字段（收窄写入） ----------------------------

@app.post("/api/cards/{card_id}/grade-info")
def set_grade_info(card_id: str, grader: str = Form(""),
                   grade_score: float = Form(0.0),
                   is_rookie: int = Form(0), is_auto: int = Form(0),
                   is_patch: int = Form(0), marketplace: str = Form(""),
                   parallel: str = Form(""), card_number: str = Form("")):
    """只改评级、版别与特征标签。

    单独开接口的理由与 search-query 一致：不重发整张卡的表单，
    就不会出现"改评级把刚编辑的备注覆盖回去"这类事故。

    parallel（base / silver / gold …）决定估值口径：
    base 与折射价差可达数十倍，标注后估值会自动分开算。
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")

        try:
            score = float(grade_score or 0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(score, 10.0))

        conn.execute(
            "UPDATE cards SET grader = ?, grade_score = ?, is_rookie = ?, "
            "is_auto = ?, is_patch = ?, marketplace = ?, parallel = ?, "
            "card_number = ?, updated_at = ? WHERE card_id = ?",
            (str(grader or "").strip().upper() if grader else "",
             score, 1 if is_rookie else 0, 1 if is_auto else 0, 1 if is_patch else 0,
             str(marketplace or "").strip(),
             str(parallel or "").strip(),
             str(card_number or "").strip().lstrip("#"),
             now_str(), card_id),
        )
        conn.commit()
        out = dict(conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone())
    finally:
        conn.close()

    _export_vault_inventory()
    return {"status": "success", "card": {
        "card_id": out.get("card_id"),
        "grader": out.get("grader") or "",
        "grade_score": out.get("grade_score") or 0,
        "is_rookie": out.get("is_rookie") or 0,
        "is_auto": out.get("is_auto") or 0,
        "is_patch": out.get("is_patch") or 0,
        "marketplace": out.get("marketplace") or "",
        "parallel": out.get("parallel") or "",
        "card_number": out.get("card_number") or "",
    }}


# ==============================================================================
# CardValue 核心估值与成交价对齐引擎（1:1 属性强对齐 & 同款取最新成交）
# 铁律 1：同款取最新成交（球员、年份系列、平行版别、编数、签字、评级 1:1 强对齐）
# 铁律 2：无同款坚决保留估值（绝对不粗暴折算中位数，保留人工估值并标 insufficient）
# ==============================================================================

_PLAYER_SYNONYMS = {
    "wembanyama": ["文班亚马", "文班", "斑马"],
    "alcaraz": ["阿尔卡拉斯", "阿卡"],
    "zheng": ["郑钦文"],
    "ronaldo": ["c罗", "克里斯蒂亚诺·罗纳尔多", "罗纳尔多"],
    "messi": ["梅西"],
    "curry": ["库里", "斯蒂芬·库里"],
    "james": ["詹姆斯", "勒布朗"],
    "doncic": ["东契奇", "卢卡·东契奇"],
    "jokic": ["约基奇"],
    "sinner": ["辛纳"],
}

_BRAND_PATTERNS = [
    ("panini", r"panini|帕尼尼|帕尼"),
    ("topps", r"topps|托普斯|topp\b"),
    ("futera", r"futera"),
    ("upperdeck", r"upper\s*deck|\bud\b|upper-deck"),
    ("bowman", r"bowman|波曼"),
    ("leaf", r"leaf"),
]

_BOX_PRODUCT_RE = re.compile(
    r"铂金盒|白金盒|盒子|整盒|原盒|原封|盒装|一盒|整箱|箱装|打包|box\b|blaster|mega\s*box|hobby\s*box|case\b",
    re.IGNORECASE)

_SIGN_RE = re.compile(r"签|autograph|auto\b|signature|签章|墨签|贴签|卡签|\bau\b", re.IGNORECASE)
_NO_SIGN_RE = re.compile(r"(?:无|不带|非|不是)\s*签|no\s*auto", re.IGNORECASE)

_PARALLEL_FAMILIES = {
    "red": ["red geometric", "geometric red", "red", "红折", "红几何", "红碎冰", "红波", "红波纹", "大红折", "正红折", "红宝石", "红版", "红标"],
    "gold": ["gold", "金折", "正金折", "金refractor", "金碎冰", "大金折", "正金", "金标", "金版"],
    "silver": ["silver", "银折", "银refractor", "银碎冰", "银版", "银标"],
    "blue": ["blue", "蓝折", "蓝碎冰", "蓝宝石", "正蓝折", "深蓝折", "天蓝折", "水蓝折", "蓝版"],
    "green": ["green", "绿折", "祖母绿", "正绿折", "绿碎冰", "绿版"],
    "purple": ["purple", "紫折", "正紫折", "紫碎冰", "紫版"],
    "orange": ["orange", "橙折", "橘折", "正橙折", "橙碎冰", "橙版"],
    "black": ["black", "黑折", "正黑折", "黑金", "黑版"],
    "pink": ["pink", "粉折", "正粉折", "粉碎冰", "粉版"],
    "teal": ["teal", "青折"],
    "aqua": ["aqua", "水蓝折"],
    "bronze": ["bronze", "铜折"],
    "platinum": ["platinum", "铂金折"],
    "emerald": ["emerald", "祖母绿"],
    "ruby": ["ruby", "红宝石"],
    "superfractor": ["superfractor", "超级折"],
    "shimmer": ["shimmer", "闪光"],
    "wave": ["wave", "波纹折"],
    "mojo": ["mojo"],
    "ice": ["cracked ice", "碎冰"],
    "tiger": ["tiger", "虎纹"],
    "zebra": ["zebra", "斑马"],
}

_BASE_EXPLICIT = ["base card", "base set", "base", "普卡", "基础版", "普通版"]

def _brand_of(text) -> str:
    low = str(text or "").lower()
    for name, pat in _BRAND_PATTERNS:
        if re.search(pat, low):
            return name
    return ""

def _is_signed_card(text) -> bool:
    t = str(text or "")
    if not t or _NO_SIGN_RE.search(t):
        return False
    return bool(_SIGN_RE.search(t))

def _card_wants_auto(card_type, notes="") -> bool:
    text = "%s %s" % (card_type or "", notes or "")
    if _NO_SIGN_RE.search(text):
        return False
    return bool(_SIGN_RE.search(text))

def _parse_serial(serial_str: str):
    s = str(serial_str or "").strip()
    if not s:
        return None
    if re.search(r"\b1\s*/\s*1\b|1of1|1\s*of\s*1|一编|1编|全球唯一", s, re.I):
        return "1/1"
    m = re.search(r"/\s*(\d+)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"^(\d+)$", s)
    if m:
        return int(m.group(1))
    return None

def _extract_serial_from_title(title: str):
    t = str(title or "")
    if re.search(r"\b1\s*/\s*1\b|\b1of1\b|\b1\s*of\s*1\b|一编|1编|全球唯一", t, re.I):
        return "1/1"
    m = re.search(r"(\d+)\s*编", t)
    if m:
        v = int(m.group(1))
        if 1 < v <= 5000:
            return v
    m = re.search(r"(?<!#)(?<!NO\.)(?<!NO )(?<!\d/)/\s*(\d{1,4})\b", t, re.I)
    if m:
        v = int(m.group(1))
        if 1 < v <= 5000:
            return v
    m = re.search(r"\b\d{1,4}\s*/\s*(\d{1,4})\b", t)
    if m:
        v = int(m.group(1))
        if 1 < v <= 5000:
            return v
    return None

def _get_parallel_family(text: str) -> str:
    t = str(text or "").lower()
    for be in _BASE_EXPLICIT:
        if be in t:
            return "base"
    for fam, aliases in _PARALLEL_FAMILIES.items():
        for a in aliases:
            if len(a) <= 2:
                if re.search(rf"\b{re.escape(a)}\b", t) or (a in t and not a.isascii()):
                    return fam
            elif a in t:
                return fam
    return ""

def _check_grade_match(card_grader: str, card_score: float, title: str) -> bool:
    is_card_raw = not card_grader or str(card_grader).upper() in ("RAW", "裸卡", "NONE", "") or card_score <= 0
    m = re.search(r"\b(PSA|BGS|SGC|CGC|ACE)\s*[:：#]?\s*([0-9]+(?:\.[0-9]+)?)\b", title, re.I)
    if m:
        slab_grader = m.group(1).upper()
        slab_score = float(m.group(2))
        if is_card_raw:
            return False
        return slab_grader == str(card_grader).upper() and slab_score == float(card_score)
    else:
        return is_card_raw

def _check_parallel_match(card_parallel: str, card_type: str, notes: str, title: str) -> bool:
    card_fam = _get_parallel_family(card_parallel) or _get_parallel_family(card_type) or _get_parallel_family(notes)
    if not card_fam:
        card_fam = "base"

    t_low = title.lower()
    if card_fam == "base":
        for fam, aliases in _PARALLEL_FAMILIES.items():
            for a in aliases:
                if len(a) <= 2:
                    if re.search(rf"\b{re.escape(a)}\b", t_low) or (a in t_low and not a.isascii()):
                        return False
                elif a in t_low:
                    return False
        return True
    else:
        aliases = _PARALLEL_FAMILIES.get(card_fam, [card_fam])
        matched_card_fam = False
        for a in aliases:
            if len(a) <= 2:
                if re.search(rf"\b{re.escape(a)}\b", t_low) or (a in t_low and not a.isascii()):
                    matched_card_fam = True
                    break
            elif a in t_low:
                matched_card_fam = True
                break
        if not matched_card_fam:
            return False
        for fam, other_aliases in _PARALLEL_FAMILIES.items():
            if fam == card_fam:
                continue
            for oa in other_aliases:
                if len(oa) <= 2:
                    if re.search(rf"\b{re.escape(oa)}\b", t_low) or (oa in t_low and not oa.isascii()):
                        return False
                elif oa in t_low:
                    return False
        return True

def _check_serial_match(card_serial: str, title: str) -> bool:
    cs = _parse_serial(card_serial)
    ts = _extract_serial_from_title(title)
    return cs == ts

def _check_auto_match(card_is_auto: bool, title: str) -> bool:
    ta = _is_signed_card(title)
    return bool(card_is_auto) == bool(ta)

def _check_year_set_match(year_set: str, title: str) -> bool:
    if _BOX_PRODUCT_RE.search(title):
        return False
    ys = str(year_set or "")
    t = str(title or "")

    c_brand = _brand_of(ys)
    t_brand = _brand_of(t)
    if c_brand and t_brand and c_brand != t_brand:
        return False

    c_is_sap = "蓝宝石" in ys or "sapphire" in ys.lower()
    t_is_sap = "蓝宝石" in t or "sapphire" in t.lower()
    if c_is_sap != t_is_sap:
        return False

    c_is_upd = bool(re.search(r"chrome\s*update|tcu\b", ys, re.I))
    t_is_upd = bool(re.search(r"chrome\s*update|tcu\b", t, re.I))
    if c_is_upd != t_is_upd:
        return False

    ym = re.search(r"((?:19|20)\d{2}(?:-\d{2,4})?)", ys)
    if ym:
        c_year = ym.group(1)
        t_years = re.findall(r"(?:19|20)\d{2}", t)
        c_years = re.findall(r"(?:19|20)\d{2}", c_year)
        if t_years and c_years:
            if not any(cy in t_years for cy in c_years):
                return False
    return True

def _check_player_match(player: str, title: str) -> bool:
    p = str(player or "").strip()
    if not p:
        return True
    t = str(title or "")
    t_up = t.upper()

    tokens = []
    m = re.search(r"[（(]([A-Za-z][^）)]*)[）)]", p)
    if m:
        en = m.group(1).strip()
        parts = [x for x in re.split(r"\s+", en) if x]
        if len(parts) >= 2:
            tokens.append(" ".join(parts))
        if parts:
            tokens.append(parts[-1])
    cn = re.sub(r"[（(].*?[）)]", "", p).strip()
    if cn:
        tokens.append(cn)

    parts = [x for x in re.split(r"\s+", p) if x]
    if len(parts) >= 2 and parts[0] not in tokens:
        tokens.append(" ".join(parts))
        tokens.append(parts[-1])
    elif len(parts) == 1 and parts[0] not in tokens:
        tokens.append(parts[0])

    p_low = p.lower()
    for k, syns in _PLAYER_SYNONYMS.items():
        if k in p_low or any(s in p for s in syns):
            tokens.append(k)
            tokens.extend(syns)

    full_names = [tok for tok in tokens if " " in tok]
    if full_names:
        for fn in full_names:
            if fn.upper() in t_up:
                return True

    hit = False
    for tok in tokens:
        if len(tok) >= 2 and tok.upper() in t_up:
            hit = True
            break
    if not hit:
        return False

    if "LEONARD MILLER" in p.upper() and "BRANDON" in t_up:
        return False
    if "BRANDON MILLER" in p.upper() and "LEONARD" in t_up:
        return False
    return True

def _is_exact_card_match(card: dict, sale: dict) -> bool:
    """严格判定成交记录是否为该卡片的 1:1 完全同款。"""
    title = sale.get("title") or ""
    if not _check_player_match(card.get("player") or card.get("player_hint"), title):
        return False
    if not _check_year_set_match(card.get("year_set"), title):
        return False
    card_auto = bool(card.get("is_auto")) or _card_wants_auto(card.get("card_type"), card.get("notes"))
    if not _check_auto_match(card_auto, title):
        return False
    if not _check_serial_match(card.get("serial"), title):
        return False
    if not _check_parallel_match(card.get("parallel"), card.get("card_type"), card.get("notes"), title):
        return False
    score = float(card.get("grade_score") or 0)
    if not _check_grade_match(card.get("grader"), score, title):
        return False
    return True

def _filter_exact_sales(card: dict, sales: list) -> list:
    """从候选成交列表中严格筛选出 1:1 完全同款成交。"""
    return [s for s in (sales or []) if _is_exact_card_match(card, s)]


# ---------------------------- 单卡 / 批量重估 ----------------------------

@app.post("/api/revalue/card/{card_id}")
def revalue_card(card_id: str, apply: bool = True, deep: bool = False):
    """重估单卡：遵循核心铁律 1:1 强对齐，同款取最新成交；无同款坚决保留估值。
    apply=True 时若有新同款成交才更新 est_raw_price；无同款时维持原估值。
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")

        query = (row["search_query"] or "").strip()
        if not query:
            query = build_search_query(row["sport"], row["player"], row["year_set"],
                                       row["card_type"], row["serial"])
        if not query:
            return {"status": "no_query",
                    "message": "这张卡没有可用搜索词，请先补全系列或球员名"}

        grader = row["grader"] if "grader" in row.keys() else ""
        score = row["grade_score"] if "grade_score" in row.keys() else 0
        tokens = _grade_tokens(grader, score)

        year_set_val = row["year_set"] if "year_set" in row.keys() else ""
        player_name = row["player"] if "player" in row.keys() else ""
        _par = row["parallel"] if "parallel" in row.keys() else ""
        candidates = _fallback_queries(query, player_name, year_set_val, _par)

        tried = []
        merged_sales = []
        seen_ids = set()

        for cand in candidates:
            tried.append(cand)
            try:
                one, _ = _scrapling_post("/api/card/valuation",
                                         {"keyword": cand, "max_history": 60}, timeout=15)
                if one and one.get("status") == "success":
                    for sale in (one.get("recent_sales") or []):
                        sid = str(sale.get("id") or "")
                        if sid and sid not in seen_ids:
                            seen_ids.add(sid)
                            merged_sales.append(sale)
                    if not deep:
                        break
            except Exception:
                pass

        sales = merged_sales
        parallel_val = row["parallel"] if "parallel" in row.keys() else ""
        card_type_val = row["card_type"] if "card_type" in row.keys() else ""
        notes_val = row["notes"] if "notes" in row.keys() else ""
        ys_val = row["year_set"] if "year_set" in row.keys() else ""
        serial_val = row["serial"] if "serial" in row.keys() else ""
        is_auto_val = row["is_auto"] if "is_auto" in row.keys() else None

        td = _refresh_card_value(conn, card_id, player_name, query, sales, tokens,
                                 parallel=parallel_val, card_type=card_type_val, notes=notes_val,
                                 year_set=ys_val, serial=serial_val,
                                 grader=grader, grade_score=score, is_auto=is_auto_val)

        old_value = float(row["est_raw_price"] or 0)

        # 铁律 2：无同款坚决保留估值，不脑补、不折算
        if td.get("source") == "insufficient" or td.get("value", 0) <= 0:
            try:
                conn.execute(
                    "UPDATE cards SET valuation_status = 'insufficient', "
                    "valuation_note = ?, updated_at = ? WHERE card_id = ?",
                    (INSUFFICIENT_NOTE, now_str(), card_id))
                conn.commit()
            except sqlite3.Error as exc:
                logger.warning("[重估] 标记 insufficient 失败 %s: %s", card_id, exc)
            return {
                "status": "insufficient",
                "card_id": card_id,
                "keyword": query,
                "message": INSUFFICIENT_NOTE,
                "sample_count": 0,
                "exact_count": td.get("exact_count", 0),
                "confidence": "none",
                "old_value": round(old_value, 2),
                "new_value": round(old_value, 2),
                "fair_market_value": 0,
                "latest_price": 0,
                "applied": False,
                "price_source": "insufficient",
                "valuation_note": INSUFFICIENT_NOTE,
                "latest_sale_date": "",
                "filtered_sales": [],
            }

        # 铁律 1：命中完全同款，取 sold_date 最近一笔真实成交价，原价采用
        latest_price = float(td["value"])
        sample = td["sample"]
        price_source = "exact_latest_sale"
        confidence = td["confidence"]
        earliest_sale = td.get("earliest") or ""
        latest_date = td.get("latest_sale_date") or ""
        note_text = td.get("note") or INSUFFICIENT_NOTE

        bucket = ("%s %s" % (tokens[0], tokens[1])) if len(tokens) > 1 else (tokens[0] if tokens else "RAW")
        _write_price_history(conn, card_id, latest_price, sample, bucket, price_source)

        if apply and latest_price > 0:
            conn.execute(
                "UPDATE cards SET est_raw_price = ?, valuation_status = 'ok', "
                "valuation_note = ?, updated_at = ? WHERE card_id = ?",
                (latest_price, note_text, now_str(), card_id),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "card_id": card_id,
        "keyword": query,
        "grade_filter": tokens,
        "price_source": price_source,
        "fair_market_value": round(latest_price, 2),
        # median_price 只为兼容老前端保留，含义已改成"最近同款成交价"，
        # 不再是任何形式的样本中位数。
        "median_price": round(latest_price, 2),
        "latest_price": round(latest_price, 2),
        "latest_sale_date": latest_date,
        "sample_count": sample,
        "exact_count": td.get("exact_count", sample),
        "old_value": round(old_value, 2),
        "new_value": round(latest_price, 2) if apply else round(old_value, 2),
        "applied": bool(apply and latest_price > 0),
        "confidence": confidence,
        "earliest_sale": earliest_sale,
        "valuation_note": note_text,
        "filtered_sales": td.get("samples") or [],
    }


@app.post("/api/revalue/batch")
def revalue_batch(limit: int = 5, apply: bool = True, only_unpriced: bool = False,
                  offset: int = 0, deep: bool = False):
    """批量重估。每次只处理 N 张，前端循环调用（配合 offset 递增），避免单请求超时。

    卡淘估值单张约 1-2 秒，一次塞 130 张会直接把请求拖死。
    apply=False（试算）不会更新 updated_at，所以必须靠 offset 推进，
    否则每批都会选中同一批卡。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 20))
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    conn = get_db()
    try:
        sql = "SELECT card_id FROM cards WHERE COALESCE(status,'') != '已售出'"
        if only_unpriced:
            sql += " AND COALESCE(est_raw_price,0) <= 0"
        sql += " ORDER BY id ASC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, (limit, offset)).fetchall()
        total_eligible = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE COALESCE(status,'') != '已售出'").fetchone()["n"]
    finally:
        conn.close()

    ids = [r["card_id"] for r in rows]
    if not ids:
        return {"status": "empty", "message": "没有需要重估的卡片", "results": []}

    results = []
    ok = insufficient = fail = 0
    for cid in ids:
        try:
            out = revalue_card(cid, apply=apply, deep=deep)
        except HTTPException:
            out = {"status": "error", "card_id": cid, "message": "卡片不存在"}
        except Exception as exc:
            out = {"status": "error", "card_id": cid,
                   "message": "%s: %s" % (type(exc).__name__, exc)}
        st = out.get("status")
        if st == "success":
            ok += 1
        elif st == "insufficient":
            # 无同款不是失败：按铁律保留人工估值，这是正确结果
            insufficient += 1
        else:
            fail += 1
        results.append({
            "card_id": cid,
            "status": out.get("status"),
            "old": out.get("old_value"),
            "new": out.get("new_value"),
            "computed": out.get("latest_price") or out.get("median_price"),
            "confidence": out.get("confidence"),
            "keyword": out.get("keyword"),
            "sample": out.get("sample_count"),
            "source": out.get("price_source"),
            "message": out.get("message"),
        })

    return {
        "status": "success",
        "requested": len(ids),
        "succeeded": ok,
        "insufficient": insufficient,
        "failed": fail,
        "next_offset": offset + len(ids),
        "total_eligible": total_eligible,
        "done": (offset + len(ids)) >= total_eligible,
        "results": results,
        "remaining_hint": "继续调用本接口处理下一批" if len(ids) == limit else "已处理完",
    }


# ---------------------------- 价格提醒 ----------------------------

def _init_alert_tables():
    """提醒相关建表。独立于 cards，绝不侵入收藏数据。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                kind TEXT DEFAULT 'below_fmv',
                threshold REAL DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                note TEXT,
                last_value REAL DEFAULT 0,
                last_fired TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER,
                card_id TEXT,
                fired_at TEXT,
                message TEXT,
                value REAL DEFAULT 0,
                seen INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_card ON price_alerts(card_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_seen ON alert_events(seen, fired_at)")
        conn.commit()
    finally:
        conn.close()


_init_alert_tables()


@app.get("/api/alerts")
def list_alerts():
    """列出全部提醒，并带上是哪张卡。"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT a.*, c.player, c.year_set, c.card_type, c.est_raw_price
              FROM price_alerts a
              LEFT JOIN cards c ON c.card_id = a.card_id
             ORDER BY a.id DESC
        """).fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"alerts": items, "count": len(items)}


@app.post("/api/alerts")
async def create_alert(card_id: str = Form(...), kind: str = Form("below_fmv"),
                       threshold: float = Form(0.0), note: str = Form("")):
    """新建提醒。kind: below_fmv(低于X元) / drop_pct(较上次跌X%) / new_sale(有新成交)。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        cur = conn.execute(
            "INSERT INTO price_alerts (card_id, kind, threshold, enabled, note, created_at, updated_at) "
            "VALUES (?,?,?,1,?,?,?)",
            (card_id, kind, float(threshold or 0), note, now_str(), now_str()),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return {"status": "success", "id": new_id}


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: int):
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="提醒不存在")
    finally:
        conn.close()
    return {"status": "success", "deleted": alert_id}


@app.post("/api/alerts/check")
def check_alerts():
    """检查全部启用提醒：拉一次估值，命中就写一条事件。"""
    conn = get_db()
    try:
        alerts = [dict(r) for r in conn.execute(
            "SELECT * FROM price_alerts WHERE enabled = 1").fetchall()]
    finally:
        conn.close()

    if not alerts:
        return {"status": "success", "checked": 0, "fired": 0, "events": []}

    fired = []
    for a in alerts:
        card_id = a["card_id"]
        out = revalue_card(card_id, apply=False)
        if out.get("status") != "success":
            continue

        cur_val = float(out.get("median_price") or 0)
        if cur_val <= 0:
            continue

        kind = a.get("kind") or "below_fmv"
        thr = float(a.get("threshold") or 0)
        last = float(a.get("last_value") or 0)
        hit = False
        msg = ""

        if kind == "below_fmv" and thr > 0 and cur_val <= thr:
            hit = True
            msg = "现价 %.2f 元，已低于你设的 %.2f 元" % (cur_val, thr)
        elif kind == "drop_pct" and thr > 0 and last > 0:
            drop = (last - cur_val) / last * 100.0
            if drop >= thr:
                hit = True
                msg = "较上次跌 %.1f%%（%.2f → %.2f）" % (drop, last, cur_val)
        elif kind == "new_sale" and last > 0 and abs(cur_val - last) > 0.01:
            hit = True
            msg = "出现新成交，估值 %.2f → %.2f" % (last, cur_val)

        conn2 = get_db()
        try:
            if hit:
                conn2.execute(
                    "INSERT INTO alert_events (alert_id, card_id, fired_at, message, value, seen) "
                    "VALUES (?,?,?,?,?,0)",
                    (a["id"], card_id, now_str(), msg, cur_val),
                )
                conn2.execute(
                    "UPDATE price_alerts SET last_value = ?, last_fired = ?, updated_at = ? WHERE id = ?",
                    (cur_val, now_str(), now_str(), a["id"]),
                )
                fired.append({"alert_id": a["id"], "card_id": card_id, "message": msg})
            else:
                conn2.execute(
                    "UPDATE price_alerts SET last_value = ?, updated_at = ? WHERE id = ?",
                    (cur_val, now_str(), a["id"]),
                )
            conn2.commit()
        finally:
            conn2.close()

    return {"status": "success", "checked": len(alerts), "fired": len(fired), "events": fired}


@app.get("/api/alerts/events")
def alert_events(limit: int = 20, unseen_only: bool = False):
    conn = get_db()
    try:
        sql = """
            SELECT e.*, c.player, c.year_set, c.card_type
              FROM alert_events e
              LEFT JOIN cards c ON c.card_id = e.card_id
        """
        if unseen_only:
            sql += " WHERE e.seen = 0"
        sql += " ORDER BY e.id DESC LIMIT ?"
        rows = conn.execute(sql, (max(1, min(int(limit or 20), 100)),)).fetchall()
        items = [dict(r) for r in rows]
        unseen = conn.execute(
            "SELECT COUNT(*) AS n FROM alert_events WHERE seen = 0").fetchone()["n"]
    finally:
        conn.close()
    return {"events": items, "unseen": unseen}


@app.post("/api/alerts/events/read")
def mark_events_read(event_id: int = 0):
    """event_id=0 表示全部标记已读。"""
    conn = get_db()
    try:
        if event_id:
            conn.execute("UPDATE alert_events SET seen = 1 WHERE id = ?", (event_id,))
        else:
            conn.execute("UPDATE alert_events SET seen = 1 WHERE seen = 0")
        conn.commit()
    finally:
        conn.close()
    return {"status": "success"}



# ==============================================================================
# v2.2 · 成交历史永久归档 / 时间衰减估值 / 每日快照
#
# 解决的核心问题：卡淘每年至少清空一次历史成交。一旦清空，
# 所有依赖"实时查成交"的估值全部失真，冷门卡更是直接查不到。
#
# 对策：把每次抓到的成交记录**永久落进本地库**（幂等去重），
# 此后估值优先用自有档案，卡淘实时数据只作补充。
# 平台清空不等于我们清空 —— 这份档案本身就是护城河。
#
# 配套：估值改用「按成交日期」的时间衰减，半衰期 90 天。
# 否则两年前首发的高价会永远把估值吊在高位（如夏思凝那类首发热卡）。
# ==============================================================================

import math as _math
from datetime import datetime as _dt

# 时间衰减半衰期（天）。卡牌行情约 3 个月一代，90 天是经验值。
DECAY_HALF_LIFE_DAYS = 90.0

# 估值铁律（用户拍板，不得再改）：
#   1. 禁止跨版别算中位数——同球员不同系列/编数/卡种价格天差地别；
#   2. 必须 1:1 严格同款：同球员+同年份系列+同平行折射+同限量编数+同带签+同评级；
#   3. 命中同款取 sold_date 最近一笔真实成交价，原价采用，不做任何折算；
#   4. 未命中同款保留录入时的人工估值 est_raw_price，状态置 insufficient。
INSUFFICIENT_NOTE = "暂无同款成交记录，保留人工估值"

# 归档保留窗口（天）。0 表示永久保留 —— 这正是对抗平台清空的意义。
ARCHIVE_KEEP_DAYS = 0


def _init_market_tables():
    """成交档案与每日快照。独立建表，不动 cards。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT UNIQUE,
                keyword TEXT,
                title TEXT,
                price REAL DEFAULT 0,
                sold_date TEXT,
                bid_count INTEGER DEFAULT 0,
                url TEXT,
                player_hint TEXT,
                card_id TEXT,
                source TEXT DEFAULT 'cardhobby',
                archived_at TEXT,
                first_seen_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_item ON market_sales(item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_player ON market_sales(player_hint)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_keyword ON market_sales(keyword)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_sold ON market_sales(sold_date)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snap_date TEXT,
                total_cards INTEGER DEFAULT 0,
                valued_cards INTEGER DEFAULT 0,
                total_cost REAL DEFAULT 0,
                total_value REAL DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_date ON daily_snapshot(snap_date)")
        conn.commit()
    finally:
        conn.close()


_init_market_tables()


def _norm_date(raw):
    """卡淘的成交日期格式不稳定，统一裁成 YYYY-MM-DD。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    return s[:10] if len(s) >= 10 else ""


def _archive_sales(conn, keyword, player_hint, sales, card_id=""):
    """把成交记录幂等写入档案。返回 (新增数, 跳过数)。

    以 item_id 为唯一键：同一笔成交反复抓到只更新不新增。
    这是"平台清空后我们仍有历史"的唯一保证，所以宁可多存不许漏存。
    """
    added = skipped = 0
    now = now_str()
    for s in sales or []:
        item_id = str(s.get("id") or "").strip()
        if not item_id:
            continue
        price = float(s.get("price") or 0)
        if price <= 0:
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO market_sales "
                "(item_id, keyword, title, price, sold_date, bid_count, url, "
                " player_hint, card_id, source, archived_at, first_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, str(keyword or ""), str(s.get("title") or ""), price,
                 _norm_date(s.get("end_time")), int(s.get("bid_count") or 0),
                 str(s.get("url") or ""), str(player_hint or ""), str(card_id or ""),
                 "cardhobby", now, now),
            )
            if conn.total_changes:
                added += 1
        except sqlite3.Error:
            skipped += 1
    return added, skipped


def _load_archived_sales(conn, player_hint="", keyword="", limit=400):
    """从自有档案取历史成交。优先按球员名，其次按关键词。"""
    rows = []
    if player_hint:
        rows = conn.execute(
            "SELECT item_id, title, price, sold_date, bid_count, url "
            "FROM market_sales WHERE player_hint = ? "
            "ORDER BY sold_date DESC LIMIT ?", (player_hint, limit)).fetchall()
    if not rows and keyword:
        rows = conn.execute(
            "SELECT item_id, title, price, sold_date, bid_count, url "
            "FROM market_sales WHERE keyword = ? "
            "ORDER BY sold_date DESC LIMIT ?", (keyword, limit)).fetchall()
    return [dict(r) for r in rows]


def _days_ago(date_str):
    """成交日期距今多少天。解析不了就当作"未知"，返回 None。"""
    d = _norm_date(date_str)
    if not d or len(d) != 10:
        return None
    try:
        then = _dt.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None
    return max(0.0, ( _dt.now() - then).total_seconds() / 86400.0)


def _latest_sale_value(sales):
    """取**最近一笔真实同款成交价**，返回 (价格, 样本数, 倒序样本, 最早成交日)。

    取代了原来的「按天数指数衰减加权平均」(_time_decayed_value)。
    原因：加权平均本质仍是跨样本求均值，而一个球员不同系列、不同编数、
    不同卡种的价格天差地别，把他们的成交混在一起求任何形式的均值都是错的。
    现在只做一件事：同款里谁最新用谁，价格原样采用，不折不算。
    """
    valid = []
    earliest = ""
    for s in sales or []:
        try:
            price = float(s.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        d = _norm_date(s.get("sold_date") or s.get("end_time") or "")
        valid.append((d, price, s))
        if d and (not earliest or d < earliest):
            earliest = d

    if not valid:
        return 0.0, 0, [], earliest

    # sold_date 倒序；没有日期的排最后——不能因为「日期不明」就抢走最新价的位置
    dated = sorted([x for x in valid if x[0]], key=lambda x: x[0], reverse=True)
    undated = [x for x in valid if not x[0]]
    ordered = dated + undated
    return round(ordered[0][1], 2), len(valid), [x[2] for x in ordered], earliest


def _time_decayed_value(sales, half_life_days=None):
    """已废弃，仅保留签名兼容旧调用：内部直接返回最近同款成交价。

    half_life_days 参数被忽略——时间衰减加权本身就是被用户否掉的那套算法。
    """
    return _latest_sale_value(sales)


def _refresh_card_value(conn, card_id, player_hint, keyword, raw_sales, tokens,
                        parallel="", card_type="", notes="", year_set="", serial="",
                        grader="", grade_score=0, is_auto=None):
    """按用户拍板的定价铁律估值，不做任何形式的均值/中位数折算。

    铁律：
      1. 禁止跨版别算中位数——同球员不同系列、不同编数、不同卡种价格天差地别；
      2. 1:1 严格同款匹配：同球员 + 同年份系列 + 同平行折射 + 同限量编数
         + 同带签状态 + 同评级状态，一条都不放宽；
      3. 命中同款：按 sold_date 倒序取最近一笔真实成交价，原价采用；
      4. 未命中同款：坚决不脑补折算价，保留录入时的人工估值 est_raw_price，
         调用方把 valuation_status 置为 insufficient。

    tokens 参数只为兼容旧调用保留（评级已在 _is_exact_card_match 内严格比对）。
    """
    card = {
        "player": player_hint or "",
        "year_set": year_set or "",
        "card_type": card_type or "",
        "notes": notes or "",
        "serial": serial or "",
        "parallel": parallel or "",
        "grader": grader or "",
        "grade_score": grade_score or 0,
        "is_auto": (bool(is_auto) if is_auto is not None else None),
    }

    # ---- 1. 归档 ----
    # 顺序仍然重要：先把球员过滤干净的成交写进档案，否则 Brandon Miller 的
    # 10 万成交会被永久写进 Leonard Miller 的档案，污染是不可逆的。
    pool_in = raw_sales or []
    ptokens = _player_tokens(player_hint)
    player_mode = "none"
    if ptokens:
        kept, player_mode = _filter_by_player(pool_in, ptokens)
        if player_mode == "surname" and len(kept) < 3:
            kept = pool_in
            player_mode = "fallback_all"
        pool_in = kept

    try:
        added, _ = _archive_sales(conn, keyword, player_hint, pool_in, card_id)
    except sqlite3.Error as exc:
        logger.warning("[估值] 归档失败 %s: %s", card_id, exc)
        added = 0

    # 档案是**累积池**：每次新抓到的都要并进去，不能"档案有数据就只用档案"。
    try:
        hist = _load_archived_sales(conn, player_hint=player_hint, keyword=keyword)
    except sqlite3.Error as exc:
        logger.warning("[估值] 读取档案失败 %s: %s", card_id, exc)
        hist = []

    pool = list(hist)
    seen_ids = {str(h.get("item_id") or "") for h in hist}
    for s in pool_in:
        sid = str(s.get("id") or s.get("item_id") or "")
        if sid:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
        pool.append({
            "item_id": sid,
            "title": s.get("title") or "",
            "price": s.get("price") or 0,
            "sold_date": _norm_date(s.get("end_time") or s.get("sold_date") or ""),
        })
    if not pool:
        pool = pool_in

    # ---- 2. 1:1 严格同款 ----
    # 球员 / 年份系列 / 平行版别 / 限量编数 / 带签 / 评级，六项全过才算同款。
    # 这一层不做任何"差不多就行"的放宽，宁可判 insufficient 也不乱给价。
    exact = [s for s in pool if _is_exact_card_match(card, s)]

    base = {
        "archive_size": len(hist),
        "archived_now": added,
        "player_mode": player_mode,
        "pool_count": len(pool),
        "exact_count": len(exact),
    }

    if not exact:
        return {
            **base,
            "value": 0, "sample": 0, "earliest": "",
            "source": "insufficient", "confidence": "none",
            "samples": [], "latest_sale_date": "",
            "note": INSUFFICIENT_NOTE,
        }

    # ---- 3. 命中同款：取最近一笔成交，原价采用 ----
    value, n, ordered, earliest = _latest_sale_value(exact)
    if value <= 0:
        return {
            **base,
            "value": 0, "sample": 0, "earliest": "",
            "source": "insufficient", "confidence": "none",
            "samples": [], "latest_sale_date": "",
            "note": INSUFFICIENT_NOTE,
        }

    latest = ordered[0] if ordered else {}
    latest_date = _norm_date(latest.get("sold_date") or "")
    note = "最近同款成交 ¥%.2f%s" % (
        value, ("（%s）" % latest_date) if latest_date else "")

    return {
        **base,
        "value": value,
        "sample": n,
        "earliest": earliest,
        "source": "exact_latest_sale",
        "confidence": _confidence(n),
        "note": note,
        "latest_sale_date": latest_date,
        # 只列真正参与判定的同款成交，前端展示必须用这一批
        "samples": [
            {
                "title": it.get("title") or "",
                "price": float(it.get("price") or 0),
                "closed_str": _norm_date(it.get("sold_date") or it.get("end_time") or ""),
            }
            for it in ordered[:20]
        ],
    }


# ---------------------------- 每日快照 ----------------------------

@app.post("/api/portfolio/snapshot")
def take_snapshot():
    """记录今天的组合快照。同日重复调用会覆盖。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN COALESCE(est_raw_price,0) > 0 THEN 1 ELSE 0 END) AS valued, "
            "       COALESCE(SUM(cost_price),0) AS cost, "
            "       COALESCE(SUM(est_raw_price),0) AS value "
            "FROM cards WHERE COALESCE(status,'') != '已售出'").fetchone()
        today = _dt.now().strftime("%Y-%m-%d")
        total = int(row["total"] or 0)
        valued = int(row["valued"] or 0)
        cost = float(row["cost"] or 0)
        value = float(row["value"] or 0)
        conn.execute(
            "INSERT INTO daily_snapshot (snap_date, total_cards, valued_cards, "
            "total_cost, total_value, total_pnl, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(snap_date) DO UPDATE SET "
            "total_cards=excluded.total_cards, valued_cards=excluded.valued_cards, "
            "total_cost=excluded.total_cost, total_value=excluded.total_value, "
            "total_pnl=excluded.total_pnl, created_at=excluded.created_at",
            (today, total, valued, cost, value, round(value - cost, 2), now_str()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "date": today,
            "total_cards": total, "valued_cards": valued,
            "total_value": round(value, 2), "total_pnl": round(value - cost, 2)}


@app.get("/api/portfolio/summary-daily")
def summary_daily():
    """组合总值三件套 + 涨跌榜（对标 Collectr / Card Ladder）。

    口径说明（重要）：组队拆卡得来的藏品没有独立成本价，成本记 0。
    这类卡只计入总市值，**不参与盈亏计算** —— 否则会把估值全算成纯利润，
    得出"投入 1783 元赚 18 万"这种毫无意义的数字。
    """
    conn = get_db()
    try:
        snaps = [dict(r) for r in conn.execute(
            "SELECT * FROM daily_snapshot ORDER BY snap_date DESC LIMIT 30").fetchall()]

        cur = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN COALESCE(est_raw_price,0) > 0 THEN 1 ELSE 0 END) AS valued, "
            "       SUM(CASE WHEN COALESCE(cost_price,0) > 0 THEN 1 ELSE 0 END) AS costed, "
            "       COALESCE(SUM(cost_price),0) AS cost, "
            "       COALESCE(SUM(est_raw_price),0) AS value "
            "FROM cards WHERE COALESCE(status,'') != '已售出'").fetchone()

        # 只有"有成本价"的那部分才能算盈亏
        costed_row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "       COALESCE(SUM(cost_price),0) AS cost, "
            "       COALESCE(SUM(est_raw_price),0) AS value "
            "FROM cards WHERE COALESCE(status,'') != '已售出' "
            "  AND COALESCE(cost_price,0) > 0").fetchone()

        total = int(cur["total"] or 0)
        valued = int(cur["valued"] or 0)
        costed = int(cur["costed"] or 0)
        value = float(cur["value"] or 0)
        cost = float(cur["cost"] or 0)

        costed_cost = float(costed_row["cost"] or 0)
        costed_value = float(costed_row["value"] or 0)
        costed_pnl = round(costed_value - costed_cost, 2)
        costed_pnl_pct = round(costed_pnl / costed_cost * 100, 2) if costed_cost else None

        # 无成本（组队拆卡）的那部分，只报市值，不报盈亏
        uncosted_count = total - costed
        uncosted_value = round(value - costed_value, 2)

        prev_value = None
        if snaps and snaps[0]["snap_date"] == _dt.now().strftime("%Y-%m-%d") and len(snaps) > 1:
            prev_value = float(snaps[1]["total_value"] or 0)
        elif snaps:
            prev_value = float(snaps[0]["total_value"] or 0)

        delta = round(value - prev_value, 2) if prev_value is not None else None
        delta_pct = round((value - prev_value) / prev_value * 100, 2) if prev_value else None

        movers = [dict(r) for r in conn.execute(
            "SELECT card_id, player, year_set, est_raw_price, cost_price, "
            "       (COALESCE(est_raw_price,0) - COALESCE(cost_price,0)) AS pnl "
            "FROM cards WHERE COALESCE(status,'') != '已售出' "
            "  AND COALESCE(est_raw_price,0) > 0 AND COALESCE(cost_price,0) > 0 "
            "ORDER BY pnl DESC LIMIT 5").fetchall()]
        losers = [dict(r) for r in conn.execute(
            "SELECT card_id, player, year_set, est_raw_price, cost_price, "
            "       (COALESCE(est_raw_price,0) - COALESCE(cost_price,0)) AS pnl "
            "FROM cards WHERE COALESCE(status,'') != '已售出' "
            "  AND COALESCE(est_raw_price,0) > 0 AND COALESCE(cost_price,0) > 0 "
            "ORDER BY pnl ASC LIMIT 5").fetchall()]
    finally:
        conn.close()

    return {
        "status": "success",
        "total_value": round(value, 2),
        "cards": total,
        "valued_cards": valued,
        "coverage": round(valued / total * 100, 1) if total else 0,

        # 有成本的部分：这部分才是真实盈亏
        "costed_cards": costed,
        "costed_cost": round(costed_cost, 2),
        "costed_value": round(costed_value, 2),
        "total_pnl": costed_pnl,
        "total_pnl_pct": costed_pnl_pct,

        # 组队拆卡等无成本的部分：只报市值
        "uncosted_cards": uncosted_count,
        "uncosted_value": uncosted_value,
        "note": "组队拆卡的藏品无独立成本价，只计市值不参与盈亏",

        "day_delta": delta,
        "day_delta_pct": delta_pct,
        "top_gainers": movers,
        "top_losers": losers,
        "snapshots": snaps[:14],
    }



# ==============================================================================
# v2.3 · 五维统计 / CSV 导出 / 想看清单撮合
# 对应调研里的三条高性价比项：
#   - TCDB 五维收藏统计（最快出效果，纯 group by）
#   - Heritage CSV 导出（所有收藏系统的逃生舱）
#   - TCDB Match Items 的单用户变体：缺口 ↔ 卡淘在拍自动撮合
# ==============================================================================


def _init_want_table():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wantlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                player TEXT,
                year_set TEXT,
                card_type TEXT,
                serial TEXT,
                max_price REAL DEFAULT 0,
                search_query TEXT,
                priority INTEGER DEFAULT 1,
                status TEXT DEFAULT 'wanted',
                last_checked TEXT,
                last_hit TEXT,
                last_price REAL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_want_status ON wantlist(status)")
        conn.commit()
    finally:
        conn.close()


_init_want_table()


@app.get("/api/stats/dimensions")
def stats_dimensions():
    """五维收藏统计：运动 / 年份 / 球员 / 系列 / 评级。

    照 TCDB 的 Collection Stats —— 一个 group by 面板就够了，
    不需要图表库，但信息密度极高。
    """
    conn = get_db()
    try:
        base = "FROM cards WHERE COALESCE(status,'') != '已售出'"

        by_sport = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(sport,''),'未分类') AS k, COUNT(*) AS n, "
            "       COALESCE(SUM(est_raw_price),0) AS v " + base +
            " GROUP BY k ORDER BY n DESC").fetchall()]

        by_year = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(substr(year_set,1,4),''),'未知') AS k, COUNT(*) AS n, "
            "       COALESCE(SUM(est_raw_price),0) AS v " + base +
            " GROUP BY k ORDER BY k DESC").fetchall()]

        by_player = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(player,''),'未填') AS k, COUNT(*) AS n, "
            "       COALESCE(SUM(est_raw_price),0) AS v " + base +
            " GROUP BY k ORDER BY n DESC LIMIT 30").fetchall()]

        by_grader = [dict(r) for r in conn.execute(
            "SELECT CASE WHEN COALESCE(grader,'') = '' THEN 'RAW 裸卡' "
            "            ELSE grader || ' ' || CAST(COALESCE(grade_score,0) AS TEXT) END AS k, "
            "       COUNT(*) AS n, COALESCE(SUM(est_raw_price),0) AS v " + base +
            " GROUP BY k ORDER BY n DESC").fetchall()]

        # 系列：取"年份+系列"里的系列核心词（复用已有解析）
        rows = conn.execute(
            "SELECT year_set, est_raw_price " + base).fetchall()
        series_map = {}
        for r in rows:
            key = _series_core(r["year_set"]) or "未填系列"
            item = series_map.setdefault(key, {"k": key, "n": 0, "v": 0.0})
            item["n"] += 1
            item["v"] += float(r["est_raw_price"] or 0)
        by_series = sorted(series_map.values(), key=lambda x: -x["n"])[:30]

        def clean(lst):
            return [{"key": x["k"], "count": x["n"], "value": round(float(x["v"] or 0), 2)} for x in lst]

        total = conn.execute("SELECT COUNT(*) AS n " + base).fetchone()["n"]
    finally:
        conn.close()

    return {
        "status": "success",
        "total": total,
        "by_sport": clean(by_sport),
        "by_year": clean(by_year),
        "by_player": clean(by_player),
        "by_series": clean(by_series),
        "by_grader": clean(by_grader),
    }


@app.get("/api/export/csv")
def export_csv():
    """导出全部卡片为 CSV。这是所有收藏系统的逃生舱，第一天就该有。"""
    import csv as _csv
    import io as _io

    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM cards ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="没有可导出的卡片")

    buf = _io.StringIO()
    buf.write("\ufeff")          # BOM，保证 Excel 打开不乱码
    fields = [
        "card_id", "sport", "player", "year_set", "card_type", "serial",
        "grader", "grade_score", "is_rookie", "is_auto", "is_patch",
        "marketplace", "est_raw_price", "cost_price", "status",
        "grading_verdict", "notes", "search_query", "created_at", "updated_at",
    ]
    writer = _csv.writer(buf)
    writer.writerow(fields)
    for r in rows:
        d = dict(r)
        writer.writerow([d.get(f, "") if d.get(f) is not None else "" for f in fields])

    from fastapi.responses import Response as _Resp
    return _Resp(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="card-vault-%s.csv"' % _dt.now().strftime("%Y%m%d")},
    )


# ---------------------------- 想看清单（缺口） ----------------------------

@app.get("/api/wantlist")
def list_wantlist():
    conn = get_db()
    try:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM wantlist ORDER BY priority DESC, id DESC").fetchall()]
    finally:
        conn.close()
    return {"status": "success", "items": items, "count": len(items)}


@app.post("/api/wantlist")
def add_want(name: str = Form(""), player: str = Form(""), year_set: str = Form(""),
             card_type: str = Form(""), serial: str = Form(""),
             max_price: float = Form(0.0), priority: int = Form(1)):
    """加入一张想收的卡。search_query 留空则按字段自动拼。"""
    label = (name or "").strip()
    if not label:
        label = " ".join(x for x in [year_set, card_type, player, serial] if x).strip()
    if not label:
        raise HTTPException(status_code=400, detail="至少要给一个名称或球员")

    q = build_search_query("", player, year_set, card_type, serial) or label

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO wantlist (name, player, year_set, card_type, serial, "
            "max_price, search_query, priority, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'wanted', ?, ?)",
            (label, player, year_set, card_type, serial, float(max_price or 0),
             q, int(priority or 1), now_str(), now_str()),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return {"status": "success", "id": new_id, "search_query": q}


@app.delete("/api/wantlist/{want_id}")
def delete_want(want_id: int):
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM wantlist WHERE id = ?", (want_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="条目不存在")
    finally:
        conn.close()
    return {"status": "success", "deleted": want_id}


@app.post("/api/wantlist/{want_id}/match")
def match_want(want_id: int):
    """拿一条想收的卡，去卡淘找当前在拍且低于心理价的。

    这是调研里评价最高的一条：国外做「谁有我要的」（人找人），
    单用户系统做「平台上现在有什么我要的」（人找市场）。
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM wantlist WHERE id = ?", (want_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="条目不存在")
        want = dict(row)
    finally:
        conn.close()

    query = (want.get("search_query") or "").strip()
    if not query:
        return {"status": "no_query", "message": "这条没有搜索词"}

    result, err = _scrapling_post("/api/card/valuation",
                                  {"keyword": query, "max_history": 30}, timeout=90)
    if result is None:
        return {"status": "unavailable", "message": "估值服务不可用：" + err}

    fmv = float(result.get("fair_market_value") or 0)
    cap = float(want.get("max_price") or 0)
    hits = []

    # 先用卡淘实时抓的在拍（valuation 已带 bargain 列表）
    for b in (result.get("bargains") or []):
        price = float(b.get("current_price") or 0)
        if price <= 0:
            continue
        if cap > 0 and price > cap:
            continue
        hits.append({
            "item_id": b.get("id"),
            "title": b.get("title"),
            "price": price,
            "fair_market_value": fmv,
            "discount_pct": b.get("discount_pct"),
            "url": b.get("url"),
            "end_time": b.get("end_time"),
        })

    hits.sort(key=lambda x: x["price"])

    conn = get_db()
    try:
        conn.execute(
            "UPDATE wantlist SET last_checked = ?, last_hit = ?, last_price = ?, "
            "updated_at = ? WHERE id = ?",
            (now_str(), (hits[0]["url"] if hits else ""),
             (hits[0]["price"] if hits else 0), now_str(), want_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "want_id": want_id,
        "name": want.get("name"),
        "query": query,
        "fair_market_value": fmv,
        "max_price": cap,
        "hit_count": len(hits),
        "hits": hits[:10],
        "message": ("找到 %d 个匹配在拍" % len(hits)) if hits else "当前没有符合条件在拍",
    }


@app.post("/api/wantlist/match-all")
def match_all_wants(limit: int = 3):
    """批量撮合。每次限 3 条，避免一次打太多请求触发卡淘风控。"""
    try:
        limit = max(1, min(int(limit), 5))
    except (TypeError, ValueError):
        limit = 3

    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id FROM wantlist WHERE status = 'wanted' "
            "ORDER BY priority DESC, COALESCE(last_checked,'') ASC LIMIT ?",
            (limit,)).fetchall()]
    finally:
        conn.close()

    out = []
    for r in rows:
        try:
            out.append(match_want(r["id"]))
        except HTTPException as exc:
            out.append({"want_id": r["id"], "status": "error", "message": str(exc.detail)})
        except Exception as exc:
            out.append({"want_id": r["id"], "status": "error",
                        "message": "%s: %s" % (type(exc).__name__, exc)})
    return {"status": "success", "checked": len(rows), "results": out}



# ==============================================================================
# v2.4 · base / 折射 分档（修正"低估"实为误估高的问题）
#
# 问题：同一年份同一系列的同一球员，base 卡与银折/金折价差常有 10-40 倍。
# 搜索词若只写 "2024 Prizm Wembanyama"，两种成交会混在一起，
# 中位数被高处拉爆 —— 表现就是"base 卡被低估"，实际是把 base 当折射算了。
#
# 解法：
#   1) 给卡片加 parallel / card_number 字段（人工填或视觉识别背面）
#   2) 估值时按 parallel 过滤成交：base 只算无折射词的，折射必须有对应词
#   3) 提供背面视觉识别端点，从卡背提取卡号与版别
# ==============================================================================

from pydantic import BaseModel as _PydBaseModel

# 平行/折射词表。命中任一即视为"非 base"。
_PARALLEL_TERMS = [
    # 英文
    "silver", "refractor", "prizm", "holo", "holofoil", "mojo", "wave",
    "shimmer", "sparkle", "ice", "cracked", "disco", "velocity", "laser",
    "scope", "sapphire", "mega", "choice", "tiger", "snakeskin", "zebra",
    "gold", "orange", "blue", "green", "red", "purple", "black", "pink",
    "teal", "aqua", "yellow", "bronze", "platinum", "emerald", "ruby",
    "superfractor", "printing plate", "1st bowman",
    # 中文（卡淘标题以中文为主）
    "银折", "金折", "红折", "蓝折", "绿折", "紫折", "黑折", "橙折", "粉折",
    "白折", "青折", "棕折", "铜折", "铂金折", "祖母绿", "红宝石",
    "折射", "镭射", "碎冰", "星云", "彩虹", "手绘", "波纹", "虎纹", "斑马",
    "泡泡", "闪光", "镜面", "全息", "派拉蒙", "撒哈拉", "蓝宝石", "格子",
]

# 明确标记 base 的词（这些出现时即使命中上面的词也要排除误判）
_BASE_EXPLICIT = ["base card", "base set", "普卡", "基础版", "普通版"]


def _is_parallel_title(title):
    """标题是否含折射/平行标识。"""
    up = str(title or "").upper()
    for be in _BASE_EXPLICIT:
        if be.upper() in up:
            return False
    for t in _PARALLEL_TERMS:
        if t.upper() in up:
            return True
    return False


def _filter_sales_by_parallel(items, parallel, strict=False):
    """按版别过滤成交。

    parallel 为空 → 不过滤（用户还没标注，保持原行为）
    base        → 只保留标题中**没有**折射词的成交
    其他任意值   → 只保留标题中含对应词或任一折射词的成交

    strict=True 用于**展示**：没有同版次成交就返回空列表，
    让页面如实显示"没有同版次成交"，而不是拿 base 的成交充数。
    strict=False 用于**估值**：没有同版次时回退到全样本，
    宁可给一个粗估值，也不能让整张卡变成"无法估值"。
    """
    p = str(parallel or "").strip().lower()
    if not p:
        return items, "unlabeled"

    # 有编号的卡：编数（/99、1/1）已体现在搜索词里，无需再按折射词过滤，
    # 否则会把"同编号的其他平行版"也排除，反而缩窄样本。
    if p in ("serialized", "有编", "限量"):
        return items, "serialized_skip"

    if p in ("base", "普卡", "普通版", "基础版"):
        kept = [it for it in items if not _is_parallel_title(it.get("title"))]
        if kept:
            return kept, "base_only"
        return ([], "base_none") if strict else (items, "base_fallback")

    # 具体版别：按**中英别名**匹配。
    # 只比英文会漏掉中文标题里的「金折 / 正金折 / 银折」——
    # 实测 gold 用裸字符串匹配中文标题命中率是 0，等于完全没过滤。
    exact = [it for it in items if _parallel_alias_hit(it.get("title"), p)]
    if exact:
        return exact, "parallel_exact"
    anyp = [it for it in items if _is_parallel_title(it.get("title"))]
    if anyp:
        return anyp, "parallel_any"
    return ([], "parallel_none") if strict else (items, "parallel_fallback")


# ---------------------------- 背面识别 ----------------------------

class BackScanRequest(_PydBaseModel):
    image_b64: str = ""
    title: str = ""


@app.post("/api/ai/scan-back")
async def scan_back(req: BackScanRequest):
    """看卡片背面，提取卡号与版别判断。

    为什么看背面：背面通常印有卡号（如 #136）与系列名，
    这是把"这张卡到底是哪一条 checklist"钉死的唯一可靠依据；
    仅凭正面很难区分 base 与折射（尤其 Prizm 系）。
    """
    if not req.image_b64:
        raise HTTPException(status_code=400, detail="缺少 image_b64")

    prompt = (
        "你是球星卡鉴定专家。请观察这张卡的**背面**，提取信息。" + NL
        + "重点看三样：" + NL
        + "1) 卡号（背面通常印 #136 / No.136 / 136）" + NL
        + "2) 系列名与版权行" + NL
        + "3) **版别名称**：Topps/Panini 会在卡背直接印平行版别，"
        + "如 REFRACTOR（银折）、SILVER、GOLD、PRIZM、X-FRACTOR、SUPERFRACTOR 等。"
        + "有就原样抄下来，没有就留空。" + NL
        + "只输出 JSON，不要代码块：" + NL
        + '{"card_number":"136 或空",'
        + '"set_name":"系列名或空",'
        + '"year":"年份或空",'
        + '"parallel_name":"卡背印的版别名，如 REFRACTOR，没有填空",'
        + '"is_reprint":false,'
        + '"back_text":"背面可见的关键文字片段（最多40字）",'
        + '"confidence":"high/medium/low"}'
    )

    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + req.image_b64}},
    ]
    headers = {
        "Authorization": "Bearer " + DSH_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": DSH_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": 6000,
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(DSH_API_BASE + "/chat/completions",
                                    headers=headers, json=payload)
        if res.status_code != 200:
            return {"status": "unavailable", "message": "视觉模型返回 %d" % res.status_code}
        raw = clean_fence(res.json()["choices"][0]["message"]["content"])
        data = json.loads(raw)
    except Exception as exc:
        return {"status": "error", "message": "%s: %s" % (type(exc).__name__, exc)}

    num = str(data.get("card_number") or "").strip()
    num_clean = num.replace("#", "").replace("No.", "").replace("NO.", "").strip()

    parallel_name = str(data.get("parallel_name") or "").strip()
    inferred = _match_parallel_in_text(parallel_name) if parallel_name else ""

    suggestion = "base"
    if _is_parallel_title(req.title):
        suggestion = "parallel"

    return {
        "status": "success",
        "card_number": num_clean,
        "set_name": str(data.get("set_name") or ""),
        "year": str(data.get("year") or ""),
        "parallel_name": parallel_name,
        "parallel_inferred": inferred,
        "is_reprint": bool(data.get("is_reprint")),
        "back_text": str(data.get("back_text") or ""),
        "confidence": str(data.get("confidence") or "low"),
        "title_parallel_hint": suggestion,
        "note": "卡背印有版别名（如 REFRACTOR）时，parallel_inferred 即为该版别；为空则多半是 base",
    }



# ==============================================================================
# v2.5 · 按业务规则自动推断版别
#
# 规则（用户给的，比任何算法都准）：
#   1) 有编号（/99、1/1 等）→ 一定是平行卡，不可能是 base
#   2) 无编号 → 看背面印字：印 REFRACTOR 就是银折，印 GOLD 就是金折
#   3) 背面也没有 → 默认 base
#   4) 卡背卡号 + 系列 → 可查 checklist 钉死具体是哪一版
# ==============================================================================

# 背面印字 → 标准版别名。Topps 系在卡背直接印平行名，这是最权威的依据。
#
# **顺序即优先级**：颜色词必须排在 REFRACTOR 之前。
# 实测教训：card_type 写 "1st Card Gold Refractor" 时，
# REFRACTOR 先命中会判成泛指的"折射"，丢掉"金折"这个真正决定价格的属性
# （金折/50 与普通折射/50 价差可达数倍）。
_BACK_PARALLEL_MAP = [
    ("SUPERFRACTOR", "superfractor"),
    ("PRINTING PLATE", "printing_plate"),
    ("SILVER PACK", "silver_pack"),
    # ---- 具体颜色优先 ----
    ("GOLD", "gold"),
    ("ORANGE", "orange"),
    ("PURPLE", "purple"),
    ("PLATINUM", "platinum"),
    ("EMERALD", "emerald"),
    ("BRONZE", "bronze"),
    ("TEAL", "teal"),
    ("AQUA", "aqua"),
    ("BLUE", "blue"),
    ("GREEN", "green"),
    ("BLACK", "black"),
    ("PINK", "pink"),
    ("RED", "red"),
    ("SILVER", "silver"),
    # ---- 折射类（颜色都没命中时才落在这里）----
    ("X-FRACTOR", "x_fractor"),
    ("REFRACTOR", "refractor"),
    ("PRIZM", "prizm"),
    ("MINI DIAMOND", "mini_diamond"),
    ("HOLO", "holo"),
    ("MOJO", "mojo"),
    ("SHIMMER", "shimmer"),
    ("SPARKLE", "sparkle"),
    ("WAVE", "wave"),
]

_SERIAL_PLACEHOLDERS = {"", "待查", "无", "none", "n/a", "na", "null", "-"}


def _has_real_serial(serial):
    """判断是否真有编号（排除"待查""无"这类占位）。

    有编号 = 该卡是限量平行版，**不可能是 base**。
    """
    s = str(serial or "").strip()
    if not s:
        return False
    if s.lower() in _SERIAL_PLACEHOLDERS:
        return False
    return bool(re.search(r"\d", s))


def _match_parallel_in_text(text):
    """从一段文字里找出版别标识，返回标准名或空。"""
    up = str(text or "").upper()
    if not up:
        return ""
    # 先排除明确的 base 标注
    for be in _BASE_EXPLICIT:
        if be.upper() in up:
            return ""
    for needle, canonical in _BACK_PARALLEL_MAP:
        if needle in up:
            return canonical
    return ""


def _infer_parallel(serial, back_text="", title=""):
    """按规则推断版别，返回 (parallel, 依据说明)。

    优先级：**具体版别 > 编号 > 默认 base**。

    这个顺序修正过一次，起因是实测：一张 2024 Topps Chrome 郑钦文
    "1st Card Gold Refractor 09/50"，旧逻辑「有编就返回 serialized」
    把它压成了泛泛的"有编"，于是"金折"这个真正决定价格的身份丢了 ——
    金折/50 与普通折射/50 差好几倍，搜索词也因此保留了错误的版别描述，
    最终导致刚成交的 ¥2800 一条都爬不到。

    现在的规则：
      · 背面印了版别名 → 直接信它（最权威）
      · card_type / 标题写了颜色或折射名 → 用具体版别
      · 只写了编数、没写版别 → serialized（至少知道不是 base）
      · 什么都没有 → base
    """
    hit = _match_parallel_in_text(back_text)
    if hit:
        return hit, "背面印有版别标识（最权威）"

    hit = _match_parallel_in_text(title)
    if hit:
        return hit, "卡种/标题含版别标识"

    if _has_real_serial(serial):
        return "serialized", "有编号（%s）但未写具体版别" % str(serial).strip()

    return "base", "无编号且背面/标题均无版别标识，判定 base"


@app.post("/api/cards/{card_id}/infer-parallel")
def infer_card_parallel(card_id: str, back_text: str = Form(""), apply: bool = Form(True)):
    """推断单张卡的版别。传入背面 OCR 文字可提高准确度。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        d = dict(row)

        parallel, reason = _infer_parallel(
            d.get("serial"), back_text, d.get("card_type") or d.get("search_query") or "")

        if apply:
            conn.execute("UPDATE cards SET parallel = ?, updated_at = ? WHERE card_id = ?",
                         (parallel, now_str(), card_id))
            conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "card_id": card_id,
        "parallel": parallel,
        "reason": reason,
        "applied": bool(apply),
        "serial": d.get("serial"),
    }


@app.post("/api/infer-parallel/batch")
def infer_parallel_batch(overwrite: bool = False, limit: int = 200):
    """批量推断版别。

    路径刻意避开 /api/cards/ 前缀：FastAPI 按声明顺序匹配，
    /api/cards/infer-parallel-batch 会被 /api/cards/{card_id} 先吃掉。

    默认只填补 parallel 为空的卡，不覆盖你手工标注过的值 ——
    人工判断永远优先于自动推断。
    """
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200

    conn = get_db()
    try:
        sql = "SELECT card_id, serial, card_type, search_query, parallel FROM cards " \
              "WHERE COALESCE(status,'') != '已售出'"
        if not overwrite:
            sql += " AND COALESCE(parallel,'') = ''"
        sql += " ORDER BY id ASC LIMIT ?"
        rows = [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]

        changed = []
        counts = {}
        for r in rows:
            parallel, reason = _infer_parallel(
                r.get("serial"), "", r.get("card_type") or r.get("search_query") or "")
            conn.execute("UPDATE cards SET parallel = ?, updated_at = ? WHERE card_id = ?",
                         (parallel, now_str(), r["card_id"]))
            counts[parallel] = counts.get(parallel, 0) + 1
            changed.append({"card_id": r["card_id"], "parallel": parallel, "reason": reason})
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "processed": len(changed),
        "overwrite": bool(overwrite),
        "distribution": counts,
        "results": changed[:50],
        "note": "有编号的卡判为 serialized（必为平行版，非 base）",
    }



# ==============================================================================
# v2.6 · 展示层按「球员 + 版别」过滤成交样本
#
# 问题（用户实测）：一张 2025 Chrome Flagg Base Refractor（银折）的详情页，
# 估值算出 ¥2276.98 是对的，但下面列出的成交全是 ¥110-153 的 base 卡。
# 因为雷达抓回来的是全量召回的 samples，展示时没做任何过滤。
#
# 估值已经按球员+版别过滤了，展示必须同口径 —— 否则页面上自相矛盾：
# 上面一个 2276，下面三条 150 的 base 成交，用户一眼就知道不对。
#
# 实现方式：重新定义 _attach_quotes（模块级后定义覆盖前面的），
# 在挂载行情时就把样本洗一遍，前端无需改动。
# ==============================================================================


def _filter_quote_samples(q, card):
    """按球员 + 版别过滤雷达样本，并重算中位数/样本数。"""
    samples = list(q.get("samples") or [])
    if not samples:
        return q

    kept = samples

    # 1) 球员过滤：排除同姓不同人（Miller / 米勒 这种）
    #    命中就只用命中的；一条都命中不了就清空——
    #    把别人的成交价摆在这张卡下面，比空着更糟。
    ptokens = _player_tokens(card.get("player") or "")
    if ptokens:
        by_player, _mode = _filter_by_player(kept, ptokens)
        kept = by_player

    # 2) 版别过滤：base 卡不能展示折射成交，反之亦然
    #    strict=True —— 展示场景下宁可为空，也不能拿别的版次充数
    parallel = (card.get("parallel") or "").strip()
    if parallel:
        filtered, pmod = _filter_sales_by_parallel(kept, parallel, strict=True)
        kept = filtered

    # 注意：即使一条都没过滤掉也要继续往下走。
    # 早退会让 radar 回传的 median_rmb 直接留在页面上当主数字——
    # 而中位数正是用户明令禁止的口径，必须改写成最近同款成交价。
    out = dict(q)
    out["samples"] = kept
    out["filtered_hidden"] = len(samples) - len(kept)

    # 定价口径：最近一笔同款成交价，不再算中位数
    def _date_of(item):
        for key in ("sold_date", "closed_str", "end_time", "date", "closed"):
            v = _norm_date(item.get(key) or "")
            if v:
                return v
        return ""

    valid = []
    for item in kept:
        try:
            p = float(item.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            valid.append((_date_of(item), p))

    if valid:
        dated = sorted([x for x in valid if x[0]], key=lambda x: x[0], reverse=True)
        ordered = dated + [x for x in valid if not x[0]]
        latest_price, latest_date = round(ordered[0][1], 2), ordered[0][0]
        out["sample_count"] = len(valid)
        # median_rmb 是历史字段名，语义已改为「最近同款成交价」
        out["median_rmb"] = latest_price
        out["latest_rmb"] = latest_price
        out["latest_closed"] = latest_date
        out["recent_rmb"] = latest_price
        out["price_basis"] = "latest_exact_sale"
        out["latest_outlier"] = False       # 过滤后不再判断存疑，避免误导
    else:
        # 一条同版次成交都没有：如实清空，绝不能把旧中位数留在页面上
        out["sample_count"] = 0
        out["median_rmb"] = 0
        out["latest_rmb"] = 0
        out["latest_closed"] = ""
        out["recent_rmb"] = 0
        out["price_basis"] = "insufficient"
        out["latest_outlier"] = False

    if not kept:
        out["filtered_note"] = ("同球员+同版次的成交为 0，已隐藏 %d 条无关样本"
                                % (len(samples)))
    else:
        out["filtered_note"] = ("已按同球员+同版次过滤，隐藏 %d 条无关样本"
                                % (len(samples) - len(kept)))
    return out


def _attach_quotes(cards: list) -> None:
    """把行情挂到每张卡上，挂之前先按球员+版别洗样本。

    重定义本函数是为了让过滤对所有读取路径生效：
    /api/cards（列表）、/api/cards/{id}（详情）都会经过这里。
    """
    loaded = _load_vault_quotes()
    quotes = loaded["quotes"]
    for card in cards:
        q = _quote_for(quotes, card.get("card_id", ""))
        if q:
            try:
                q = _filter_quote_samples(q, card)
            except Exception:
                pass        # 过滤失败也不能让详情页打不开
        card["market_quote"] = q


@app.get("/api/cards/{card_id}/sales-filtered")
def sales_filtered(card_id: str):
    """单独查一张卡"过滤后"的成交样本，用于排查展示错配。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        card = dict(row)
    finally:
        conn.close()

    loaded = _load_vault_quotes()
    q = _quote_for(loaded["quotes"], card_id)
    if not q:
        return {"status": "no_quote", "card_id": card_id,
                "message": "雷达还没给这张卡返回行情"}

    before = len(q.get("samples") or [])
    after_q = _filter_quote_samples(q, card)
    after = len(after_q.get("samples") or [])

    # 本地永久档案里这张卡有多少条成交（卡淘清空也不受影响）
    archive_n, archive_earliest = 0, ""
    try:
        conn2 = get_db()
        try:
            arow = conn2.execute(
                "SELECT COUNT(*) AS n, MIN(sold_date) AS e FROM market_sales "
                " WHERE player_hint = ?", (card.get("player") or "",)).fetchone()
            archive_n = int(arow["n"] or 0)
            archive_earliest = arow["e"] or ""
        finally:
            conn2.close()
    except sqlite3.Error:
        pass

    return {
        "status": "success",
        "card_id": card_id,
        "player": card.get("player"),
        "parallel": card.get("parallel") or "",
        "samples_before": before,
        "samples_after": after,
        "hidden": before - after,
        "median_rmb": after_q.get("median_rmb"),
        "sample_count": after_q.get("sample_count"),
        "filtered_note": after_q.get("filtered_note") or "",
        "archive_count": archive_n,
        "archive_earliest": archive_earliest,
        "samples": (after_q.get("samples") or [])[:20],
    }



# ==============================================================================
# v3.0 · PWA（手机可加到主屏当 App 用）
# manifest + service worker + 图标，Chrome/Safari 都支持"添加到主屏幕"
# ==============================================================================

import pathlib as _pathlib
from fastapi.responses import FileResponse as _FileResponse
from fastapi.responses import Response as _PlainResp

_TPL_DIR = _pathlib.Path(__file__).resolve().parent / "templates"

_PWA_MANIFEST = {
    "name": "Card Vault 球星卡档案库",
    "short_name": "Card Vault",
    "description": "私人球星卡收藏管理与行情估值",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f172a",
    "theme_color": "#0f172a",
    "lang": "zh-CN",
    "icons": [
        {"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# 刻意不做离线缓存：这是个数据工具，宁可每次联网拿最新数据，
# 也不要让手机上看到几天前的旧估值。service worker 只为满足可安装条件。
_SW_JS = (
    "const CACHE = 'card-vault-shell-v3';\n"
    "self.addEventListener('install', e => { self.skipWaiting(); });\n"
    "self.addEventListener('activate', e => {\n"
    "  e.waitUntil(self.clients.claim());\n"
    "});\n"
    "// 明确走网络：不缓存 HTML，避免用户看到过期页面\n"
    "self.addEventListener('fetch', e => {\n"
    "  if (e.request.method !== 'GET') return;\n"
    "  // 静态图标可缓存，页面与接口一律走网络\n"
    "  if (/\\.(png|ico|svg)$/.test(new URL(e.request.url).pathname)) {\n"
    "    e.respondWith(caches.open(CACHE).then(async c => {\n"
    "      const hit = await c.match(e.request);\n"
    "      if (hit) return hit;\n"
    "      const res = await fetch(e.request);\n"
    "      c.put(e.request, res.clone()).catch(() => {});\n"
    "      return res;\n"
    "    }).catch(() => fetch(e.request)));\n"
    "  }\n"
    "});\n"
)


@app.get("/manifest.json")
def pwa_manifest():
    return _PWA_MANIFEST


@app.get("/sw.js")
def pwa_service_worker():
    return _PlainResp(
        content=_SW_JS.encode("utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


def _serve_icon(name: str):
    p = _TPL_DIR / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="icon missing: " + name)
    return _FileResponse(str(p), media_type="image/png",
                         headers={"Cache-Control": "public, max-age=86400"})


@app.get("/pwa-icon-192.png")
def pwa_icon_192():
    return _serve_icon("pwa-icon-192.png")


@app.get("/pwa-icon-512.png")
def pwa_icon_512():
    return _serve_icon("pwa-icon-512.png")


@app.get("/api/mobile/info")
def mobile_info(request: Request):
    """给手机端用的轻量摘要：一次请求拿到首页需要的所有数字。

    手机上网络比桌面差，能少一个请求就少一个。
    """
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN COALESCE(est_raw_price,0) > 0 THEN 1 ELSE 0 END) AS valued, "
            "       SUM(CASE WHEN COALESCE(cost_price,0) > 0 THEN 1 ELSE 0 END) AS costed, "
            "       COALESCE(SUM(cost_price),0) AS cost, "
            "       COALESCE(SUM(est_raw_price),0) AS value "
            "FROM cards WHERE COALESCE(status,'') != '已售出'").fetchone()
        costed_row = conn.execute(
            "SELECT COALESCE(SUM(cost_price),0) AS cost, COALESCE(SUM(est_raw_price),0) AS value "
            "FROM cards WHERE COALESCE(status,'') != '已售出' AND COALESCE(cost_price,0) > 0"
        ).fetchone()
        top = [dict(r) for r in conn.execute(
            "SELECT card_id, player, year_set, est_raw_price, parallel "
            "FROM cards WHERE COALESCE(status,'') != '已售出' "
            "ORDER BY COALESCE(est_raw_price,0) DESC LIMIT 5").fetchall()]
    finally:
        conn.close()

    total = int(cur["total"] or 0)
    valued = int(cur["valued"] or 0)
    value = float(cur["value"] or 0)
    ccost = float(costed_row["cost"] or 0)
    cval = float(costed_row["value"] or 0)

    return {
        "status": "success",
        "total_value": round(value, 2),
        "cards": total,
        "valued_cards": valued,
        "coverage": round(valued / total * 100, 1) if total else 0,
        "costed_cards": int(cur["costed"] or 0),
        "costed_pnl": round(cval - ccost, 2),
        "costed_pnl_pct": round((cval - ccost) / ccost * 100, 2) if ccost else None,
        "top_cards": top,
        "host": request.headers.get("host", ""),
    }


@app.get("/api/sales/archive-stats")
def archive_stats():
    """成交档案概览：这是对抗卡淘清空的底牌，得能看见。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT player_hint) AS players, "
            "       MIN(sold_date) AS earliest, MAX(sold_date) AS latest, "
            "       COALESCE(SUM(price),0) AS total "
            "FROM market_sales").fetchone()
    finally:
        conn.close()
    return {
        "status": "success",
        "archived_sales": int(row["n"] or 0),
        "players_covered": int(row["players"] or 0),
        "earliest_sale": row["earliest"] or "",
        "latest_sale": row["latest"] or "",
        "total_value_tracked": round(float(row["total"] or 0), 2),
        "note": "本地永久档案，与卡淘是否清空历史无关",
    }



# ==============================================================================
# v3.1 · 手机专用轻量界面 /m
# 主站 5800 行在手机上太重，这里另起一个手机优先的单页：
# 三个 tab（概览 / 卡片 / 统计）+ 底部导航 + 大数字，PWA 可直接加到主屏。
# ==============================================================================

@app.get("/m", response_class=HTMLResponse)
def mobile_page():
    p = _TPL_DIR / "mobile.html"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="mobile.html 缺失")
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@app.get("/api/mobile/ping")
def mobile_ping():
    return {"status": "ok", "page": "/m", "pwa": True}



# ==============================================================================
# v3.2 · 手动补录历史成交
#
# 场景（用户原话）：「很多卡我没有输入成本价…很多成交价是找不到的。
# 比如我那张夏思凝，真的是运气好，首发卖的很贵，但是现在肯定卖不到这个价格」
#
# 卡淘会清空历史，平台清掉的东西我们抓不回来。但你自己记得——
# 首发卖了多少钱、线下成交多少、别的平台成交多少。
# 这些是估值里最稀缺的"真实成交"，必须能手工喂进档案。
#
# 写入 market_sales 并标记 source='manual'，估值时与抓取到的成交同等参与
# 时间衰减计算（老的高价会因半衰期自动弱化，不会被当成当前行情）。
# ==============================================================================


@app.post("/api/sales/manual")
def add_manual_sale(player: str = Form(""), card_id: str = Form(""),
                    price: float = Form(0.0), sold_date: str = Form(""),
                    title: str = Form(""), note: str = Form("")):
    """补录一条历史成交。"""
    try:
        price = float(price or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        raise HTTPException(status_code=400, detail="成交价必须大于 0")

    p = (player or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="必须提供球员名（用于匹配估值）")

    d = _norm_date(sold_date) or _dt.now().strftime("%Y-%m-%d")
    item_id = "manual-%d-%d" % (int(time.time() * 1000), os.getpid())

    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO market_sales "
            "(item_id, keyword, title, price, sold_date, bid_count, url, "
            " player_hint, card_id, source, archived_at, first_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, "manual", (title or "手动补录").strip(), price, d, 0, "",
             p, card_id, "manual", now_str(), now_str()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "item_id": item_id,
        "player": p,
        "price": price,
        "sold_date": d,
        "note": "已写入本地档案，下次重估会自动把它算进去（老成交按时间衰减加权）",
    }


@app.get("/api/sales/manual")
def list_manual_sales(player: str = ""):
    conn = get_db()
    try:
        sql = ("SELECT item_id, player_hint, title, price, sold_date, card_id, archived_at "
               "FROM market_sales WHERE source = 'manual'")
        args = ()
        if player:
            sql += " AND player_hint = ?"
            args = (player,)
        sql += " ORDER BY sold_date DESC, id DESC LIMIT 100"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    return {"status": "success", "items": rows, "count": len(rows)}


@app.delete("/api/sales/manual/{item_id}")
def del_manual_sale(item_id: str):
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM market_sales WHERE item_id = ? AND source = 'manual'",
            (item_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="记录不存在或不是手动补录的")
    finally:
        conn.close()
    return {"status": "success", "deleted": item_id}


@app.get("/api/sales/player-history")
def player_history(player: str = "", limit: int = 30):
    """某个球员在本地档案里的全部成交（含手动补录），用于前端核对。"""
    p = (player or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="缺少 player")
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 30
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT item_id, title, price, sold_date, source, card_id "
            "FROM market_sales WHERE player_hint = ? "
            "ORDER BY sold_date DESC, id DESC LIMIT ?", (p, limit)).fetchall()]
    finally:
        conn.close()
    return {"status": "success", "player": p, "count": len(rows), "items": rows}



# ==============================================================================
# v3.3 · 版别推断修正 + 搜索词重建（郑钦文金折实测驱动的修复）
#
# 实测案例：用户一张 2024 Topps Chrome 郑钦文 1st Card Gold Refractor 09/50，
# 刚以 ¥2500~2800 成交，系统却爬不到。
#
# 三重原因：
#   1) 搜索词写成 "2024 Chrome Zheng /50 金碎冰" —— 金碎冰是**蓝宝石版**的叫法，
#      实物是 Topps Chrome 金折。卡淘 searchKey 逐词 AND，多这一个词就把
#      标题写「金折」的成交全部排除。实测「金碎冰」只召回蓝宝石的 ¥5001/¥4100，
#      换「金折」立刻出现 ¥2800 / ¥8377 / ¥8699 等高價成交。
#   2) 版别推断顺序错了：_infer_parallel 先看编数，有编就返回 "serialized"，
#      把 card_type 里明写的 "Gold Refractor" 丢掉了 → 丢失"金折"这个关键信息。
#   3) 颜色匹配只有英文：parallel="gold" 而中文标题写「金折」，匹配不上。
#
# 本补丁：
#   · 颜色词优先于 refractor（Gold Refractor = 金折，不是泛指折射）
#   · 具体版别优先于编数（有编且写了颜色 → 用颜色）
#   · parallel 值做中英别名映射，gold ↔ 金折/正金折
#   · 新增「按卡种重建搜索词」端点，把错的版别词换掉
# ==============================================================================

# 版别值 → 标题里可能的写法（中英双语 + 常见变体）
_PARALLEL_ALIASES = {
    "gold": ["gold", "金折", "正金折", "金refractor", "金"],
    "silver": ["silver", "银折", "银refractor"],
    "orange": ["orange", "橙折", "橘折"],
    "blue": ["blue", "蓝折", "蓝碎冰", "蓝宝石"],
    "green": ["green", "绿折"],
    "red": ["red", "红折"],
    "purple": ["purple", "紫折"],
    "black": ["black", "黑折"],
    "pink": ["pink", "粉折"],
    "teal": ["teal", "青折"],
    "aqua": ["aqua", "水蓝折"],
    "bronze": ["bronze", "铜折"],
    "platinum": ["platinum", "铂金折"],
    "emerald": ["emerald", "祖母绿"],
    "refractor": ["refractor", "折射", "银折"],
    "x_fractor": ["x-fractor", "x fractor", "x折射"],
    "prizm": ["prizm", "prizm折"],
    "holo": ["holo", "全息"],
    "mojo": ["mojo"],
    "shimmer": ["shimmer", "闪光"],
    "sparkle": ["sparkle"],
    "wave": ["wave", "波纹折"],
    "superfractor": ["superfractor", "超级折"],
    "printing_plate": ["printing plate", "印版"],
}


def _parallel_alias_hit(title, parallel):
    """标题是否命中该版别的任一别名。"""
    up = str(title or "").upper()
    p = str(parallel or "").strip().lower()
    if not p or not up:
        return False
    for alias in _PARALLEL_ALIASES.get(p, [p]):
        if str(alias).upper() in up:
            return True
    return False


@app.post("/api/cards/{card_id}/rebuild-query")
def rebuild_query(card_id: str, use_parallel: bool = Form(True)):
    """按卡片字段重建搜索词，修掉手填的版别错词。

    原则（沿用雷达的召回优先）：广召回 + 代码校验。
    所以词里只放「年份 + 系列核心 + 球员 + 编数」，版别交给估值时过滤，
    不再把颜色名塞进搜索词 —— 塞错的代价就是整条成交都搜不出来。
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        d = dict(row)

        parts = []
        m = re.search(r"(19|20)\d{2}", str(d.get("year_set") or ""))
        if m:
            parts.append(m.group(0))
        core = _series_core(d.get("year_set"))
        if core:
            parts.append(core)
        name = _player_short(d.get("player"))
        if name:
            parts.append(name)
        # 编数**刻意不放进搜索词**：卡淘 searchKey 逐词 AND，
        # 标题没写编数的成交会被整条排除。实测郑钦文金折 ¥2800 那笔
        # 标题就没写 /50，带编数就一条都搜不到。
        # 编数交给估值时按分母校验，召回优先 —— 与雷达的 _recall_queries 保持一致。

        new_q = " ".join(x for x in parts if x).strip()
        if not new_q:
            raise HTTPException(status_code=400, detail="字段不足，拼不出搜索词")

        old_q = (d.get("search_query") or "").strip()
        conn.execute("UPDATE cards SET search_query = ?, updated_at = ? WHERE card_id = ?",
                     (new_q, now_str(), card_id))
        conn.commit()
    finally:
        conn.close()

    _export_vault_inventory()
    return {
        "status": "success",
        "card_id": card_id,
        "old_query": old_q,
        "new_query": new_q,
        "tip": "新版别已剔除颜色词：卡淘逐词 AND，颜色写错会导致整条成交搜不到。",
    }


@app.post("/api/cards/{card_id}/set-parallel")
def set_parallel(card_id: str, parallel: str = Form("")):
    """直接设置版别（供批量修正脚本使用）。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        conn.execute("UPDATE cards SET parallel = ?, updated_at = ? WHERE card_id = ?",
                     (str(parallel or "").strip().lower(), now_str(), card_id))
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "card_id": card_id, "parallel": parallel}


# ---------------------------- 签字 / 非签字 分档 ----------------------------
# 实测教训：郑钦文那张 "1st Card Gold Refractor"（无签字），
# 用宽词搜回来的 12 条里混进了 ¥8377 / ¥8699 / ¥4322 / ¥5099 四笔**签字卡**成交，
# 中位数被抬到 ¥3464，而真实的无签字金折只有 ¥625~¥1609 这个量级。
# 签字与不签字是同款不同卡，价差可达 5~10 倍，必须分开。

_SIGN_RE = re.compile(r"签|autograph|auto\b|signature|签章|墨签|贴签|卡签", re.IGNORECASE)
_NO_SIGN_RE = re.compile(r"(?:无|不带|非|不是)\s*签|no\s*auto", re.IGNORECASE)


def _is_signed_card(title) -> bool:
    """标题是否指向签字卡。先排除"无签/不带签"这类否定写法。"""
    t = str(title or "")
    if not t:
        return False
    if _NO_SIGN_RE.search(t):
        return False
    return bool(_SIGN_RE.search(t))


def _filter_sales_by_signature(items, wants_auto: bool):
    """按是否签字过滤样本。返回 (过滤后列表, 说明)。

    wants_auto=True  → 只留签字成交
    wants_auto=False → 只留非签字成交
    过滤后为空则回退全量（宁可粗一点，也不要让整张卡变成无法估值）。
    """
    kept = []
    for it in items:
        signed = _is_signed_card(it.get("title"))
        if signed == bool(wants_auto):
            kept.append(it)
    if not kept:
        return items, "sign_fallback"
    return kept, ("signed_only" if wants_auto else "unsigned_only")


def _card_wants_auto(card_type, notes="") -> bool:
    """从 card_type / notes 判断这张卡本身是不是签字卡。"""
    text = "%s %s" % (card_type or "", notes or "")
    if _NO_SIGN_RE.search(text):
        return False
    return bool(_SIGN_RE.search(text))


# ---------------------------- 产品线分档（Chrome / 蓝宝石 / Update） ----------------------------
# 实测教训：2024 Topps Chrome 郑钦文金折，搜回来的样本里混进「蓝宝石金碎冰」
# ¥5001 / ¥4100 —— 蓝宝石（Sapphire）是独立产品线，盒子价差好几倍，
# 成交价自然不在一个量级，混算会把普通 Chrome 的估值顶高。
_SAPPHIRE_RE = re.compile(r"蓝宝石|sapphire", re.IGNORECASE)
_UPDATE_RE = re.compile(r"chrome\s*update|tcu\b|crbu|update\s*系列", re.IGNORECASE)


def _filter_sales_by_product_line(items, year_set):
    """按产品线过滤。仅在 year_set 明确属于 Topps Chrome 系时启用。

    · 本卡是蓝宝石 → 只留蓝宝石成交
    · 本卡是普通 Chrome → 排除蓝宝石与 Chrome Update
    """
    ys = str(year_set or "")
    low = ys.lower()
    if "chrome" not in low and "topps" not in low:
        return items

    is_sapphire = bool(_SAPPHIRE_RE.search(ys))
    kept = []
    for it in items:
        title = str(it.get("title") or "")
        has_sap = bool(_SAPPHIRE_RE.search(title))
        has_upd = bool(_UPDATE_RE.search(title))
        if is_sapphire:
            if has_sap:
                kept.append(it)
        else:
            if not has_sap and not has_upd:
                kept.append(it)
    return kept if kept else items


# ---------------------------- 极端价剔除 ----------------------------
# 卡淘上大量 ¥1 / ¥2 / ¥4 的占位标价和整包打包价，不是单卡真实成交。
# 雷达侧早有 _trim_extremes 做这件事，Card Vault 侧补上，口径对齐。
def _trim_price_outliers(items, factor=20.0):
    """按中位数 ±20 倍剔除极端价。样本 <3 条时不动（中位数本身不可信）。"""
    prices = sorted(float(x.get("price") or 0) for x in items if (x.get("price") or 0) > 0)
    if len(prices) < 3:
        return items, "too_few"
    n = len(prices)
    mid = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2.0
    if mid <= 0:
        return items, "no_mid"
    lo, hi = mid / factor, mid * factor
    kept = [x for x in items if lo <= float(x.get("price") or 0) <= hi]
    if not kept:
        return items, "all_outlier_fallback"
    return kept, "trimmed_%d" % (len(items) - len(kept))



# ==============================================================================
# v3.4 · 品牌硬校验 + 1/1 保护  「信息明确就必须算对」
#
# 事故（用户实地指出）：C罗 2022-23 Panini Immaculate Excellence Autographs
# Platinum 1/1，信息完整无误，系统却估成 ¥485。
#
# 实测样本：20 条全是 Topps Platinum / Futera Platinum 的「铂金盒」成交。
#   · 你的卡：Panini 的 Platinum **平行等级**（全球仅 1 张）
#   · 那些成交：Topps / Futera 的 **产品线名**（整盒在卖）
#   同一个英文词「Platinum」，指代完全不同，价格差 3 个数量级。
#
# 用户的原则很清楚：卡牌信息明确时系统必须算对；算不出要如实说算不出，
# 绝不能拿别的品牌、别的产品线的成交凑一个数字出来。
#
# 本补丁：
#   1) 品牌硬校验：Panini 的卡只能拿 Panini 的成交估价，Topps/Futera 同理
#   2) 1/1 保护：全球唯一的卡，没有"同款"可言，无真成交就不给估值
#   3) 样本门槛：过滤后不足 2 条时明确返回 insufficient，而不是给个凑数的数
# ==============================================================================

_BRAND_PATTERNS = [
    ("panini", r"panini|帕尼尼|帕尼"),
    ("topps", r"topps|托普斯|topp\b"),
    ("futera", r"futera"),
    ("upperdeck", r"upper\s*deck|\bud\b|upper-deck"),
    ("bowman", r"bowman|波曼"),
]

# 产品线词：出现在标题里说明是"盒子/系列名"，不是单卡的平行版
_BOX_PRODUCT_RE = re.compile(
    r"铂金盒|白金盒|盒子|整盒|原盒|原封|盒装|一盒|box\b|blaster|mega\s*box|hobby\s*box",
    re.IGNORECASE)


def _brand_of(text) -> str:
    low = str(text or "").lower()
    for name, pat in _BRAND_PATTERNS:
        if re.search(pat, low):
            return name
    return ""


def _filter_sales_by_brand(items, year_set):
    """品牌硬校验。

    Panini 的卡拿 Topps 的成交估价，等于把两条完全不同的产品线混算。
    实测 C罗 Immaculate 1/1 就是这样被 Topps/Futera 的铂金盒成交拉成 ¥485。
    """
    want = _brand_of(year_set)
    if not want:
        return items, "no_brand"

    kept = []
    for it in items:
        title = str(it.get("title") or "")
        got = _brand_of(title)
        # 标题里没写品牌的不砍（很多卖家只写球员+系列），写了不同品牌的才砍
        if not got or got == want:
            kept.append(it)
    # **不回退**：一条同品牌成交都没有，说明这次真的没有可比参照。
    # 回退等于拿别的品牌的价格冒充，正是用户明确反对的"凑数字"。
    return kept, ("brand_%s" % want if kept else "brand_none")


def _filter_box_products(items):
    """剔除整盒/整箱成交。单卡估值不能拿一盒的价当参照。

    同样不回退：如果搜回来的全是整盒，说明搜索方向就错了。
    """
    kept = [it for it in items if not _BOX_PRODUCT_RE.search(str(it.get("title") or ""))]
    return kept, ("box_excluded" if kept else "box_none")


def _is_unique_serial(serial) -> bool:
    """是否 1/1（全球唯一）。"""
    s = str(serial or "").strip()
    return bool(re.match(r"^0*1\s*/\s*1$", s))


@app.get("/api/cards/{card_id}/valuation-check")
def valuation_check(card_id: str, deep: bool = True):
    """诊断一张卡的估值链路：每个环节砍掉了什么、为什么。

    存在的意义：当用户说"这个价不可能"时，能一分钟内看到是哪一步出的问题，
    而不是靠猜。
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        card = dict(row)
    finally:
        conn.close()

    out = {
        "card_id": card_id,
        "player": card.get("player"),
        "year_set": card.get("year_set"),
        "card_type": card.get("card_type"),
        "serial": card.get("serial"),
        "parallel": card.get("parallel"),
        "brand": _brand_of(card.get("year_set")),
        "is_one_of_one": _is_unique_serial(card.get("serial")),
        "wants_auto": _card_wants_auto(card.get("card_type"), card.get("notes")),
        "current_estimate": card.get("est_raw_price"),
    }
    try:
        rv = revalue_card(card_id, apply=False, deep=deep)
        status = rv.get("status")
        out["revaluation"] = {
            "status": status,
            # 定价依据只有两种状态：命中 1:1 同款取最近成交价 / 无同款保留人工估值
            "price_basis": ("latest_exact_sale" if status == "success" else "insufficient"),
            "latest_price": rv.get("latest_price"),
            "latest_sale_date": rv.get("latest_sale_date"),
            "exact_count": rv.get("exact_count"),
            "sample_count": rv.get("sample_count"),
            "confidence": rv.get("confidence"),
            "old_value": rv.get("old_value"),
            "message": rv.get("message"),
            "note": rv.get("valuation_note"),
            "samples": rv.get("filtered_sales"),
        }
    except Exception as exc:                      # noqa: BLE001
        out["revaluation"] = {"status": "error", "message": str(exc)}
    return out

# ============================ 2.1 升级：Checklist 资料库与 PWA 支持 ============================

@app.get("/manifest.json")
def pwa_manifest():
    """PWA Web App Manifest 供手机浏览器添加到主屏幕全屏运行"""
    return JSONResponse(content={
        "name": "Card Vault 球星卡档案库",
        "short_name": "CardVault",
        "description": "球星卡私人档案库与资产估值终端",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#090d16",
        "theme_color": "#090d16",
        "icons": [
            {
                "src": "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/icons/gem.svg",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml"
            }
        ]
    })


@app.get("/api/checklists/search")
def search_checklists(q: str = "", series: str = "", limit: int = 15):
    """检索 Checklist 资料库，支持球员中英文、卡号、系列、球队模糊匹配"""
    conn = get_db()
    try:
        q = (q or "").strip()
        series = (series or "").strip()
        limit = max(1, min(int(limit), 50))
        
        conditions = []
        params = []
        
        if series:
            conditions.append("(series LIKE ? OR year LIKE ?)")
            params.extend([f"%{series}%", f"%{series}%"])
            
        if q:
            conditions.append("(player_en LIKE ? OR player_cn LIKE ? OR card_number LIKE ? OR team LIKE ? OR subset LIKE ? OR notes LIKE ? OR series LIKE ? OR brand LIKE ?)")
            kw = f"%{q}%"
            params.extend([kw, kw, kw, kw, kw, kw, kw, kw])
            
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT id, sport, year, brand, series, card_number, player_en, player_cn,
                   team, subset, is_rookie, is_auto, is_relic, serial_max, parallels, source, notes
            FROM checklists
            {where_clause}
            ORDER BY is_rookie DESC, id ASC
            LIMIT ?
        """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        
        for r in rows:
            if r.get("parallels"):
                try:
                    r["parallels_list"] = json.loads(r["parallels"])
                except Exception:
                    r["parallels_list"] = []
            else:
                r["parallels_list"] = []
                
        return {"status": "success", "count": len(rows), "results": rows}
    finally:
        conn.close()


@app.get("/api/checklists/series")
def list_checklist_series():
    """返回当前资料库中收录的所有系列与收录卡片数"""
    conn = get_db()
    try:
        sql = """
            SELECT series, year, brand, sport, COUNT(*) as card_count
            FROM checklists
            GROUP BY series, year, brand
            ORDER BY card_count DESC
        """
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        total = conn.execute("SELECT COUNT(*) as n FROM checklists").fetchone()["n"]
        return {"status": "success", "total_cards": total, "series": rows}
    finally:
        conn.close()


@app.post("/api/checklists/import-text")
async def import_checklist_text(request: Request):
    """通过 DeepSeek AI 解析微信公众号/官方推文/社群卡表文本，批量结构化入库"""
    try:
        body = await request.json()
    except Exception:
        body = {}
        
    raw_text = (body.get("text") or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="推文或卡表文本不能为空")
        
    default_sport = body.get("sport") or "篮球"
    default_year = body.get("year") or ""
    default_brand = body.get("brand") or "Panini"
    default_series = body.get("series") or ""
    
    prompt = (
        "你是球星卡 Checklist 资料库专家。请把下面这段卡圈公众号/社群的发售推文或卡表文本，提取出结构化卡片清单。\n"
        "返回严格纯 JSON 数组，每个元素包含字段：\n"
        "sport (运动: 篮球/足球/网球/田径/综合), year (年份/赛季如2023-24), brand (品牌如Panini/Topps/奇卡/达卡), series (系列名如Prizm, Select, 凌云), "
        "card_number (卡号数字或代号如136或EA-CR7), player_en (球员英文名), player_cn (球员中文名), team (所属球队), "
        "subset (卡种如Base, Rookie Variations, Signatures), is_rookie (数字0或1), is_auto (数字0或1), is_relic (数字0或1), "
        "parallels (该系列提及的折射版本字符串数组，如 [\"Base (普卡)\", \"Silver Prizm (银折)\", \"Gold /10 (金折)\", \"Black 1/1 (黑折)\"])\n"
        "只输出纯 JSON，不要包含任何 markdown 代码块或解释。\n\n"
        f"待解析推文文本：\n{raw_text[:6000]}"
    )
    
    # 转发给 Scrapling 容器 (8091) 的 LLM 解析能力
    res, err = _scrapling_post("/api/ai/parse-card", {"title": prompt}, timeout=90)
    if res is None or not res.get("parsed"):
        raise HTTPException(status_code=502, detail=f"调用 AI 解析卡表失败: {err}")
        
    parsed_obj = res.get("parsed")
    items = []
    if isinstance(parsed_obj, list):
        items = parsed_obj
    elif isinstance(parsed_obj, dict):
        if "cards" in parsed_obj and isinstance(parsed_obj["cards"], list):
            items = parsed_obj["cards"]
        elif "raw_output" in parsed_obj:
            raw = parsed_obj["raw_output"].strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.splitlines()[1:-1]).strip()
            try:
                items = json.loads(raw)
            except Exception:
                items = []
        else:
            items = [parsed_obj]
            
    conn = get_db()
    inserted = 0
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            c_num = str(it.get("card_number") or "").strip()
            p_en = str(it.get("player_en") or "").strip()
            p_cn = str(it.get("player_cn") or "").strip()
            s_name = str(it.get("series") or default_series or "未知系列").strip()
            if not p_en and not p_cn:
                continue
                
            parallels_json = json.dumps(it.get("parallels") or [], ensure_ascii=False)
            
            # 查重去重
            cur = conn.execute(
                "SELECT id FROM checklists WHERE series = ? AND card_number = ? AND (player_en = ? OR player_cn = ?)",
                (s_name, c_num, p_en, p_cn)
            )
            if cur.fetchone() is None:
                conn.execute("""
                    INSERT INTO checklists (sport, year, brand, series, card_number, player_en, player_cn,
                                           team, subset, is_rookie, is_auto, is_relic, parallels, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    it.get("sport") or default_sport,
                    it.get("year") or default_year,
                    it.get("brand") or default_brand,
                    s_name,
                    c_num,
                    p_en,
                    p_cn,
                    it.get("team") or "",
                    it.get("subset") or "Base",
                    1 if it.get("is_rookie") else 0,
                    1 if it.get("is_auto") else 0,
                    1 if it.get("is_relic") else 0,
                    parallels_json,
                    "wechat_post"
                ))
                inserted += 1
        conn.commit()
    finally:
        conn.close()
        
    return {"status": "success", "parsed_count": len(items), "inserted": inserted}



# ==============================================================================
# v3.6 · 从成交标题反推卡片身份（自动积累 Catalog）
#
# 设计原则（用户思路）：不追求把整个系列 checklist 扒全，
# 只要求「我实际持有的卡」能被准确描述：厂商 + 年份 + 系列 + 卡种 + 平行。
# 每跑一次雷达/重估，market_sales 就多一批真实标题，catalog 随之变厚。
#
# 组合键去重：(brand, year, series, card_number, player, subset)
# —— 与用户给的架构提醒一致：卡表必须先于成交，且要有稳定的身份键。
# ==============================================================================

_BRAND_DICT = [
    ("upper deck", "Upper Deck"), ("panini", "Panini"), ("topps", "Topps"),
    ("futera", "Futera"), ("bowman", "Bowman"), ("donruss", "Donruss"),
    ("score", "Score"), ("奇卡", "奇卡 (QICA)"), ("qica", "奇卡 (QICA)"),
    ("leaf", "Leaf"), ("sage", "SAGE"),
]

# 长词优先：chrome update 必须先于 chrome 命中
_SERIES_DICT = [
    "national treasures", "chrome update", "cosmic chrome", "stadium club",
    "allen & ginter", "crown royale", "crystal premium", "immaculate collection",
    "immaculate", "contenders", "obsidian", "revolution", "spectra", "transcendent",
    "flagship", "chrome", "prizm", "optic", "hoops", "select", "mosaic",
    "finest", "sapphire", "pristine", "absolute", "elite", "noir", "flawless",
    "luxe", "archives", "heritage", "top class", "donruss", "score",
    "bowman", "leaf", "futera", "champion", "museum collection", "merlin",
    "topps now", "unmatched", "resurgence", "tribal", "decade",
]

_PARALLEL_CN = [
    "银折", "金折", "红折", "蓝折", "绿折", "紫折", "黑折", "橙折", "粉折", "白折",
    "青折", "铜折", "铂金折", "折射", "镭射", "碎冰", "星云", "彩虹", "手绘",
    "波纹", "虎纹", "斑马", "闪光", "银闪", "绿闪", "蓝闪", "红闪", "金闪",
    "几何折", "棱镜折", "水波", "泡泡", "全息", "正新秀", "正金折",
]
_PARALLEL_EN = [
    "silver", "gold", "orange", "blue", "green", "red", "purple", "black",
    "pink", "teal", "aqua", "bronze", "platinum", "refractor", "prizm",
    "holo", "mojo", "shimmer", "sparkle", "wave", "scope", "ice", "sapphire",
    "superfractor", "xfractor", "x-fractor",
]

_YEAR_RE2 = re.compile(r"(\d{4})(?:-(\d{2,4}))?")
_CARDNUM_RE = re.compile(r"#\s*([A-Za-z]{0,4}-?\d{1,4})")
_SERIAL_RE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,4})|/\s*(\d{1,4})\s*编|(1)\s*/\s*1")


def _parse_card_identity(title: str):
    """从标题提取 (brand, year, series, card_number, subset, parallels, serial_max)。"""
    t = str(title or "")
    low = t.lower()

    brand = ""
    for key, canon in _BRAND_DICT:
        if key in low:
            brand = canon
            break

    year = ""
    for m in _YEAR_RE2.finditer(t):
        y = int(m.group(1))
        if 1950 <= y <= 2100:
            year = m.group(1) + ("-" + m.group(2) if m.group(2) else "")
            break

    series = ""
    for name in _SERIES_DICT:
        if name in low:
            series = name.title()
            break

    card_number = ""
    m = _CARDNUM_RE.search(t)
    if m:
        card_number = m.group(1).upper()

    subset_parts = []
    if re.search(r"\brc\b|新秀", low):
        subset_parts.append("RC")
    if re.search(r"签字|auto|autograph|卡签|贴签", low):
        subset_parts.append("Auto")
    if re.search(r"patch|物料|球衣|relic|jersey", low):
        subset_parts.append("Patch")
    if re.search(r"\bbase\b|普卡", low):
        subset_parts.append("Base")
    subset = " ".join(subset_parts) or "Base"

    parallels = []
    for w in _PARALLEL_CN:
        if w in t:
            parallels.append(w)
    for w in _PARALLEL_EN:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            parallels.append(w.title())
    parallels = list(dict.fromkeys(parallels))[:8]

    serial_max = None
    ms = _SERIAL_RE.search(t)
    if ms:
        denom = ms.group(2) or ms.group(3)
        if denom:
            try:
                serial_max = int(denom)
            except ValueError:
                serial_max = None
        elif ms.group(4):
            serial_max = 1

    return brand, year, series, card_number, subset, parallels, serial_max


def _guess_sport_from_player(player: str) -> str:
    p = str(player or "")
    if re.search(r"Zheng|Sinner|Alcaraz|Djokovic|Nadal|Swiatek|Rybakina|Jovic|Andreeva|Krueger|孙璐璐", p):
        return "网球"
    if re.search(r"Ronaldo|Messi|Mbappé|Mbappe|Kroos|Haaland|Félix|Felix|Dembélé|Bellingham", p):
        return "足球"
    if re.search(r"James|Curry|Doncic|Dončić|Wembanyama|Flagg|Holmgren|Durant|Bryant|McGrady|Jabbar", p):
        return "篮球"
    if re.search(r"夏思凝|王楚钦|孙颖莎|李梦", p):
        return "综合"
    return ""


@app.post("/api/catalog/harvest")
def catalog_harvest(limit: int = 3000, min_price: float = 1.0):
    """从成交标题反推卡片身份，写入 checklists（catalog）。

    只处理带 player_hint 的成交 —— 那是「我们库里真实关心的卡」，
    别把整站成交都塞进来。默认取最近的 N 条。
    """
    try:
        limit = max(50, min(int(limit), 20000))
    except (TypeError, ValueError):
        limit = 3000

    conn = get_db()
    parsed = 0
    inserted = 0
    skipped_no_brand = 0
    try:
        rows = conn.execute(
            "SELECT title, player_hint, price FROM market_sales "
            "WHERE COALESCE(player_hint,'') != '' AND COALESCE(title,'') != '' "
            "  AND COALESCE(price,0) >= ? "
            "ORDER BY id DESC LIMIT ?", (float(min_price or 0), limit)).fetchall()

        for r in rows:
            title = r["title"] or ""
            player = r["player_hint"] or ""
            brand, year, series, cnum, subset, parallels, serial_max = _parse_card_identity(title)
            parsed += 1
            if not brand:
                skipped_no_brand += 1
                continue

            p_en = ""
            m = re.search(r"([A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+){0,2})", player)
            if m:
                p_en = m.group(1)
            p_cn = re.sub(r"\([^)]*\)", "", player).strip()

            cur = conn.execute(
                "SELECT id FROM checklists WHERE "
                "COALESCE(brand,'')=? AND COALESCE(year,'')=? AND COALESCE(series,'')=? "
                "AND COALESCE(card_number,'')=? AND COALESCE(player_en,'')=?",
                (brand, year, series, cnum, p_en))
            if cur.fetchone() is not None:
                continue

            conn.execute(
                "INSERT INTO checklists "
                "(sport, year, brand, series, card_number, player_en, player_cn, team, "
                " subset, is_rookie, is_auto, is_relic, serial_max, parallels, source, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_guess_sport_from_player(player), year, brand, series, cnum,
                 p_en, p_cn, "",
                 subset,
                 1 if "RC" in subset else 0,
                 1 if "Auto" in subset else 0,
                 1 if "Patch" in subset else 0,
                 serial_max,
                 json.dumps(parallels, ensure_ascii=False),
                 "harvested",
                 "自动从成交标题反推"))
            inserted += 1

        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM checklists").fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "success",
        "scanned": parsed,
        "inserted": inserted,
        "skipped_no_brand": skipped_no_brand,
        "catalog_total": total,
        "note": "只从带 player_hint 的真实成交里反推，不扒整站",
    }



# ==============================================================================
# v3.7 · Checklist 分层浏览
#
# 用户反馈：2,214 条平铺出来看不清。改成三层钻取：
#   维度（运动/品牌/年份/系列/球星）→ 该维度分组及数量 → 组内卡片
# ==============================================================================

_TREE_DIMS = {
    "sport": "sport",
    "brand": "brand",
    "year": "year",
    "series": "series",
    "player": "player_cn",
}


@app.get("/api/checklists/tree")
def checklists_tree(dim: str = "series", limit: int = 200):
    """返回某个维度下的分组统计（按数量倒序）。"""
    col = _TREE_DIMS.get(str(dim or "").strip().lower())
    if not col:
        raise HTTPException(status_code=400, detail="不支持的维度，可选：sport/brand/year/series/player")

    try:
        limit = max(10, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(%s),''),'(未标注)') AS k, COUNT(*) AS n "
            "FROM checklists GROUP BY k ORDER BY n DESC LIMIT ?" % col,
            (limit,)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM checklists").fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "success",
        "dim": dim,
        "total": total,
        "groups": [{"key": r["k"], "count": r["n"]} for r in rows],
    }


@app.get("/api/checklists/browse")
def checklists_browse(dim: str = "", value: str = "", q: str = "",
                      offset: int = 0, limit: int = 40):
    """浏览某一分组内的卡片（分页）。dim 为空则浏览全部。"""
    try:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        offset, limit = 0, 40

    col = _TREE_DIMS.get(str(dim or "").strip().lower())

    conn = get_db()
    try:
        where = []
        args = []

        if col and value:
            if value == "(未标注)":
                where.append("COALESCE(NULLIF(TRIM(%s),''),'(未标注)') = ?" % col)
            else:
                where.append("COALESCE(NULLIF(TRIM(%s),''),'(未标注)') = ?" % col)
            args.append(value)

        if q:
            where.append("(player_cn LIKE ? OR player_en LIKE ? OR card_number LIKE ? "
                         "OR series LIKE ? OR brand LIKE ? OR notes LIKE ?)")
            kw = "%%%s%%" % q
            args.extend([kw] * 6)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            "SELECT id, sport, year, brand, series, card_number, player_en, player_cn, "
            "       team, subset, is_rookie, is_auto, serial_max, parallels, source "
            "FROM checklists" + clause + " ORDER BY id ASC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()

        total = conn.execute("SELECT COUNT(*) FROM checklists" + clause, args).fetchone()[0]
    finally:
        conn.close()

    out = []
    for r in rows:
        d = dict(r)
        try:
            d["parallels_list"] = json.loads(d.get("parallels") or "[]")
        except (ValueError, TypeError):
            d["parallels_list"] = []
        out.append(d)

    return {
        "status": "success",
        "dim": dim,
        "value": value,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(out)) < total,
        "results": out,
    }



# ==============================================================================
# v3.8 · 评级证书档案（cert + pop）
#
# 为什么不自动抓：PSA/BGS 的 cert 页有 Cloudflare 挑战，
# FlareSolverr 实测 130 秒超时（两次独立尝试均失败），不适合自动化；
# 且该服务与媒体刮削器共用，不应长期占用。
# 因此提供「登记 + 关联 + 查询」，数据由人工确认，来源可追溯。
# ==============================================================================

def _init_cert_table():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graded_certs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cert_number TEXT UNIQUE,
                grader TEXT,
                grade REAL DEFAULT 0,
                pop_total INTEGER DEFAULT 0,
                pop_higher INTEGER DEFAULT 0,
                card_desc TEXT,
                player TEXT,
                year TEXT,
                brand TEXT,
                series TEXT,
                card_number TEXT,
                card_id TEXT,
                note TEXT,
                source TEXT DEFAULT 'manual',
                checked_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cert_num ON graded_certs(cert_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cert_card ON graded_certs(card_id)")
        conn.commit()
    finally:
        conn.close()


_init_cert_table()


@app.post("/api/certs")
def add_cert(cert_number: str = Form(...), grader: str = Form("PSA"),
             grade: float = Form(0.0), pop_total: int = Form(0),
             pop_higher: int = Form(0), card_desc: str = Form(""),
             player: str = Form(""), year: str = Form(""), brand: str = Form(""),
             series: str = Form(""), card_number: str = Form(""),
             card_id: str = Form(""), note: str = Form("")):
    """登记一张评级证书。cert_number 唯一，重复登记会更新 pop 数据。"""
    cn = str(cert_number or "").strip().lstrip("#")
    if not cn:
        raise HTTPException(status_code=400, detail="证书号不能为空")

    g = str(grader or "PSA").strip().upper()
    now = now_str()

    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM graded_certs WHERE cert_number = ?", (cn,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO graded_certs (cert_number, grader, grade, pop_total, pop_higher, "
                " card_desc, player, year, brand, series, card_number, card_id, note, "
                " source, checked_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cn, g, float(grade or 0), int(pop_total or 0), int(pop_higher or 0),
                 card_desc, player, year, brand, series, card_number, card_id, note,
                 "manual", now, now, now))
            action = "created"
        else:
            conn.execute(
                "UPDATE graded_certs SET grader=?, grade=?, pop_total=?, pop_higher=?, "
                " card_desc=?, player=?, year=?, brand=?, series=?, card_number=?, "
                " card_id=CASE WHEN ? != '' THEN ? ELSE card_id END, "
                " note=?, checked_at=?, updated_at=? WHERE cert_number=?",
                (g, float(grade or 0), int(pop_total or 0), int(pop_higher or 0),
                 card_desc, player, year, brand, series, card_number,
                 card_id, card_id, note, now, now, cn))
            action = "updated"
        conn.commit()
        out = dict(conn.execute("SELECT * FROM graded_certs WHERE cert_number = ?", (cn,)).fetchone())
    finally:
        conn.close()

    return {"status": "success", "action": action, "cert": out}


@app.get("/api/certs")
def list_certs(q: str = "", limit: int = 100):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    conn = get_db()
    try:
        if q:
            kw = "%%%s%%" % q
            rows = conn.execute(
                "SELECT * FROM graded_certs WHERE cert_number LIKE ? OR player LIKE ? "
                " OR card_desc LIKE ? OR series LIKE ? ORDER BY id DESC LIMIT ?",
                (kw, kw, kw, kw, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM graded_certs ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"status": "success", "count": len(items), "items": items}


@app.get("/api/certs/by-card/{card_id}")
def certs_by_card(card_id: str):
    """查某张卡关联的评级证书（估值时可作溢价参考）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM graded_certs WHERE card_id = ? ORDER BY grade DESC",
            (card_id,)).fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"status": "success", "card_id": card_id, "count": len(items), "items": items}


@app.delete("/api/certs/{cert_id}")
def del_cert(cert_id: int):
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM graded_certs WHERE id = ?", (cert_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="证书不存在")
    finally:
        conn.close()
    return {"status": "success", "deleted": cert_id}


@app.get("/api/certs/pop-stats")
def cert_pop_stats():
    """pop 概览：多少张评级卡、各分数量、稀缺度分布。"""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM graded_certs").fetchone()[0]
        by_grader = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(grader,''),'未知') AS g, COUNT(*) AS n "
            "FROM graded_certs GROUP BY g ORDER BY n DESC").fetchall()]
        scarce = [dict(r) for r in conn.execute(
            "SELECT cert_number, grader, grade, pop_total, pop_higher, player, card_desc "
            "FROM graded_certs WHERE pop_total > 0 ORDER BY pop_total ASC LIMIT 20").fetchall()]
        linked = conn.execute(
            "SELECT COUNT(*) FROM graded_certs WHERE COALESCE(card_id,'') != ''").fetchone()[0]
    finally:
        conn.close()
    return {"status": "success", "total": total, "linked_to_cards": linked,
            "by_grader": by_grader, "scarcest": scarce}



# ==============================================================================
# v3.9 · 运动分级样本门槛 + 单张证书查询
#
# 门槛依据用户的外部调研：足球卡样本稀疏，至少 5 笔才出周中位；篮球 3 笔即可。
# 注意：本轮只提供判定函数，不改动估值主链路（改核心逻辑风险高，
# 且当前 insufficient 已有「样本 < 2 不出价」的硬底线兜着）。
# ==============================================================================

_MIN_SAMPLES_BY_SPORT = {
    "足球": 5,
    "篮球": 3,
    "网球": 3,
    "综合": 3,
}


def _min_samples_for_sport(sport) -> int:
    """该运动至少要几笔成交，才允许给出中位数。"""
    key = str(sport or "").strip()
    for name, n in _MIN_SAMPLES_BY_SPORT.items():
        if name in key:
            return n
    return 3


@app.get("/api/thresholds")
def get_thresholds():
    """各运动的最少样本门槛（前端可据此提示用户）。"""
    return {"status": "success", "thresholds": _MIN_SAMPLES_BY_SPORT, "default": 3}


@app.get("/api/cert/{cert_number}")
def get_cert(cert_number: str):
    """按证书号查单张评级证书档案。"""
    cn = str(cert_number or "").strip().lstrip("#")
    if not cn:
        raise HTTPException(status_code=400, detail="证书号不能为空")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM graded_certs WHERE cert_number = ?", (cn,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="没有这张证书的档案")
    return {"status": "success", "cert": dict(row)}



# ==============================================================================
# v4.0 · Checklist URL 导入
#
# 实测各源结果：
#   Beckett / TCDB      → Cloudflare 403（FlareSolverr 130s 超时，放弃）
#   Cardboard Connection → 200，但只有配置/平行，无球员名单
#   sportscardchecklist  → 200，但名单是图片/JS
#   checklistcenter.com  → 200，纯文本名单，可解析（F1 实测 180 张全中）
#
# 因此默认走 checklistcenter.com；其他源若结构适配可继续加。
# ==============================================================================

_CHECKLIST_NOISE = {
    "total cards", "cards", "card", "inserts", "insert", "auto", "autos",
    "autograph", "autographs", "encased", "box", "boxes", "pack", "packs",
    "parallel", "parallels", "base", "set", "relic", "relics", "jersey",
    "jerseys", "hobby", "blaster", "mega", "case", "per", "and", "the", "with",
}
_CHECKLIST_NOISE_PHRASE = (
    "cards per", "per box", "per pack", "per case", "total cards",
    "encased cards", "box", "pack", "case", "checklist", "parallel",
    "autograph", "insert", "base set",
)

_CARD_LINE_RE = re.compile(
    r"^(\d{1,3})[.\-\s]+([A-Z][A-Za-z'.\-\u00C0-\u024F]+(?:\s+[A-Za-z'.\-\u00C0-\u024F]+){0,3})$"
)


def _parse_checklist_html(html: str):
    """从 checklist 页面提取 [{'num': '1', 'name': 'Lando Norris'}, ...]"""
    t = html
    t = re.sub(r"<script[\s\S]*?</script>", " ", t, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|h\d|li|tr)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&#8217;", "'").replace("&#8211;", "-").replace("&amp;", "&").replace("&nbsp;", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)

    out = []
    seen = set()
    for line in t.split("\n"):
        line = line.strip()
        if not line or len(line) > 60:
            continue
        m = _CARD_LINE_RE.match(line)
        if not m:
            continue
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        name = m.group(2).strip()
        if num < 1 or num > 400:
            continue
        low = name.lower()
        if low in _CHECKLIST_NOISE or len(name) < 3:
            continue
        if any(p in low for p in _CHECKLIST_NOISE_PHRASE):
            continue
        key = (num, low)
        if key in seen:
            continue
        seen.add(key)
        out.append({"num": str(num), "name": name})
    return out


def _guess_meta_from_title(title: str):
    """从页面标题猜 年份 / 品牌 / 系列。"""
    year = ""
    my = re.search(r"(\d{4})(?:-(\d{2,4}))?", title)
    if my:
        year = my.group(1) + ("-" + my.group(2) if my.group(2) else "")

    brand = ""
    for b in ["Topps", "Panini", "Upper Deck", "Bowman", "Donruss", "Fleer", "Leaf", "Futera", "Score"]:
        if b.lower() in title.lower():
            brand = b
            break

    series = re.sub(r"(Card\s*Checklist.*|Checklist.*|Set Review.*|Review.*)$", "", title, flags=re.I).strip()
    series = re.sub(r"^\d{4}(-\d{2,4})?\s*", "", series).strip()
    series = re.sub(r"^(" + "|".join(["Topps", "Panini", "Upper Deck", "Bowman"]) + r")\s*", "", series, flags=re.I).strip()
    series = re.sub(r"\s*-\s*$", "", series).strip()
    return year, brand, series or title[:40]


@app.post("/api/checklist/fetch")
async def checklist_fetch(url: str = Form(...), sport: str = Form(""), dry_run: bool = Form(False)):
    """抓取一个 checklist 页面，解析球员名单并入库。

    只从可信源抓，不扒站；单页单次，不做批量爬取。
    """
    u = (url or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="链接不能为空")
    if not u.startswith("http://") and not u.startswith("https://"):
        raise HTTPException(status_code=400, detail="需要 http(s) 链接")

    allowed = ("checklistcenter.com", "cardboardconnection.com")
    if not any(h in u for h in allowed):
        raise HTTPException(
            status_code=400,
            detail="目前只支持 checklistcenter.com / cardboardconnection.com（其余源有 Cloudflare 或图片卡表）")

    try:
        import urllib.request as _ur

        req = _ur.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with _ur.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=502, detail="抓取失败：HTTP %s" % resp.status)
            html = resp.read().decode("utf-8", "ignore")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="抓取异常：%s" % exc)

    tm = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.I)
    title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
    cards = _parse_checklist_html(html)

    year, brand, series = _guess_meta_from_title(title)
    want_sport = (sport or "").strip()

    if dry_run or not cards:
        return {
            "status": "success",
            "dry_run": True,
            "title": title,
            "url": u,
            "parsed": len(cards),
            "year": year,
            "brand": brand,
            "series": series,
            "sample": cards[:15],
            "note": ("解析到卡片，未写入" if cards else
                     "这个页面没有可解析的纯文本卡表（可能是图片或 JS 渲染）"),
        }

    conn = get_db()
    inserted = 0
    skipped = 0
    try:
        for c in cards:
            cur = conn.execute(
                "SELECT id FROM checklists WHERE COALESCE(brand,'')=? AND COALESCE(year,'')=? "
                "AND COALESCE(series,'')=? AND COALESCE(card_number,'')=?",
                (brand, year, series, c["num"]))
            if cur.fetchone() is not None:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO checklists "
                "(sport, year, brand, series, card_number, player_en, player_cn, team, "
                " subset, is_rookie, is_auto, is_relic, serial_max, parallels, source, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (want_sport, year, brand, series, c["num"], c["name"], "",
                 "", "Base", 0, 0, 0, None, "[]", "checklistcenter", "URL 导入：" + title[:60]))
            inserted += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM checklists").fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "success",
        "title": title,
        "url": u,
        "year": year,
        "brand": brand,
        "series": series,
        "parsed": len(cards),
        "inserted": inserted,
        "skipped_duplicate": skipped,
        "catalog_total": total,
    }


# ---- Topps 官方发售与抽签活动查询 API (2026-11-16 新增) ----
@app.get("/api/topps/drops")
async def api_topps_drops():
    """获取 Topps 官方放货与抽签最新活动列表 (Topps收藏卡小程序 & 淘宝TOPPS官方旗舰店)。"""
    drops_path = "/app/data/radar/topps_drops.json"
    if not os.path.exists(drops_path):
        drops_path = os.path.join(os.path.dirname(__file__), "..", "..", "cardhobby_radar", "data", "topps_drops.json")
    if os.path.exists(drops_path):
        try:
            with open(drops_path, "r", encoding="utf-8") as f:
                return {"status": "success", "drops": json.load(f)}
        except Exception as e:
            return {"status": "error", "error": str(e), "drops": []}
    return {"status": "success", "drops": []}

