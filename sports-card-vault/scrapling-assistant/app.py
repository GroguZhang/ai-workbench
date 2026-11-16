import os
import json
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Card Vault Scrapling Assistant", version="1.3.0")

DSH_API_BASE = os.getenv("DSH_API_BASE", "http://localhost:18899/v1")
DSH_API_KEY = os.getenv("DSH_API_KEY", "")
VAULT_API_BASE = os.getenv("VAULT_API_BASE", "http://localhost:8088")
DSH_MODEL = os.getenv("DSH_MODEL", "deepseek-official/deepseek-flash")

NL = chr(10)
FENCE = chr(96) * 3
ITEM_URL = "https://www.cardhobby.com.cn/market/item/"
SEARCH_API = "https://www.cardhobby.com.cn/NewCommodity/SearchCommodity"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.cardhobby.com.cn/market/search",
    "Origin": "https://www.cardhobby.com.cn",
}


class ScrapeRequest(BaseModel):
    keyword: str
    max_items: int = 20
    mode: str = "active"


class CardParseRequest(BaseModel):
    title: str = ""
    raw_title: str | None = None
    keyword: str | None = None


def clean_fence(text):
    out = (text or "").strip()
    if not out.startswith(FENCE):
        return out
    lines = out.splitlines()
    if lines and lines[0].strip().startswith(FENCE):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith(FENCE):
        lines = lines[:-1]
    return NL.join(lines).strip()


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "version": "1.3.0",
        "dsh_api_base": DSH_API_BASE,
        "dsh_model": DSH_MODEL,
        "vault_api_base": VAULT_API_BASE,
    }


@app.post("/api/ai/parse-card")
async def parse_card(req: CardParseRequest):
    prompt = (
        "你是体育球卡鉴定与估值专家。把下面这条卡淘标题解析成 JSON，"
        "只输出 JSON，不要代码块，不要解释。" + NL
        + "字段：sport(运动), player(球员中英文), year_set(年份+系列), "
        "card_type(版别/签字类型), serial(编数,如1/1或/10,未知填待查), "
        "defect_note(瑕疵/状态,没有填无), search_query(用于查成交价的精炼关键词)"
        + NL + NL + "标题：" + (req.title or req.raw_title or req.keyword or "")
    )
    headers = {
        "Authorization": "Bearer " + DSH_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": DSH_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(
                DSH_API_BASE + "/chat/completions", headers=headers, json=payload
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="调用 DSH 失败: " + str(exc))

    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="DSH 返回 " + str(res.status_code) + ": " + res.text[:300],
        )

    data = res.json()
    content = clean_fence(data["choices"][0]["message"]["content"])
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = {"raw_output": content}
    return {"status": "success", "raw_title": (req.title or req.raw_title or req.keyword or ""), "parsed": parsed}


@app.post("/api/scrape/cardhobby")
async def scrape_cardhobby(req: ScrapeRequest):
    """调用卡淘搜索 JSON 接口（与捡漏雷达同一端点，稳定可用）。

    mode=active  → Status=1，在拍/在售，按最快结束排序
    mode=sold    → Status=-2，已成交，按最近成交排序（LowestPrice 才是成交价）
    """
    sold = str(req.mode).lower() == "sold"
    status_value = -2 if sold else 1
    page_size = req.max_items if req.max_items and req.max_items > 0 else 20
    if page_size > 60:
        page_size = 60

    params = {
        "userId": "",
        "pageIndex": 1,
        "pageSize": page_size,
        "searchKey": req.keyword,
        "searchJson": json.dumps(
            [{"Key": "Status", "Value": status_value}], separators=(",", ":")
        ),
        "sort": "EffectiveTimeStamp",
        "sortType": "desc" if sold else "asc",
    }

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=30.0, follow_redirects=True
        ) as client:
            res = await client.get(SEARCH_API, params=params)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="请求卡淘接口失败: " + str(exc))

    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="卡淘接口返回 " + str(res.status_code),
        )

    try:
        payload = res.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="卡淘返回非 JSON（疑似风控兜底页，长度 "
            + str(len(res.text))
            + "）",
        )

    root = payload.get("data") or payload.get("Data") or {}
    rows = root.get("PagedMarketItemList") or []
    total = root.get("TotalCount") or 0

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("ID") or row.get("id") or "")
        title = str(row.get("Title") or row.get("title") or "").strip()
        if not item_id or not title:
            continue
        if sold:
            price = row.get("LowestPrice")
        else:
            price = row.get("Price")
        try:
            price = float(price)
        except Exception:
            price = 0.0
        items.append(
            {
                "id": item_id,
                "title": title,
                "price": price,
                "bid_count": row.get("PriceCount") or 0,
                "end_time": str(
                    row.get("EffectiveDate") or row.get("EndTime") or ""
                ),
                "url": ITEM_URL + item_id,
            }
        )
        if len(items) >= page_size:
            break

    return {
        "status": "success",
        "source": "cardhobby-json-api",
        "keyword": req.keyword,
        "mode": "sold" if sold else "active",
        "total_count": total,
        "count": len(items),
        "items": items,
    }

