import os
import json
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Card Vault Scrapling Assistant", version="1.2.0")

DSH_API_BASE = os.getenv("DSH_API_BASE", "http://localhost:18899/v1")
DSH_API_KEY = os.getenv("DSH_API_KEY", "")
VAULT_API_BASE = os.getenv("VAULT_API_BASE", "http://card-vault:8000")
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
    title: str


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
        "version": "1.2.0",
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
        + NL + NL + "标题：" + req.title
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
    return {"status": "success", "raw_title": req.title, "parsed": parsed}


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