class VisionParseRequest(BaseModel):
    image_b64: str


@app.post("/api/ai/vision-card")
async def vision_card(req: VisionParseRequest):
    prompt = (
        "你是体育球卡鉴定专家。观察这张球卡正面，提取信息并返回严格纯JSON，不要代码块："
        + NL
        + "{"
        + NL
        + '  "sport": "运动(篮球/足球/网球/棒球等)",'
        + NL
        + '  "player": "球员中英文姓名",'
        + NL
        + '  "team": "球队名称",'
        + NL
        + '  "year": "年份/赛季(如2023-24)",'
        + NL
        + '  "set_name": "系列(如Prizm, Topps Chrome)",'
        + NL
        + '  "card_set": "版型/特卡名称(如Base, Silver Prizm, Mojo)",'
        + NL
        + '  "parallel": "平行折射版本",'
        + NL
        + '  "serial_num": null,'
        + NL
        + '  "serial_total": null,'
        + NL
        + '  "is_rookie": false,'
        + NL
        + '  "is_auto": false,'
        + NL
        + '  "is_patch": false,'
        + NL
        + '  "graded": "Raw",'
        + NL
        + '  "search_query": "精炼卡淘搜索词"'
        + NL
        + "}"
        + NL
        + "注意：serial_num与serial_total为整数或null；is_rookie/is_auto/is_patch为布尔值；评级卡写明公司和分数如PSA 10，裸卡写Raw。"
    )
    headers = {
        "Authorization": "Bearer " + DSH_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": "deepseek-official/deepseek-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + req.image_b64
                        },
                    },
                ],
            }
        ],
        "max_tokens": 4000,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(
                DSH_API_BASE + "/chat/completions", headers=headers, json=payload
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="调用视觉模型失败: " + str(exc))

    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="视觉模型返回 " + str(res.status_code) + ": " + res.text[:300],
        )

    data = res.json()
    content = clean_fence(data["choices"][0]["message"]["content"])
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = {"raw_output": content}
    return {"status": "success", "parsed": parsed}


ROOT_HTML = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Card Vault Scrapling Assistant</title>\n<style>\nbody{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;\nbackground:#0f172a;color:#e2e8f0;margin:0;padding:40px 20px}\n.wrap{max-width:780px;margin:0 auto}\nh1{font-size:22px;margin:0 0 6px}\n.badge{display:inline-block;background:#16a34a;color:#fff;border-radius:999px;\npadding:2px 10px;font-size:12px;vertical-align:middle;margin-left:8px}\np.sub{color:#94a3b8;margin:0 0 24px;font-size:14px}\ntable{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden}\nth,td{text-align:left;padding:11px 14px;font-size:14px;border-bottom:1px solid #334155}\nth{background:#273449;color:#cbd5e1;font-weight:600}\ntr:last-child td{border-bottom:none}\ncode{background:#0f172a;padding:2px 6px;border-radius:5px;font-size:13px;color:#7dd3fc}\na{color:#38bdf8;text-decoration:none}\n.note{margin-top:22px;font-size:13px;color:#94a3b8;line-height:1.7}\n</style></head><body><div class="wrap">\n<h1>Card Vault Scrapling Assistant<span class="badge">running</span></h1>\n<p class="sub">卡淘抓取与 AI 球卡解析服务 · v1.2.1</p>\n<table>\n<tr><th>方法</th><th>路径</th><th>说明</th></tr>\n<tr><td>GET</td><td><code>/health</code></td><td>健康检查与依赖状态</td></tr>\n<tr><td>GET</td><td><code>/docs</code></td><td>交互式接口文档（可直接调试）</td></tr>\n<tr><td>POST</td><td><code>/api/scrape/cardhobby</code></td><td>body: keyword / max_items / mode(active|sold)</td></tr>\n<tr><td>POST</td><td><code>/api/ai/parse-card</code></td><td>body: title</td></tr>\n<tr><td>POST</td><td><code>/api/ai/vision-card</code></td><td>body: image_b64（纯 base64，不含 data: 前缀）</td></tr>\n</table>\n<div class="note">看到本页面说明服务正常，接口调试请打开 <a href="/docs">/docs</a>。<br>\n根路径原本返回 404，本页为后补的说明页。</div>\n</div></body></html>'


@app.get("/", response_class=HTMLResponse)
def index():
    return ROOT_HTML


# ==============================================================================
# v1.3.0：FMV 公允估值 · PSA 四维预评级 · 一键上架文案
# 零新增依赖（仅标准库 + 已装 httpx / fastapi）
# ==============================================================================

import math
import statistics
from fastapi.middleware.cors import CORSMiddleware

# Card Vault 前端（8088）需跨域直连，放开 CORS；allow_credentials 保持 False 以兼容通配 Origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

VERSION_130 = "1.3.0"
# 捡漏判定门槛：FMV 低于此值说明样本是同款混杂，谈折扣没有意义
MIN_FMV_FOR_BARGAIN = 20.0
# 卡淘 1 元起拍是占位价，不代表真实市场价，必须过滤
MIN_PRICE_FOR_BARGAIN = 3.0


async def _fetch_cardhobby(keyword, mode="sold", page_size=40):
    """内部复用：按关键词取卡淘结构化条目（sold=成交 / active=在拍）。"""
    sold = str(mode).lower() == "sold"
    status_value = -2 if sold else 1
    try:
        page_size = int(page_size)
    except Exception:
        page_size = 20
    page_size = max(1, min(page_size, 60))

    params = {
        "userId": "",
        "pageIndex": 1,
        "pageSize": page_size,
        "searchKey": keyword,
        "searchJson": json.dumps(
            [{"Key": "Status", "Value": status_value}], separators=(",", ":")
        ),
        "sort": "EffectiveTimeStamp",
        "sortType": "desc" if sold else "asc",
    }
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=30.0, follow_redirects=True
        ) as client:
            res = await client.get(SEARCH_API, params=params)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="请求卡淘失败: " + str(exc))

    if res.status_code != 200:
        raise HTTPException(status_code=502, detail="卡淘接口返回 %d" % res.status_code)

    try:
        payload = res.json()
    except Exception:
        raise HTTPException(status_code=502, detail="卡淘返回非 JSON（疑似风控页）")

    root = payload.get("data") or payload.get("Data") or {}
    rows = root.get("PagedMarketItemList") or []
    total = root.get("TotalCount") or 0

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("ID") or row.get("id") or "")
        title = str(row.get("Title") or row.get("title") or "").strip()
        if not item_id or not title:
            continue
        raw_price = row.get("LowestPrice") if sold else row.get("Price")
        try:
            price = round(float(raw_price), 2)
        except Exception:
            price = 0.0
        items.append({
            "id": item_id,
            "title": title,
            "price": price,
            "bid_count": int(row.get("PriceCount") or 0),
            "end_time": str(row.get("EffectiveDate") or row.get("EndTime") or ""),
            "url": ITEM_URL + item_id,
        })
        if len(items) >= page_size:
            break
    return {"items": items, "total_count": total}


def _iqr_filter(prices):
    """四分位距过滤：剔除 0.01 元测试拍、恶意托价等离群值。"""
    vals = [float(p) for p in prices if p and float(p) > 0]
    if len(vals) < 4:
        return vals
    s = sorted(vals)
    n = len(s)
    q1 = s[n // 4]
    q3 = s[(3 * n) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return vals
    lo = max(0.5, q1 - 1.5 * iqr)
    hi = q3 + 1.5 * iqr
    kept = [p for p in vals if lo <= p <= hi]
    return kept or vals


def _weighted_fmv(prices):
    """时间衰减加权公允估值：首项为最近成交，权重 exp(-0.15*i)。"""
    if not prices:
        return 0.0
    weights = [math.exp(-0.15 * i) for i in range(len(prices))]
    tw = sum(weights)
    if tw <= 0:
        return round(float(prices[0]), 2)
    return round(sum(p * w for p, w in zip(prices, weights)) / tw, 2)


def _trend_of(prices):
    if len(prices) < 3:
        return "数据不足 ⏳", 0.0
    recent = statistics.mean(prices[:2])
    older = statistics.mean(prices[2:])
    if older <= 0:
        return "平稳 ➡️", 0.0
    ratio = (recent - older) / older
    pct = round(ratio * 100, 1)
    if ratio >= 0.08:
        return "上升 📈", pct
    if ratio <= -0.08:
        return "走低 📉", pct
    return "平稳 ➡️", pct


class ValuationRequest(BaseModel):
    keyword: str
    max_history: int = 40
    filter_outliers: bool = True


@app.post("/api/card/valuation")
async def card_valuation(req: ValuationRequest):
    """基于卡淘真实成交计算公允估值 (FMV)，并扫描在拍捡漏机会。"""
    kw = (req.keyword or "").strip()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword 不能为空")

    try:
        history = int(req.max_history)
    except Exception:
        history = 40
    history = max(4, min(history, 60))

    sold = await _fetch_cardhobby(kw, mode="sold", page_size=history)
    raw = [it["price"] for it in sold["items"] if it.get("price")]
    if not raw:
        return {
            "status": "no_data",
            "keyword": kw,
            "sample_count": 0,
            "valid_count": 0,
            "fair_market_value": 0.0,
            "trend": "暂无成交 ⏳",
            "trend_pct": 0.0,
            "bargains": [],
            "bargain_count": 0,
            "bargain_note": "无成交样本",
            "recent_sales": [],
            "message": "卡淘没有该关键词的成交记录，建议换更精确的搜索词",
        }

    prices = _iqr_filter(raw) if req.filter_outliers else [p for p in raw if p > 0]
    if not prices:
        prices = [p for p in raw if p > 0]

    fmv = _weighted_fmv(prices)
    trend_text, trend_pct = _trend_of(prices)

    active = await _fetch_cardhobby(kw, mode="active", page_size=40)
    bargains = []
    note = ""

    if fmv < MIN_FMV_FOR_BARGAIN:
        note = (
            "公允估值仅 %.2f 元，低于 %.0f 元门槛：说明该关键词命中的是不同卡种的混杂成交，"
            "不构成同款比价基础。建议把搜索词补全到系列/版别/编数（例：夏思凝 琥珀金折 1/10）"
            % (fmv, MIN_FMV_FOR_BARGAIN)
        )
    else:
        skipped_placeholder = 0
        for it in active["items"]:
            cur = it.get("price") or 0.0
            if 0 < cur < MIN_PRICE_FOR_BARGAIN:
                skipped_placeholder += 1
                continue
            if cur >= MIN_PRICE_FOR_BARGAIN and cur <= fmv * 0.70:
                disc = round((1 - cur / fmv) * 100, 1)
                bargains.append({
                    "id": it["id"],
                    "title": it["title"],
                    "current_price": cur,
                    "fair_market_value": fmv,
                    "discount_pct": disc,
                    "bid_count": it["bid_count"],
                    "end_time": it["end_time"],
                    "url": it["url"],
                })
        bargains.sort(key=lambda x: x["discount_pct"], reverse=True)
        if not bargains:
            note = "当前在拍没有低于公允估值 70% 的标的"
        if skipped_placeholder:
            note = (note + "；" if note else "") + (
                "已忽略 %d 个 1-3 元的起拍占位价" % skipped_placeholder
            )

    return {
        "status": "success",
        "keyword": kw,
        "sample_count": len(raw),
        "valid_count": len(prices),
        "latest_price": prices[0] if prices else 0.0,
        "avg_price": round(statistics.mean(prices), 2),
        "median_price": round(statistics.median(prices), 2),
        "min_price": round(min(prices), 2),
        "max_price": round(max(prices), 2),
        "fair_market_value": fmv,
        "trend": trend_text,
        "trend_pct": trend_pct,
        "bargain_count": len(bargains),
        "bargain_note": note,
        "bargains": bargains[:10],
        # 原来硬编码 [:10]，导致估值只能看到前 10 笔成交。
        # 实测郑钦文金折刚成交的 ¥2800 排在第 11 条之后，永远进不了估值。
        # 改成按调用方要的 max_history 返回（上限 60，避免响应过大）。
        "recent_sales": sold["items"][:max(10, min(history, 60))],
    }


# ---------------------------- PSA 四维预评级 ----------------------------

class CenteringBox(BaseModel):
    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0


class PreGradeRequest(BaseModel):
    title: str = ""
    centering: CenteringBox | None = None
    image_b64: str | None = None


def _grade_centering(box):
    """按 PSA 正面容差：55/45→10，60/40→9，65/35→8，70/30→7。"""
    l = float(box.left or 0)
    r = float(box.right or 0)
    t = float(box.top or 0)
    b = float(box.bottom or 0)
    lr_sum = l + r
    tb_sum = t + b
    if lr_sum <= 0:
        l, r, lr_sum = 0.5, 0.5, 1.0
    if tb_sum <= 0:
        t, b, tb_sum = 0.5, 0.5, 1.0

    lr_hi = max(l / lr_sum, r / lr_sum) * 100
    tb_hi = max(t / tb_sum, b / tb_sum) * 100
    worst = max(lr_hi, tb_hi)
    lr_str = "%.1f / %.1f" % (lr_hi, 100 - lr_hi)
    tb_str = "%.1f / %.1f" % (tb_hi, 100 - tb_hi)

    if worst <= 55.0:
        return 10.0, "完美居中", "左右 %s，上下 %s，达 PSA 55/45 黄金标准" % (lr_str, tb_str), []
    if worst <= 60.0:
        return 9.0, "优良", "左右 %s，上下 %s，在 PSA 60/40 容差内" % (lr_str, tb_str), []
    if worst <= 65.0:
        return 8.0, "尚可", "左右 %s，上下 %s，超出 10 分标准" % (lr_str, tb_str), ["居中偏差可能限制最终等级"]
    if worst <= 70.0:
        return 7.0, "明显偏版", "左右 %s，上下 %s，肉眼可见偏离" % (lr_str, tb_str), ["居中已构成主要扣分项"]
    return 6.0, "严重偏版 (OC)", "左右 %s，上下 %s，超出常规容差" % (lr_str, tb_str), ["存在 Off-Center 判定风险，不建议送评"]


def _psa_overall(scores):
    """PSA 经验规则：总分以最低子维度为基准，最多上浮 1 分；低于 7 分不上浮。"""
    valid = [s for s in scores if s > 0]
    if not valid:
        return 0.0
    lowest = min(valid)
    if lowest < 7.0:
        return round(lowest, 1)
    return round(min(10.0, lowest + 1.0), 1)


async def _ask_vision_dimensions(title, image_b64, centering_note):
    """只看四角 / 边缘 / 卡面；居中由几何算法负责，避免重复判断。"""
    if not image_b64:
        return None

    prompt = (
        "你是资深球星卡评级师，熟悉 PSA 标准。请评估这张卡的三个维度："
        "corners(四角)、edges(边缘)、surface(卡面)。"
        "不要评估居中（已由几何算法完成：" + centering_note + "）。" + NL
        + "每维度给 1-10 分并附一句依据。看不清就填 0 并说明。" + NL
        + "只输出 JSON，不要代码块：" + NL
        + '{"corners":{"score":9.0,"notes":"..."},'
        + '"edges":{"score":9.0,"notes":"..."},'
        + '"surface":{"score":9.0,"notes":"..."},'
        + '"risks":["..."]}'
    )

    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
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
            res = await client.post(
                DSH_API_BASE + "/chat/completions", headers=headers, json=payload
            )
        if res.status_code != 200:
            return None
        raw = clean_fence(res.json()["choices"][0]["message"]["content"])
        return json.loads(raw)
    except Exception:
        return None


@app.post("/api/ai/pre-grade")
async def pre_grade(req: PreGradeRequest):
    """PSA 四维预评级。维度不全时只做保守估计，绝不给满分结论。"""
    title = (req.title or "").strip()

    # 维度一：居中（纯算法，可复现）
    if req.centering and (req.centering.left or req.centering.right):
        c_score, c_status, c_note, c_risks = _grade_centering(req.centering)
        centering_source = "measured"
    else:
        c_score, c_status = 0.0, "未测量"
        c_note = "未提供左右/上下边框像素，请先用「居中预评」量一次"
        c_risks = ["居中未测量，最终等级无法定档"]
        centering_source = "missing"

    # 维度二三四：四角 / 边缘 / 卡面（必须有图）
    vision = await _ask_vision_dimensions(title, req.image_b64, c_note) if req.image_b64 else None

    def pick(key):
        if not vision or not isinstance(vision.get(key), dict):
            return 0.0, "未评估", "未提供图片或视觉模型未返回", []
        d = vision[key]
        try:
            sc = float(d.get("score") or 0)
        except Exception:
            sc = 0.0
        status = "无法判定" if sc <= 0 else "已评估"
        return sc, status, str(d.get("notes") or ""), []

    corners = pick("corners")
    edges = pick("edges")
    surface = pick("surface")

    dims = {
        "centering": c_score,
        "corners": corners[0],
        "edges": edges[0],
        "surface": surface[0],
    }
    valid = [s for s in dims.values() if s > 0]
    evaluated = len(valid)

    # 关键修正：维度不全时不得给出满分/高潜质结论
    if evaluated == 0:
        overall = 0.0
        verdict = "无法定档"
        advice = "请先做居中测量并上传清晰实拍图，四个维度齐全后才能给等级。"
    elif evaluated < 4:
        overall = _psa_overall(valid)
        verdict = "部分维度缺失（%d/4 已评估）" % evaluated
        advice = (
            "当前仅有 %d 个维度有效，结论仅供参考，不能作为送评依据。"
            "补齐缺失维度后再定档。" % evaluated
        )
    else:
        overall = _psa_overall(valid)
        if overall >= 9.5:
            verdict = "PSA 10 潜质 (Gem Mint)"
            advice = "四维均达高分，建议送评 PSA；送前用强光复核卡面微痕。"
        elif overall >= 8.5:
            verdict = "PSA 9 倾向 (Mint)"
            advice = "整体优秀，送评性价比可以；想冲 10 分先补强最低那一维。"
        elif overall >= 7.5:
            verdict = "PSA 8 倾向 (NM-MT)"
            advice = "存在可见瑕疵，送评成本可能高于溢价，视卡价决定。"
        else:
            verdict = "不建议送评"
            advice = "多维度扣分明显，保留裸卡或按流通品出售更划算。"

    risks = list(c_risks)
    if vision and isinstance(vision.get("risks"), list):
        risks.extend([str(x) for x in vision["risks"][:5]])

    return {
        "status": "success",
        "card_title": title or "未命名卡片",
        "overall_grade": overall,
        "verdict": verdict,
        "advice": advice,
        "dimensions_evaluated": evaluated,
        "dimensions": {
            "centering": {"score": c_score, "status": c_status, "note": c_note, "source": centering_source},
            "corners": {"score": corners[0], "status": corners[1], "note": corners[2]},
            "edges": {"score": edges[0], "status": edges[1], "note": edges[2]},
            "surface": {"score": surface[0], "status": surface[1], "note": surface[2]},
        },
        "risks": risks,
        "vision_available": vision is not None,
    }


# ---------------------------- 一键上架文案 ----------------------------

class ListingRequest(BaseModel):
    title: str = ""
    entity: dict | None = None
    fmv: float | None = None
    condition: str = "全品 / 无明显肉眼瑕疵"
    # 默认走本地模板：毫秒级、稳定。模型版慢且可能被推理截断，需显式开启
    use_model: bool = False


def _listing_template(title, fmv, condition):
    """本地模板生成：不依赖模型，稳定且够用。"""
    t = (title or "").strip()
    base = float(fmv or 0) or 100.0

    # 卡淘标题：原文压缩到 58 字符内，保留关键信息
    ch = t if len(t) <= 58 else t[:58]

    # 闲鱼标题：加卖点前缀
    xy_core = t if len(t) <= 34 else t[:34]
    xy_title = "【原砖好品】" + xy_core

    desc = (
        "【卡片详情】" + t + NL
        + "【卡品声明】" + (condition or "全品") + "。实物拍摄以图为准；"
        + "收藏卡可能存在出厂痕或微瑕，完美主义者请慎拍。" + NL
        + "【包装防护】卡膜 + 磁力厚砖 + 双层气泡袋 + 加硬纸箱，编数与卡品发货前留档。" + NL
        + "【快递售后】默认顺丰到付、可保价；签收请录一镜到底开箱视频，作为唯一售后凭证；售出非假不退。"
    )

    start = round(base * 0.6, 2)
    buyout = round(base * 1.2, 2)
    return {
        "status": "success",
        "cardhobby_title": ch,
        "xianyu_title": xy_title,
        "xianyu_description": desc,
        "start_price": start,
        "buyout_price": buyout,
        "tips": [
            "起拍价用公允价的 60%，留出竞价空间",
            "晚间 20:00-22:00 结标关注度最高",
        ],
        "source": "template",
        "fmv_used": base,
    }


async def _listing_model(title, entity, fmv, condition):
    """模型版：max_tokens 提到 6000，避免推理吃掉预算导致 JSON 截断。"""
    prompt = (
        "你是球星卡资深卖家。为下面这张卡生成上架文案。" + NL
        + "标题：" + (title or "") + NL
        + "字段：" + json.dumps(entity or {}, ensure_ascii=False) + NL
        + "公允价：" + (("%.2f" % fmv) if fmv else "未知") + " 元" + NL
        + "品相：" + (condition or "全品") + NL + NL
        + "输出 JSON，字段：cardhobby_title(≤60字符)、xianyu_title、"
        + "xianyu_description(分四段：卡片详情/卡品声明/包装防护/快递售后)、"
        + "start_price(数字)、buyout_price(数字)、tips(数组)。"
        + "直接输出 JSON，不要任何解释。"
    )
    headers = {
        "Authorization": "Bearer " + DSH_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": DSH_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 6000,
    }
    async with httpx.AsyncClient(timeout=150.0) as client:
        res = await client.post(DSH_API_BASE + "/chat/completions", headers=headers, json=payload)
    if res.status_code != 200:
        raise RuntimeError("DSH 返回 %d" % res.status_code)
    ch = res.json()["choices"][0]
    if ch.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断")
    data = json.loads(clean_fence(ch["message"]["content"]))
    return {
        "status": "success",
        "cardhobby_title": str(data.get("cardhobby_title") or "")[:60],
        "xianyu_title": str(data.get("xianyu_title") or ""),
        "xianyu_description": str(data.get("xianyu_description") or ""),
        "start_price": float(data.get("start_price") or 0),
        "buyout_price": float(data.get("buyout_price") or 0),
        "tips": data.get("tips") or [],
        "source": "model",
        "fmv_used": fmv,
    }


@app.post("/api/ai/generate-listing")
async def generate_listing(req: ListingRequest):
    """生成卡淘标题 + 闲鱼防撕文案 + 建议定价。默认模板，可选模型增强。"""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")

    if req.use_model:
        try:
            return await _listing_model(title, req.entity, req.fmv, req.condition)
        except Exception as exc:
            out = _listing_template(title, req.fmv, req.condition)
            out["model_error"] = str(exc)
            return out

    return _listing_template(title, req.fmv, req.condition)


# ---------------------------- 热更新：自重启 ----------------------------

import os as _os
import threading as _threading
import time as _time

# 修改 app.py 后调用本端点即可让容器重启并加载新代码，
# 依赖 docker-compose 里的 restart: unless-stopped 自动拉起，无需人工干预。
RESTART_TOKEN = _os.getenv("REA_RESTART_TOKEN", "vault-reload-2026")


@app.post("/api/admin/restart")
def admin_restart(token: str = ""):
    """自杀式重启：进程退出后由 Docker 自动拉起，用于热更新部署。"""
    if token != RESTART_TOKEN:
        raise HTTPException(status_code=403, detail="token 不正确")

    def _die():
        _time.sleep(0.6)
        _os._exit(0)

    _threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting", "hint": "约 3-8 秒后新代码生效，可轮询 /health 的 version"}


# ---------------------------- 📚 Checklist 资料库与智能秒填 ----------------------------

import sqlite3
DB_CARDS_PATH = "/app/data/cards.db"

def get_cards_db():
    conn = sqlite3.connect(DB_CARDS_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/checklists/search")
def cl_search(q: str = "", series: str = "", limit: int = 15):
    """检索 Checklist 资料库，支持球员中英文、卡号、系列、球队模糊匹配"""
    conn = get_cards_db()
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
def cl_series():
    """返回当前资料库中收录的所有系列与收录卡片数"""
    conn = get_cards_db()
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


class ChecklistImportRequest(BaseModel):
    text: str = ""
    sport: str = "篮球"
    year: str = ""
    brand: str = "Panini"
    series: str = ""


@app.post("/api/checklists/import-text")
async def cl_import_text(req: ChecklistImportRequest):
    """通过 DeepSeek AI 解析微信公众号/官方推文/社群卡表文本，批量结构化入库"""
    raw_text = (req.text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="推文或卡表文本不能为空")
        
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
    
    headers = {
        "Authorization": "Bearer " + DSH_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": DSH_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 3000,
    }
    
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            res = await client.post(DSH_API_BASE + "/chat/completions", headers=headers, json=payload)
        if res.status_code != 200:
            raise RuntimeError(f"DSH 返回错误码 {res.status_code}")
        
        msg = res.json()["choices"][0]["message"]
        content = clean_fence(msg.get("content") or msg.get("reasoning_content") or "")
        items = json.loads(content)
        if not isinstance(items, list):
            if isinstance(items, dict) and "cards" in items:
                items = items["cards"]
            else:
                items = [items]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 解析卡表失败: {exc}")
        
    conn = get_cards_db()
    inserted = 0
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            c_num = str(it.get("card_number") or "").strip()
            p_en = str(it.get("player_en") or "").strip()
            p_cn = str(it.get("player_cn") or "").strip()
            s_name = str(it.get("series") or req.series or "未知系列").strip()
            if not p_en and not p_cn:
                continue
                
            parallels_json = json.dumps(it.get("parallels") or [], ensure_ascii=False)
            
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
                    it.get("sport") or req.sport,
                    it.get("year") or req.year,
                    it.get("brand") or req.brand,
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
