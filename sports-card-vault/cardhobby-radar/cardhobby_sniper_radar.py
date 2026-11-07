#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卡淘 (Card Hobby) 跨市场捡漏扫描雷达 v2.0 (Pro 实战版) - 群晖 NAS & WorkBuddy 专享
================================================================================
针对球星卡实战交易深度重构的 8 大核心能力：
1. 【同名消歧与跨界防误伤】：内置必含上下文词 (must_contain_any) 与专属排除词 (exclude_words)，
   彻底解决搜“利斯”出数百张足球利斯·詹姆斯、搜“科达”出高尔夫奈莉·科达等实战致命误报。
2. 【8 档精细限量编号 + 签字类型拆解】：
   严格区分 /1-/10(极低编)、/15-/25(橙折档)、/35-/50(金折档)、/75-/99(绿折档)、
   /125-/199(蓝折档)、/250-/299(紫折档)、无编签、Case Hit 特卡；
   自动识别卡签 (On-Card, 如 #AT- 系列) 与贴纸签 (#AO- 系列)，卡签估值自动上浮 20%。
   自动识别首编 (01/xx)、尾编 (xx/xx)，自动上浮安全埋价 15%。
3. 【系列等级 (Set Tier) 乘数与纯球衣卡拦截】：
   区分顶级/主流折射 (Sapphire/Finest/Chrome/Prizm/Select/Optic) 与低端纸卡 (Hoops/Donruss纸/Score)；
   自动拦截“带编但无签字的普通球衣/物料卡 (Relic/Patch)”，防止把廉价物料卡当折射漏。
4. 【评级卡 (PSA/BGS/SGC) 智能溢价折算】：
   自动识别 PSA 10 / BGS 9.5 (计入省下的送评费与10分溢价)、PSA 9 (x1.15)、PSA 8及以下 (折价预警)。
5. 【SQLite 跨时段状态追踪 (17:00 摸底 vs 19:20 终极对比)】：
   本地 SQLite 记录 17:00 拍品价格与出价次数，19:20 推送时自动标注：
   [🧊冷门死水·0新增出价] / [🔥暗盘试探] / [⚠️多人盯盘] / [🆕晚间新入窗口]。
6. 【同代卖商家合并运费算法】：
   自动识别【拔脚】【战斧】【腕龙】【卡淘】等大场商家，同店命中多张时自动聚合，
   第 2 张起免除首重运费 (¥12)，动态上调第 2 张卡的建议最高埋价。
7. 【双引擎抓取架构 (Playwright 真实无头浏览器拦截 XHR + HTTP 直连兜底)】：
   支持 Playwright 驱动 Chromium 自动打开卡淘搜索页并直接拦截底层真实 JSON 响应（无惧 JS 签名改版），
   同时支持外置 watchlist.json 热加载，修改红线价格无需重启容器。
8. 【PushPlus 微信图文卡片 + Bark iPhone 锁屏强提醒 + 企业微信三通道】。
================================================================================
"""

import os
import re
import sys
import math
import time
import json
import logging
import argparse
import copy
import datetime
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

# ==============================================================================
# 一、 全局配置与环境变量 (支持 .env / Docker 环境变量 / 外部 watchlist.json 热加载)
# ==============================================================================

BASE_DIR = os.getenv("RADAR_BASE_DIR", "/app" if (os.path.exists("/app") and os.access("/app", os.W_OK)) else "/tmp/cardhobby_radar")
DATA_DIR = os.getenv("RADAR_DATA_DIR", os.path.join(BASE_DIR, "data"))
CONFIG_DIR = os.getenv("RADAR_CONFIG_DIR", os.path.join(BASE_DIR, "config"))
LOG_DIR = os.getenv("RADAR_LOG_DIR", os.path.join(BASE_DIR, "logs"))

for d in (DATA_DIR, CONFIG_DIR, LOG_DIR):
    os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "cardhobby_radar.log"), encoding="utf-8")
    ]
)
logger = logging.getLogger("CardHobbyRadar")

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
BARK_URL = os.getenv("BARK_URL", "").strip()
WECHAT_WORK_WEBHOOK = os.getenv("WECHAT_WORK_WEBHOOK", "").strip()

DEFAULT_SHIPPING_RMB = float(os.getenv("DEFAULT_SHIPPING_RMB", "12.0"))
# 词库扩到 68 人后必须并发，否则单轮 4 分钟以上。抓取并发、评估串行。
MAX_WORKERS = max(1, int(os.getenv("RADAR_MAX_WORKERS", "4")))
# 并发下把单关键词间隔压到 0.8s；4 线程 ≈ 每秒 1.2 个请求，对 ASP.NET 站点不算激进
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "0.8"))
USE_REAL_POSTAGE = os.getenv("USE_REAL_POSTAGE", "1").strip() not in ("0", "false", "False")
PUSH_WHEN_EMPTY = os.getenv("PUSH_WHEN_EMPTY", "0").strip() in ("1", "true", "True")

# ------------------------------------------------------------------------------
# 策略参数（实盘调参入口，全部可用 .env 覆盖，改完 restart 即生效）
# ------------------------------------------------------------------------------
# 变现折算率：eBay 成交价 != 你能落袋的钱。
# eBay 抽 ~13% 成交费 + 国际运费 + 关税风险，国内卡淘转手佣金仅 3~5%。
# 0.70 的含义：eBay 卖 $100 的卡，你实际能落袋约 ¥500（而非按汇率直算 ¥720）。
REALIZATION_RATE = float(os.getenv("REALIZATION_RATE", "0.70"))

# 合邮第 2 张起免的是"首重"，续重仍需支付。用它替代原先"完全免运费"的乐观假设。
COMBINED_EXTRA_SHIPPING = float(os.getenv("COMBINED_EXTRA_SHIPPING", "6.0"))

# 卖家累计评价低于此值 → 标注新卖家风险（不代表骗子，但卡品纠纷率高）
MIN_SELLER_REVIEW = int(os.getenv("MIN_SELLER_REVIEW", "50"))

# 标题出现瑕疵类词（角损/划痕/折痕…）时对红线打折，避免把残卡当漏
CONDITION_PENALTY = float(os.getenv("CONDITION_PENALTY", "0.75"))

# ------------------------------------------------------------------------------
# 正向溢价封顶（2026-10 加入：保护真金白银）
#
# 旧版把「卡签 ×1.15」「高端系列 ×1.20」「首尾编 ×1.15」连乘，
# 三个都命中就是 ×1.52，再叠一张 PSA 10（≈×1.5）能到 ×2.28。
# 问题是这三个光环**高度共线**：能写「卡签」的卡通常本来就是高端系列，
# 三个乘数其实在描述同一件事，连乘等于把同一份溢价算三遍。
#
# 现在改成：正向部分先加算，再整体封顶。负向（瑕疵、兑换板）保持连乘——
# 折价之间是真的独立惩罚（又残又是兑换板，就该叠着扣）。
# ------------------------------------------------------------------------------
POSITIVE_CAP_BARE = float(os.getenv("POSITIVE_CAP_BARE", "1.30"))     # 非评级裸卡
POSITIVE_CAP_GRADED = float(os.getenv("POSITIVE_CAP_GRADED", "1.65"))  # 含评级壳
ON_CARD_BONUS = float(os.getenv("ON_CARD_BONUS", "0.15"))
PREMIUM_SET_BONUS = float(os.getenv("PREMIUM_SET_BONUS", "0.20"))
SPECIAL_SERIAL_BONUS = float(os.getenv("SPECIAL_SERIAL_BONUS", "0.15"))

# 这些系列「出厂就是卡签」，再给卡签溢价等于为同一件事付两次钱。
# 注意：只影响卡签加成，不影响高端系列自身的 +20%。
NATURAL_AUTO_SETS = (
    "national treasures", "national treasure", "国宝",
    "flawless", "immaculate", "eminence",
)

# ------------------------------------------------------------------------------
# 收益地板（词库扩到 68 人后必须设，实测教训）
#
# 卡淘几乎所有拍品都 ¥1 起拍。旧版只判断「现价 < 红线」，于是一张红线 ¥4 的
# /399 平行卡在 ¥1 时也算「真漏」——技术上没错，扣掉贴单/上架/沟通成本却是纯噪音。
# 68 人 × 5.5h 窗口实测：不设地板一轮 985 条命中，网页直接变刷屏，
# 手机推送还被 PushPlus 以 413 拒收。
#
# 口径用「按红线买入还能赚多少」而不是「现价买入赚多少」：
# 前者衡量这个档位值不值得追（稳定），后者随当前价乱跳（¥1 起拍时会虚高）。
# ------------------------------------------------------------------------------
MIN_PROFIT_RMB = float(os.getenv("MIN_PROFIT_RMB", "40"))
MIN_ROI_MULTIPLE = float(os.getenv("MIN_ROI_MULTIPLE", "1.6"))

# ------------------------------------------------------------------------------
# 「起拍观察」与列表封顶（同一批实测教训的后半段）
#
# 卡淘拍卖全是 ¥1 起拍，出价 0 次时 LowestPrice 就等于起拍价。
# 这类拍品不能算「低于红线的漏」——成交价还没被市场验证，你只能盲拍。
# 但它们也不是废数据：红线 ¥950 的约基奇签字挂在 ¥1 起拍，确实值得盯。
# 所以单独归为 blind（起拍观察），门槛更高（期望利 ≥ BLIND_MIN_PROFIT_RMB），
# 且不进「待抢」计数，避免几百条 ¥1 把真正能抢的漏挤到看不见。
# ------------------------------------------------------------------------------
BLIND_MIN_PROFIT_RMB = float(os.getenv("BLIND_MIN_PROFIT_RMB", "150"))

# 「起拍观察」还必须真的便宜。profit_at_red 只跟红线/估值有关，与现价无关，
# 所以光靠它会把「起拍 ¥40000、红线 ¥5000」这种天价货也塞进观察区
# （线上实测：2021 Panini Eminence 康宁汉姆 RPA 起拍 ¥40000，标记「净利 1756 / 0.10x」）。
# 起拍价必须显著低于红线，才算「0 出价 = 还没人发现」，否则只是「没人要」。
BLIND_MAX_START_RATIO = float(os.getenv("BLIND_MAX_START_RATIO", "0.5"))

# 68 人词库下即使过滤完，"低于红线"的条数依然会超出人能追的量。
# 每个球员最多留几张 + 每个桶的总量上限，保证首页是一份能执行的清单。
MAX_HITS_PER_PLAYER = int(os.getenv("MAX_HITS_PER_PLAYER", "2"))
MAX_STEAL_TOTAL = int(os.getenv("MAX_STEAL_TOTAL", "24"))
MAX_NEAR_TOTAL = int(os.getenv("MAX_NEAR_TOTAL", "10"))
MAX_BLIND_TOTAL = int(os.getenv("MAX_BLIND_TOTAL", "12"))
# 单条推送最多列几张，其余引导去网页看（PushPlus 有内容体积上限，实测 413）
PUSH_MAX_HITS = int(os.getenv("PUSH_MAX_HITS", "6"))
# PushPlus 正文硬上限实测 2 万字（超了返回 code 999「发送内容过大」整条丢失）。
# 留 20% 余量按字符预算动态裁剪，而不是拍一个"最多几张"的固定值——
# 一张卡的 HTML 长度会随标题长度浮动，固定张数根本兜不住。
PUSH_MAX_CHARS = int(os.getenv("PUSH_MAX_CHARS", "16000"))

# PSA10/BGS9.5 高评壳乘数。原 1.65 内含"省下 400 元送评费"，
# 但 2026 年 PSA 团送单张约 ¥200~250，故默认下调到 1.45，可按自己送评渠道调整。
GRADE10_MULTIPLIER = float(os.getenv("GRADE10_MULTIPLIER", "1.45"))

# ------------------------------------------------------------------------------
# 卡淘真实接口（2026-10-19 由真实浏览器拦截 XHR 实测确认，非猜测）
#
#   GET https://www.cardhobby.com.cn/NewCommodity/SearchCommodity
#       ?userId=&pageIndex=1&pageSize=60
#       &searchKey=<关键词>
#       &searchJson=[{"Key":"Status","Value":1}]
#       &sort=EffectiveTimeStamp&sortType=asc
#
# 要点（踩过的坑，勿动）：
#   1) searchJson 必须带 [{"Key":"Status","Value":1}]，缺了服务端会返回兜底 HTML 而非 JSON。
#   2) sort=EffectiveTimeStamp + sortType=asc 即"最快结束"，服务端已按截标时间升序，
#      因此第 1 页 60 条就覆盖了最近截标的一批，遇到超窗可直接短路，无需翻页。
#   3) 返回体是 {"data": {...}, "msg":..., "result":..., "lag":...} —— 外层 data 是小写，
#      列表在 data.PagedMarketItemList（不是 List）。
#   4) 实测字段：ID / Title / Price / PriceCount(出价次数) / EffectiveDate(截标,本地时间)
#      / SellRealName(卖家) / TitImg(图) / NewPostageMoney(真实运费) / ByWay。
# ------------------------------------------------------------------------------
CARDHOBBY_SEARCH_API = os.getenv(
    "CARDHOBBY_SEARCH_API",
    "https://www.cardhobby.com.cn/NewCommodity/SearchCommodity"
)
CARDHOBBY_PAGE_SIZE = int(os.getenv("CARDHOBBY_PAGE_SIZE", "60"))
CARDHOBBY_ITEM_URL_TPL = os.getenv(
    "CARDHOBBY_ITEM_URL_TPL",
    "https://www.cardhobby.com.cn/market/item/{item_id}"
)
CARDHOBBY_COOKIE = os.getenv("CARDHOBBY_COOKIE", "").strip()
CARDHOBBY_TOKEN = os.getenv("CARDHOBBY_TOKEN", "").strip()


# ------------------------------------------------------------------------------
# 时区加固：卡淘返回的 EffectiveDate 是北京时间字符串。
# python:3.11-slim 等精简镜像默认 UTC 且可能缺 tzdata，若用 datetime.now()
# 会整体偏 8 小时，导致所有拍品被误判为"已截标"而全部过滤。
# 中国无夏令时，直接固定 +08:00，不依赖系统 tzdata，绝对可靠。
# ------------------------------------------------------------------------------
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now_cn() -> datetime.datetime:
    """返回北京时间（naive，便于与接口返回的字符串直接比较）。"""
    return datetime.datetime.now(CN_TZ).replace(tzinfo=None)


def _today_cn() -> str:
    return _now_cn().date().isoformat()


EXTERNAL_WATCHLIST_PATH = os.path.join(CONFIG_DIR, "watchlist.json")


# ==============================================================================
# 二、 内置生产级黄金狙击词库 (含同名消歧校验词 + 8 档精细红线)
# ==============================================================================
# 8 大档位说明 (max_buy_rmb 均指【含单张首重运费 ¥12 在内的最高可买红线总成本】)：
# - s_1_10:      1/1 至 /10 极低编 (红折/黑折/大真金)
# - s_15_25:     /15 至 /25 超低编 (橙折/橙几何等硬通货)
# - s_35_50:     /35 至 /50 低编 (金折/金几何/金波浪)
# - s_75_99:     /75 至 /99 中低编 (绿折/绿几何)
# - s_125_199:   /125 至 /199 中编 (蓝折/蓝几何/碎冰)
# - s_250_399:   /250 至 /399 高编 (紫折/粉折)
# - base_auto:   无编签字 (若识别出 #AT- 卡签自动上浮 20%)
# - case_hit:    箱货特卡 (Aces / Youthquake / Instinct / Daily Dribble / Helix / Alter Ego)

DEFAULT_GOLDEN_WATCHLIST: List[Dict[str, Any]] = [
    # ---------------- 1. 网球 (2026 Topps Chrome Tennis 核心跨市场套利池) ----------------
    {
        "keyword": "科达",
        "en_name": "Sebastian Korda",
        "sport": "网球",
        "must_contain_any": ["科达", "korda", "sebastian", "塞巴斯蒂安", "at-ska", "ao-ska"],
        "exclude_words": ["高尔夫", "golf", "nelly", "奈莉", "内莉", "洁西卡", "jessica",
                            "kodat", "科达特", "toby", "托比", "arokodare", "阿罗科达雷", "科达雷"],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-ska"],
        "note": "美国名门硬地名将，/25橙签eBay实售$49.99(¥356)，卡淘常百元内漏拍",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 280, "ebay_ref_rmb": 850, "desc": "/10及以下极低编卡签"},
            "s_15_25":   {"max_buy_rmb": 125, "ebay_ref_rmb": 356, "desc": "/25橙折卡签 (#AT-SKA)"},
            "s_35_50":   {"max_buy_rmb": 88,  "ebay_ref_rmb": 240, "desc": "/50金折卡签"},
            "s_75_99":   {"max_buy_rmb": 58,  "ebay_ref_rmb": 165, "desc": "/99绿折卡签"},
            "s_125_199": {"max_buy_rmb": 45,  "ebay_ref_rmb": 125, "desc": "/150蓝折卡签"},
            "s_250_399": {"max_buy_rmb": 35,  "ebay_ref_rmb": 95,  "desc": "/250紫折卡签"},
            "base_auto": {"max_buy_rmb": 32,  "ebay_ref_rmb": 90,  "desc": "基础无编签"},
            "case_hit":  {"max_buy_rmb": 120, "ebay_ref_rmb": 330, "desc": "Aces / 特卡签"}
        }
    },
    {
        "keyword": "门希克",
        "en_name": "Jakub Mensik",
        "sport": "网球",
        "must_contain_any": ["门希克", "mensik", "jakub", "at-jmk", "ao-jmk"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-jmk"],
        "note": "05后捷克ATP超级新星，海外视为未来大满贯热门 (#AT-JMK)",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 420, "ebay_ref_rmb": 1200, "desc": "/10及以下极低编签"},
            "s_15_25":   {"max_buy_rmb": 220, "ebay_ref_rmb": 580,  "desc": "/25橙折卡签"},
            "s_35_50":   {"max_buy_rmb": 165, "ebay_ref_rmb": 430,  "desc": "/50金折卡签"},
            "s_75_99":   {"max_buy_rmb": 125, "ebay_ref_rmb": 320,  "desc": "/99绿折卡签"},
            "s_125_199": {"max_buy_rmb": 95,  "ebay_ref_rmb": 250,  "desc": "/150蓝折卡签"},
            "s_250_399": {"max_buy_rmb": 75,  "ebay_ref_rmb": 195,  "desc": "/250紫折卡签"},
            "base_auto": {"max_buy_rmb": 65,  "ebay_ref_rmb": 180,  "desc": "基础无编卡签 (#AT-JMK)"},
            "case_hit":  {"max_buy_rmb": 160, "ebay_ref_rmb": 450,  "desc": "Youthquake / Aces 特卡签"}
        }
    },
    {
        "keyword": "米克尔森",
        "en_name": "Alex Michelsen",
        "sport": "网球",
        "must_contain_any": ["米克尔森", "michelsen", "at-amn", "ca-amn"],
        "exclude_words": ["高尔夫", "golf", "phil", "菲尔"],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-amn", "ca-amn"],
        "note": "美国本土新生代主力，Youthquake /99签eBay曾拍出$155(¥1100)",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 320, "ebay_ref_rmb": 900, "desc": "/10及以下极低编签"},
            "s_15_25":   {"max_buy_rmb": 160, "ebay_ref_rmb": 420, "desc": "/25橙折签"},
            "s_35_50":   {"max_buy_rmb": 120, "ebay_ref_rmb": 320, "desc": "/50金折签"},
            "s_75_99":   {"max_buy_rmb": 85,  "ebay_ref_rmb": 230, "desc": "/99绿折签"},
            "s_125_199": {"max_buy_rmb": 68,  "ebay_ref_rmb": 185, "desc": "/150蓝折签"},
            "s_250_399": {"max_buy_rmb": 52,  "ebay_ref_rmb": 145, "desc": "/250紫折签"},
            "base_auto": {"max_buy_rmb": 45,  "ebay_ref_rmb": 130, "desc": "基础无编签"},
            "case_hit":  {"max_buy_rmb": 180, "ebay_ref_rmb": 550, "desc": "Youthquake / Aces 特卡签"}
        }
    },
    {
        "keyword": "丰塞卡",
        "en_name": "Joao Fonseca",
        "sport": "网球",
        "must_contain_any": ["丰塞卡", "fonseca", "at-jfs", "ao-jfa"],
        "exclude_words": ["足球", "soccer", "paulo", "保罗"],
        "require_auto_for_non_low_serial": False,  # 丰塞卡连带编RC折射都极贵
        "on_card_codes": ["at-jfs"],
        "note": "巴西男网第一天才、NextGen总冠军，全球顶流追捧RC",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 1200, "ebay_ref_rmb": 3500, "desc": "/10及以下极低编RC签/折"},
            "s_15_25":   {"max_buy_rmb": 550,  "ebay_ref_rmb": 1500, "desc": "/25橙折RC签"},
            "s_35_50":   {"max_buy_rmb": 400,  "ebay_ref_rmb": 1050, "desc": "/50金折RC签"},
            "s_75_99":   {"max_buy_rmb": 300,  "ebay_ref_rmb": 780,  "desc": "/99绿折RC签"},
            "s_125_199": {"max_buy_rmb": 240,  "ebay_ref_rmb": 600,  "desc": "/150蓝折RC签"},
            "s_250_399": {"max_buy_rmb": 190,  "ebay_ref_rmb": 480,  "desc": "/250紫折RC签"},
            "base_auto": {"max_buy_rmb": 175,  "ebay_ref_rmb": 420,  "desc": "基础RC签 (#AO-JFA/#AT-JFS)"},
            "case_hit":  {"max_buy_rmb": 350,  "ebay_ref_rmb": 880,  "desc": "Youthquake / Aces RC特卡"}
        }
    },
    {
        "keyword": "克鲁格曼",
        "en_name": "Hannah Klugman",
        "sport": "网球",
        "must_contain_any": ["克鲁格曼", "klugman", "at-hkm"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-hkm"],
        "note": "英国09后天才少女，/150蓝签eBay实售$39.99(¥285)，国内认知度低",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 350, "ebay_ref_rmb": 950, "desc": "/10及以下极低编RC签"},
            "s_15_25":   {"max_buy_rmb": 170, "ebay_ref_rmb": 480, "desc": "/25橙折RC签"},
            "s_35_50":   {"max_buy_rmb": 130, "ebay_ref_rmb": 360, "desc": "/50金折RC签"},
            "s_75_99":   {"max_buy_rmb": 95,  "ebay_ref_rmb": 310, "desc": "/99绿折RC签"},
            "s_125_199": {"max_buy_rmb": 80,  "ebay_ref_rmb": 285, "desc": "/150蓝折RC卡签 (#AT-HKM)"},
            "s_250_399": {"max_buy_rmb": 55,  "ebay_ref_rmb": 190, "desc": "/250紫折RC卡签"},
            "base_auto": {"max_buy_rmb": 42,  "ebay_ref_rmb": 150, "desc": "无编RC卡签"},
            "case_hit":  {"max_buy_rmb": 120, "ebay_ref_rmb": 350, "desc": "Youthquake 特卡签"}
        }
    },
    {
        "keyword": "森梅兹",
        "en_name": "Zeynep Sonmez",
        "sport": "网球",
        "must_contain_any": ["森梅兹", "sonmez", "zeynep", "at-zsz"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-zsz"],
        "note": "土耳其女网一姐，无编RC卡签eBay实售$34.99(¥249)，欧洲买盘极强",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 300, "ebay_ref_rmb": 850, "desc": "/10及以下极低编RC签"},
            "s_15_25":   {"max_buy_rmb": 150, "ebay_ref_rmb": 420, "desc": "/25橙折RC签"},
            "s_35_50":   {"max_buy_rmb": 115, "ebay_ref_rmb": 340, "desc": "/50金折RC签"},
            "s_75_99":   {"max_buy_rmb": 90,  "ebay_ref_rmb": 295, "desc": "/99绿折RC签"},
            "s_125_199": {"max_buy_rmb": 75,  "ebay_ref_rmb": 270, "desc": "/150蓝折RC签"},
            "s_250_399": {"max_buy_rmb": 62,  "ebay_ref_rmb": 255, "desc": "/250紫折RC签"},
            "base_auto": {"max_buy_rmb": 55,  "ebay_ref_rmb": 249, "desc": "无编RC卡签 (#AT-ZSZ)"},
            "case_hit":  {"max_buy_rmb": 110, "ebay_ref_rmb": 320, "desc": "Aces / 特卡签"}
        }
    },
    {
        "keyword": "埃娃",
        "alt_keywords": ["利斯"],
        "en_name": "Eva Lys",
        "sport": "网球",
        "must_contain_any": ["埃娃", "爱娃", "eva lys", "lys", "at-els", "ao-els"],
        "exclude_words": ["足球", "切尔西", "利物浦", "英超", "篮球", "nba", "james", "reece", "alisson",
                            "阿利斯", "莫利斯", "evans", "evan "],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-els"],
        "note": "德国高人气女网新星 (#AT-ELS)，注意严防与足球‘利斯·詹姆斯/阿利斯’混淆",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 350, "ebay_ref_rmb": 950, "desc": "/10及以下极低编签"},
            "s_15_25":   {"max_buy_rmb": 175, "ebay_ref_rmb": 460, "desc": "/25橙折卡签"},
            "s_35_50":   {"max_buy_rmb": 135, "ebay_ref_rmb": 360, "desc": "/50金折卡签"},
            "s_75_99":   {"max_buy_rmb": 95,  "ebay_ref_rmb": 260, "desc": "/99绿折卡签 (#AT-ELS)"},
            "s_125_199": {"max_buy_rmb": 75,  "ebay_ref_rmb": 210, "desc": "/150蓝折卡签"},
            "s_250_399": {"max_buy_rmb": 58,  "ebay_ref_rmb": 165, "desc": "/250紫折卡签"},
            "base_auto": {"max_buy_rmb": 48,  "ebay_ref_rmb": 140, "desc": "无编卡签"},
            "case_hit":  {"max_buy_rmb": 130, "ebay_ref_rmb": 350, "desc": "Aces 特卡签"}
        }
    },
    {
        "keyword": "施耐德",
        "en_name": "Diana Shnaider",
        "sport": "网球",
        "must_contain_any": ["施耐德", "shnaider", "黛安娜", "ao-dsr", "at-dsr"],
        "exclude_words": ["足球", "斯内德", "施耐德林", "篮球"],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-dsr"],
        "note": "WTA巡回赛多冠王，海外实绩买盘稳固",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 280, "ebay_ref_rmb": 780, "desc": "/10及以下极低编签"},
            "s_15_25":   {"max_buy_rmb": 145, "ebay_ref_rmb": 390, "desc": "/25橙折签"},
            "s_35_50":   {"max_buy_rmb": 115, "ebay_ref_rmb": 310, "desc": "/50金折签"},
            "s_75_99":   {"max_buy_rmb": 78,  "ebay_ref_rmb": 210, "desc": "/99绿折签"},
            "s_125_199": {"max_buy_rmb": 62,  "ebay_ref_rmb": 175, "desc": "/150蓝折签"},
            "s_250_399": {"max_buy_rmb": 45,  "ebay_ref_rmb": 130, "desc": "/250紫折签"},
            "base_auto": {"max_buy_rmb": 35,  "ebay_ref_rmb": 102, "desc": "基础几何折射签 (#AO-DSR)"},
            "case_hit":  {"max_buy_rmb": 120, "ebay_ref_rmb": 320, "desc": "Aces 特卡签"}
        }
    },
    {
        "keyword": "斯特恩斯",
        "en_name": "Peyton Stearns",
        "sport": "网球",
        "must_contain_any": ["斯特恩斯", "stearns", "peyton", "at-pss"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": True,
        "on_card_codes": ["at-pss"],
        "note": "美国NCAA总冠军名将，/250紫签eBay实售$20.39(¥145)，卡淘常20多元漏拍",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 240, "ebay_ref_rmb": 650, "desc": "/10及以下极低编卡签"},
            "s_15_25":   {"max_buy_rmb": 125, "ebay_ref_rmb": 340, "desc": "/25橙折卡签"},
            "s_35_50":   {"max_buy_rmb": 95,  "ebay_ref_rmb": 260, "desc": "/50金折卡签"},
            "s_75_99":   {"max_buy_rmb": 62,  "ebay_ref_rmb": 185, "desc": "/99绿折卡签"},
            "s_125_199": {"max_buy_rmb": 48,  "ebay_ref_rmb": 160, "desc": "/150蓝折卡签 (#AT-PSS)"},
            "s_250_399": {"max_buy_rmb": 38,  "ebay_ref_rmb": 145, "desc": "/250紫折卡签 (#AT-PSS)"},
            "base_auto": {"max_buy_rmb": 28,  "ebay_ref_rmb": 95,  "desc": "无编卡签"},
            "case_hit":  {"max_buy_rmb": 100, "ebay_ref_rmb": 280, "desc": "Aces 特卡签"}
        }
    },
    # ---------------- 2. NBA (2025-26 Topps Chrome & 核心套利池) ----------------
    {
        "keyword": "字母哥",
        "en_name": "Giannis Antetokounmpo",
        "sport": "篮球",
        "must_contain_any": ["字母哥", "giannis", "antetokounmpo"],
        "exclude_words": ["萨纳西斯", "thanasis", "哥哥", "弟弟", "字母弟", "kostas", "alex"],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "超巨低编特卡国内常漏拍，Daily Dribble /25 eBay实售$52-$76(¥372-¥540)",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 650, "ebay_ref_rmb": 1800, "desc": "/10及以下极低编折射/特卡"},
            "s_15_25":   {"max_buy_rmb": 220, "ebay_ref_rmb": 510,  "desc": "/25橙折 (Instinct/Daily Dribble/主卡)"},
            "s_35_50":   {"max_buy_rmb": 160, "ebay_ref_rmb": 380,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 90,  "ebay_ref_rmb": 220,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 55,  "ebay_ref_rmb": 140,  "desc": "/150或/199蓝折"},
            "s_250_399": {"max_buy_rmb": 35,  "ebay_ref_rmb": 90,   "desc": "/250或/299高编折"},
            "base_auto": {"max_buy_rmb": 850, "ebay_ref_rmb": 2200, "desc": "字母哥官方签字卡"},
            "case_hit":  {"max_buy_rmb": 210, "ebay_ref_rmb": 490,  "desc": "Helix / Let's Go / Kaboom 等箱货特卡"}
        }
    },
    {
        "keyword": "约基奇",
        "en_name": "Nikola Jokic",
        "sport": "篮球",
        "must_contain_any": ["约基奇", "jokic", "约老师"],
        "exclude_words": ["约维奇", "jovic"],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "现役第一人，Instinct /50金折及低编在eBay极其硬通($70-$85)",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 750, "ebay_ref_rmb": 2100, "desc": "/10及以下极低编折射/特卡"},
            "s_15_25":   {"max_buy_rmb": 240, "ebay_ref_rmb": 580,  "desc": "/25橙折"},
            "s_35_50":   {"max_buy_rmb": 175, "ebay_ref_rmb": 450,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 95,  "ebay_ref_rmb": 240,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 60,  "ebay_ref_rmb": 150,  "desc": "/150或/199蓝折"},
            "s_250_399": {"max_buy_rmb": 38,  "ebay_ref_rmb": 95,   "desc": "/250或/299高编折"},
            "base_auto": {"max_buy_rmb": 950, "ebay_ref_rmb": 2500, "desc": "约基奇官方签字卡"},
            "case_hit":  {"max_buy_rmb": 230, "ebay_ref_rmb": 550,  "desc": "Helix / Stained Glass / 箱货特卡"}
        }
    },
    {
        "keyword": "阿门",
        "en_name": "Amen Thompson",
        "sport": "篮球",
        "must_contain_any": ["阿门", "amen thompson", "amen"],
        "exclude_words": ["奥萨尔", "ausar", "活塞", "克莱", "klay", "勇士", "独行侠"],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "火箭攻防核心，北美市场追捧度远超国内（严防与弟弟奥萨尔或克莱混淆）",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 480, "ebay_ref_rmb": 1350, "desc": "/10及以下极低编"},
            "s_15_25":   {"max_buy_rmb": 190, "ebay_ref_rmb": 520,  "desc": "/25橙折"},
            "s_35_50":   {"max_buy_rmb": 140, "ebay_ref_rmb": 380,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 75,  "ebay_ref_rmb": 210,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 52,  "ebay_ref_rmb": 150,  "desc": "/150或/199蓝折"},
            "s_250_399": {"max_buy_rmb": 35,  "ebay_ref_rmb": 100,  "desc": "/250或/275高编折"},
            "base_auto": {"max_buy_rmb": 220, "ebay_ref_rmb": 580,  "desc": "阿门签字卡"},
            "case_hit":  {"max_buy_rmb": 150, "ebay_ref_rmb": 390,  "desc": "Youthquake / 稀有特卡"}
        }
    },
    {
        "keyword": "杰伦约翰逊",
        "en_name": "Jalen Johnson",
        "sport": "篮球",
        "must_contain_any": ["杰伦约翰逊", "杰伦·约翰逊", "jalen johnson"],
        "exclude_words": ["杰伦格林", "杰伦布朗", "杰伦威廉姆斯", "卡梅隆约翰逊", "凯尔登约翰逊"],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "老鹰新鹰王，美国Fantasy与高阶数据派极热，国内因老鹰冷门常漏拍",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 420, "ebay_ref_rmb": 1200, "desc": "/10及以下极低编"},
            "s_15_25":   {"max_buy_rmb": 175, "ebay_ref_rmb": 480,  "desc": "/25橙折"},
            "s_35_50":   {"max_buy_rmb": 130, "ebay_ref_rmb": 360,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 68,  "ebay_ref_rmb": 190,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 48,  "ebay_ref_rmb": 135,  "desc": "/150或/199蓝折"},
            "s_250_399": {"max_buy_rmb": 32,  "ebay_ref_rmb": 90,   "desc": "/250或/275高编折"},
            "base_auto": {"max_buy_rmb": 180, "ebay_ref_rmb": 460,  "desc": "杰伦约翰逊签字卡"},
            "case_hit":  {"max_buy_rmb": 140, "ebay_ref_rmb": 360,  "desc": "稀有箱货特卡"}
        }
    },
    # ---------------- 3. 足球 (2026 世界杯东道主头牌 & 欧冠硬通货) ----------------
    {
        "keyword": "普利西奇",
        "en_name": "Christian Pulisic",
        "sport": "足球",
        "must_contain_any": ["普利西奇", "pulisic"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "2026美加墨世界杯美国队长，北美主场溢价极高，国内常低价结标",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 450, "ebay_ref_rmb": 1300, "desc": "/10及以下极低编"},
            "s_15_25":   {"max_buy_rmb": 170, "ebay_ref_rmb": 480,  "desc": "/25超低编折射"},
            "s_35_50":   {"max_buy_rmb": 125, "ebay_ref_rmb": 350,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 70,  "ebay_ref_rmb": 210,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 50,  "ebay_ref_rmb": 155,  "desc": "/150或/199带编折"},
            "s_250_399": {"max_buy_rmb": 35,  "ebay_ref_rmb": 105,  "desc": "/250或/299高编折"},
            "base_auto": {"max_buy_rmb": 220, "ebay_ref_rmb": 600,  "desc": "普利西奇官方签字卡"},
            "case_hit":  {"max_buy_rmb": 180, "ebay_ref_rmb": 500,  "desc": "National Landmarks / Color Blast"}
        }
    },
    {
        "keyword": "哈兰德",
        "en_name": "Erling Haaland",
        "sport": "足球",
        "must_contain_any": ["哈兰德", "haaland"],
        "exclude_words": [],
        "require_auto_for_non_low_serial": False,
        "on_card_codes": [],
        "note": "多特时期或欧冠带编折在eBay极稳(/175实售$38/¥270)，卡淘常百元内结标",
        "tiers": {
            "s_1_10":    {"max_buy_rmb": 850,  "ebay_ref_rmb": 2400, "desc": "/10及以下极低编"},
            "s_15_25":   {"max_buy_rmb": 310,  "ebay_ref_rmb": 820,  "desc": "/25橙折"},
            "s_35_50":   {"max_buy_rmb": 220,  "ebay_ref_rmb": 580,  "desc": "/50金折"},
            "s_75_99":   {"max_buy_rmb": 135,  "ebay_ref_rmb": 350,  "desc": "/75或/99绿折"},
            "s_125_199": {"max_buy_rmb": 105,  "ebay_ref_rmb": 275,  "desc": "/125至/199带编折射(含多特时期)"},
            "s_250_399": {"max_buy_rmb": 68,   "ebay_ref_rmb": 180,  "desc": "/250至/299高编折"},
            "base_auto": {"max_buy_rmb": 1200, "ebay_ref_rmb": 3200, "desc": "哈兰德官方签字卡"},
            "case_hit":  {"max_buy_rmb": 240,  "ebay_ref_rmb": 620,  "desc": "Alter Ego / Helix / Kaboom 特卡"}
        }
    }
]

# 全局硬性黑名单：遇到直接过滤
# 注意：英文词一律走 word_in_text() 的词边界匹配，绝不能用裸子串。
# 这里的 "hit" 原本是子串匹配，会把 "W-h-i-t-e Sparkle" 里的 hit 也吃掉，
# 白白漏掉一张真卡 —— 已改成 "hit parade"（品牌），并保留 CASE_HIT_KEYWORDS 正向识别。
GLOBAL_BLACKLIST = [
    "印刷签", "印签", "facsimile", "leaf", "wild card", "sage", "hit parade",
    "卡牌砖", "卡砖", "空盒", "散包",
    "代拆", "直播", "随机", "盲盒", "同背", "拼接", "自制", "diy",
    "选秀卡", "贴纸包", "match attax", "adrenalyn", "tcg",
]

# ==============================================================================
# 密封产品（整包 / 整盒 / 未拆）识别
#
# 这是最容易造成"天价误判"的一类：卖家标题里写着「16编低编」「哈兰德」，
# 但卖的是一整包未拆封的卡。按单卡红线去比价毫无意义 —— 包里有没有这张卡都不确定。
# 真实踩过的坑：2022-23 Panini Chronicles fat pack 被当成 /16 单卡，直接套 /25 档红线。
# ==============================================================================

# 先剥掉这些词，避免「一卡包邮」被「卡包」误伤
SHIPPING_NOISE = ["包邮", "包顺丰", "包退", "包换", "包赔", "包含", "包税", "包过关",
                  # 单卡"带原盒/带卡砖盒"是包装描述，不是整盒商品，先剥掉再判整包
                  "带原盒", "含原盒", "赠原盒", "带盒子", "原包装"]

# 强信号：命中即判定为密封产品
SEALED_PATTERNS = [
    # 英文（一律带词边界，防 "tin" 吃掉 "Destiny" 之类）
    r"fat\s*pack", r"hanger(?:\s*pack)?", r"blaster", r"booster",
    r"mega\s*(?:box|pack)", r"hobby\s*(?:box|pack)", r"retail\s*(?:box|pack)",
    r"cello\s*pack", r"rack\s*pack", r"sleeve\s*pack", r"gravity\s*pack",
    r"\b(?:box|pack)s?\b", r"\bsealed\b", r"unopened", r"factory\s*sealed",
    # 中文
    r"整盒", r"整箱", r"整包", r"整条", r"原封", r"未拆", r"未开封", r"没拆",
    r"拆卡", r"开盒", r"拆盒", r"盒装", r"套装", r"套卡", r"卡包", r"礼盒",
    r"福袋", r"盲包", r"一盒", r"一包", r"全新原盒", r"原盒",
    r"\d+\s*包\s*入", r"\d+\s*盒\s*装", r"\d+\s*张\s*/\s*盒",
    # 多卡合售：「一图」= 一张图里好几张卡；「打包」几乎只出现在多卡挂牌上
    # （线上实测漏网：「…黄平行等 Base /250/399编 一图打包」，红线按单卡算直接虚高）
    r"一图", r"打包", r"合集", r"捆绑", r"批量",
    # 拆卖 / 组队 / 赌卡玩法：买的是"不确定是哪张"，比价模型完全失效
    # （线上实测漏网：「elevencard Barcelona team set refractor 原箱拆卖保一编博大」）
    r"原箱", r"拆卖", r"拆箱", r"组队", r"拼包", r"拼盒",
    r"保一编", r"保限量", r"博大",
    # 拆卡/抽奖/多卡打包：买的是"可能抽到"，不是这张卡本身
    # （线上实测：「Prizm retail 拆文班亚马42编、88编…有机会抽粉脉冲」，被按 42 编估成 17.7 倍）
    r"有机会抽", r"抽奖", r"随机发", r"不保卡品", r"\blot\b",
    # 注意：不要把「可累计」写进拦截表。线上实测它绝大多数是「运费可累计」的缩写，
    # 方括号形式也照样出现在单卡挂牌上（【甲子拍卖】#51【可累计】…/249编 银折），
    # 一拦就把正常单卡误杀了。
    r"team\s*set",
    # 凑套 / 成套 / 多卡合集（针对漏网彻底堵死）
    r"全套", r"一套(?:\s*出)?", r"凑套", r"大全套", r"大套", r"小套", r"base套", r"特卡套",
    r"成套", r"套出", r"出套", r"合集", r"捆绑", r"批量", r"整图", r"一图打包",
    # 盲盒 / 福袋 / 拆卖博弈
    r"盲盒", r"盲袋", r"盲抽", r"盲选", r"盲包", r"福袋", r"福盒", r"福利盒", r"福利包",
    r"手撕包", r"手撕", r"自制盒", r"自制包", r"选队", r"选人", r"随队", r"随人", r"随机卡",
    # Topps Signature Class「签字盒」= 一盒多张签字卡的整盒货，不是单张
    r"签字盒",
    # 裸「盒」兜底：排除「盒子/盒损/盒压」这类品相或包装描述
    r"盒(?!子|损|压)",
]

# 弱信号：必须配合"多张/打包"语义才算（单个「打包」太常见，不能一刀切）
SEALED_WEAK_PATTERNS = [
    r"\d+\s*张\s*(?:打包|合售|套出|一起)",
    r"(?:打包|合售|套出|一并)\s*(?:出|卖|甩)",
    r"多卡\s*(?:合售|打包|套装)",
    r"\blot\s*of\b", r"\blot\b\s*\d+",
]

SEALED_RE = [re.compile(p, re.IGNORECASE) for p in SEALED_PATTERNS]
SEALED_WEAK_RE = [re.compile(p, re.IGNORECASE) for p in SEALED_WEAK_PATTERNS]

# ------------------------------------------------------------------------------
# 多人卡 / 合集：一张卡上有两个以上球星，按单人档位比价必然虚高
#
# 和"整包"不同 —— 卡是真的只有一张，但买它不等于买到某个人的卡。
# 线上实测：「博新秀文班亚马 晚安库里 詹姆斯 东契奇 欧文等经典双人全息特卡」，
# 被按文班亚马单人 /10 档位估成 ¥5250 变现、159 倍。
# ------------------------------------------------------------------------------
MULTI_PLAYER_PATTERNS = [
    r"双人", r"三人", r"双签", r"三签", r"多人", r"多球星",
    r"合辑", r"同框", r"等经典", r"等多位", r"等人", r"等球星",
]
MULTI_PLAYER_RE = [re.compile(p, re.IGNORECASE) for p in MULTI_PLAYER_PATTERNS]


def multi_player_hit(title: str) -> str:
    """返回命中的多人卡关键词，没有则空串。"""
    for pat in MULTI_PLAYER_RE:
        m = pat.search(title or "")
        if m:
            return m.group(0)
    return ""

# 近漏观察区：含运总成本在红线这一倍数以内，不丢弃，进「准漏」列表
NEAR_MISS_TOLERANCE = float(os.getenv("NEAR_MISS_TOLERANCE", "1.20"))

# 低端纸卡系列（非折射，若非低编签字直接大幅降权或拦截）
LOW_END_PAPER_SETS = ["hoops", "donruss", "chronicles", "score", "prestige", "contenders", "纸卡"]

# 顶级高端/蓝宝石系列（估值上浮 20%）
PREMIUM_SETS = ["sapphire", "蓝宝石", "finest", "flawless", "immaculate", "national treasures", "国宝", "eminence"]

# 高价值 Case Hit 特卡关键词
#
# 注意 "aces" 已剔除（2026-10-20）：Prizm 系列的 Aces 是**普通 insert**
# （约 1:8 包），不是 case hit。原配置让它套用 ¥490~820 的箱货特卡参照价，
# 线上把一张「2026 Panini Prizm World Cup Bellingham Aces 扑克A特卡」
# 算成参照 ¥820 / 现价 ¥1，属于凭空造漏。宁可漏掉，不要虚报。
CASE_HIT_KEYWORDS = [
    "youthquake", "instinct", "daily dribble", "helix",
    "alter ego", "let's go", "color blast", "kaboom", "stained glass",
    "national landmarks", "radiating rookies", "hidden gems", "manga", "downtown"
]

# 兑换板 / redemption —— 卖家寄回厂商换卡，不是实物。
# 线上踩到：「2025-26 Topps Pristine 约基奇 /5编 绰号签 铭文签字
# Personal Endorsements 交换板」被按实物 /5 签字估出参照 ¥16675。
# 兑换板真实成交通常只有实物的 40%~60%，且存在换不到/换到别款的风险。
REDEMPTION_RE = re.compile(
    r"(?:交换板|交換板|兑换板|兌換板|兑换卡|兌換卡|兑换码|交换卡|redemption|redeem)",
    re.IGNORECASE,
)
REDEMPTION_FACTOR = float(os.getenv("REDEMPTION_FACTOR", "0.55"))

# ------------------------------------------------------------------------------
# Base 普卡 vs 平行/折射
#
# 折射轨（parallel_tiers）的每一档都隐含「这是一张低编平行」：
#   p_35_50 给的是「/50 金折」的价，比同为 /50 的 Base 黑折便宜十倍不止。
# 但原代码是「无签字 + serial<=399」就直接套档，**从不检查标题里到底有没有
# 折射/颜色特征**。结果一张卖家自己写了备注的 Prizm 世界杯 Base 普卡，
# 被算出参照 ¥1015 / 现价 ¥16 = 17 倍假漏。
#
# 所以：无签字轨必须先证明"它是张平行"，否则不予估价。
# ------------------------------------------------------------------------------
PARALLEL_FEATURE_CN = re.compile(
    # 「折」必须排除「折价 / 折扣 / 折痕 / 折损 / 折叠」——
    # 残卡描述里全是这些词，否则一张角损 Base 卡会被当成"有折射特征"放行。
    r"(?:折射|折(?!价|扣|痕|损|叠|算)|几何|碎冰|波浪|泡泡|闪电|镭射|全息|"
    r"彩虹|大理石|平行|闪卡|亮面|镜面)"
)

# 英文特征必须走词边界：`green` 会吃到 Jalen Green 这种球员名，
# `red` 会吃到 Contenders，裸子串匹配等于没约束。
PARALLEL_FEATURE_EN = re.compile(
    r"(?<![a-z0-9])(?:refractor|wave|shimmer|sparkle|holo|mojo|cracked|disco|"
    r"pulsar|scope|choice|hyper|pyramid|atomic|laser|speckle|marble|"
    r"tie-dye|nebula|genesis|velocity|kaleidoscopic|groovy|sapphire|"
    r"mini-diamond|parallel|silver|gold|orange|purple|pink|aqua|teal|"
    r"fast-break|holofoil|prizm-color)(?![a-z0-9])",
    re.IGNORECASE,
)

# 明确标注是 Base 普卡的（词边界，避免 base 吃到 baseball / database）
BASE_CARD_RE = re.compile(r"(?<![a-z0-9])(?:base|普卡|普折|基础卡)(?![a-z0-9])",
                          re.IGNORECASE)


def has_parallel_feature(title: str) -> bool:
    """标题里能不能找到折射/平行的特征词（折、几何、工艺名、英文颜色）。

    这是折射轨的准入条件：没有它，说明这只是一张 Base 平卡，
    拿「/50 金折」的参照价去估它必然虚报。
    """
    t = (title or "").lower()
    if PARALLEL_FEATURE_CN.search(t):
        return True
    return bool(PARALLEL_FEATURE_EN.search(t))


def is_base_plain_card(title: str) -> bool:
    """标题明确写了 Base/普卡，且找不到折射特征 → 普通平卡，不估价。"""
    t = title or ""
    if not BASE_CARD_RE.search(t):
        return False
    return not has_parallel_feature(t)

# 常见大代卖商家前缀识别（用于同店合并运费计算）
KNOWN_CONSIGNORS = ["拔脚", "战斧", "腕龙", "卡淘", "寻觅", "老A", "球星卡社", "麦卡", "大鲨鱼", "爱卡"]


def word_in_text(word: str, text_lower: str) -> bool:
    """关键词匹配：纯英文/数字强制单词边界，中文走子串。

    为什么必须这样：
    - 英文短词 `amen` 用裸子串会命中 `tourn-amen-t`，把别人的卡标成「阿门」；
    - 黑名单里的 `hit` 会命中 `w-hit-e`，把 White Sparkle 误杀。
    中文没有词边界概念，只能子串。
    """
    w = (word or "").lower().strip()
    if not w:
        return False
    if re.match(r"^[a-z0-9\-\s\.']+$", w):
        pattern = r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])"
        return bool(re.search(pattern, text_lower))
    return w in text_lower


def en_name_candidates(en_name: str) -> List[str]:
    """从 en_name 生成英文候选词：**只认全名与姓氏，绝不拆出单名**。

    踩过的坑（线上 272x 幻觉的来源）：
      en_name = "Luka Doncic" → split() 得到 ["Luka", "Doncic"]，
      于是 "Luka Garza 卢卡 加尔扎" 的卡被当成东契奇的，红线按 ¥3780 算，
      现价 ¥11 → 显示 75 倍暴利。单名重名率太高，一律不认。
    姓氏（parts[-1]）足够有辨识度，又能兼容卖家只写 "Korda" 的情况。
    """
    e = (en_name or "").strip()
    if not e:
        return []
    parts = e.split()
    return [e] if len(parts) == 1 else [e, parts[-1]]


# 全部监控对象的主关键词（load_watchlist 时刷新），用于识别"一标题多球星"
_ALL_PLAYER_KEYS: List[str] = []
# 全部监控对象的英文名 token（token, 所属 keyword），用于"标题写的是别人"的判定
_ALL_EN_TOKENS: List[Tuple[str, str]] = []


def count_matched_players(text_lower: str) -> int:
    """标题里同时出现了几个监控球星 —— 用于拦截多人卡/合集。"""
    n = 0
    for kw in _ALL_PLAYER_KEYS:
        if kw and word_in_text(kw, text_lower):
            n += 1
    return n


# ------------------------------------------------------------------------------
# 中文简称的"假名字"守卫
#
# 中文没有词边界，两三个字的简称会随机命中别人名字的内部：
#   凯沙【德约】翰逊  → 被当成德约科维奇的卡（红线 ¥3780，现价 ¥11 → 272 倍幻觉）
#   若昂贝兰热【贝林】格 → 贝林厄姆
#   玛丽亚【萨卡】里 → 萨卡
#   【马克西】莫 阿科斯塔 → 马克西
# 判据：命中处后接的汉字若是常见音译用字，且「简称+该字」不是同一球员更长词条的前缀，
# 就认定这一处命中是"别人的名字内部"，不算数。
# ------------------------------------------------------------------------------
TRANSLIT_CHARS = set(
    "尼莫格尔斯拉德森逊奇夫娃娜巴达亚瓦佩雷托蒂库贝科特普哈马卡罗阿埃伊奥尤布茨敦豪洛维莱"
    "翰里恩杰兰迪治威廉查加米休乔妮珊诺塔姆希丘兹菲辛登堡塞舍舒泰坦佩彭斯"
)

# 这些字在标题里是「卡片语义」而不是人名音译（卡签/卡折/整张…），
# 一律当作音译块的**终止符**，否则「东契奇卡签」会被扩成「东契奇卡」而误杀。
# 只收那些几乎不可能出现在球员译名内部、但天天出现在卡圈标题里的字。
BLOCK_STOP_CHARS = set("卡签折编张套盒包版新秀特RC")

# 「XX队友」「XX副手」——卖家蹭热度写在一起，卡不是他的
RIVAL_WORDS = ("队友", "前队友", "副手", "搭档", "同门", "师弟", "模板")


def _translit_block(text: str, start: int, end: int) -> str:
    """把 [start,end) 这段命中向左右扩成连续的「音译用字」连通块。

    中文没有词边界，所以「这个短简称是不是嵌在别人的名字里」只能靠
    「它左右粘着的音译字有多长」来判断：
        凯沙【德约】翰逊  → 连通块 德约翰逊（5 字 > 简称 2 字）
        玛丽亚【萨卡】里  → 连通块 亚萨卡里（4 字 > 2 字）
        【马克西】莫      → 连通块 马克西莫（4 字 > 3 字）
    合法长名正好相反：连通块本身**就是**那个长译名，例如
        德约 在 【德约科维奇】 里 → 连通块 德约科维奇（在合法长名列表里）
    """
    left = start
    while left > 0:
        ch = text[left - 1]
        if ch in BLOCK_STOP_CHARS or ch not in TRANSLIT_CHARS:
            break
        left -= 1
    right = end
    while right < len(text):
        ch = text[right]
        if ch in BLOCK_STOP_CHARS or ch not in TRANSLIT_CHARS:
            break
        right += 1
    return text[left:right]


def _normalize_cn(name: str) -> str:
    """去掉分隔点与空白，用来做「连通块 ⊆ 合法长名」的包含判断。"""
    return re.sub(r"[\s·•・\.\-]", "", str(name or ""))


def player_name_matched(cand: str, text_lower: str, all_cands: List[str]) -> bool:
    """带"假名字"守卫的球员名匹配。只用于球员识别，黑名单仍走 word_in_text。"""
    if not cand or len(cand) < 2:
        return False
    if not re.match(r"^[\u4e00-\u9fff]+$", cand):
        return word_in_text(cand, text_lower)

    legal = [_normalize_cn(o) for o in all_cands if len(o) > len(cand)]

    pos = text_lower.find(cand)
    while pos >= 0:
        end = pos + len(cand)
        block = _translit_block(text_lower, pos, end)
        if len(block) <= len(cand):
            return True                      # 没被音译字包住 → 干净命中
        # 连通块比简称长：只有当它整个落在某个合法长译名里，才算同一个人的名字。
        # 例：卢卡【卡东契奇】不会发生（卡 是终止符），但 克里斯蒂亚诺【罗纳尔多】
        # 这类「音译长名里含短名」的情况必须放行。
        if any(block in name or name in block for name in legal):
            return True
        pos = text_lower.find(cand, pos + 1)
    return False


# 撞名率极高的英文大姓：**单独出现绝不作为身份判据**，必须同时出现首名或全名。
#
# 线上实测（2026-10-20 一次扫描就踩到三条）：
#   2019-20 Panini Encased Carsen Edwards 卡森 爱德华兹 …
#     → 被当成 Anthony Edwards，红线 ¥5945 / 现价 ¥49 = 122 倍幻觉
#   Trey Alexander RC 特雷 亚历山大 新秀…
#     → 被当成 SGA，红线 ¥2012 / 现价 ¥1 = 2012 倍幻觉
#   Alexandre Pantoja UFC 亚历山大 潘多哈 前蝇量级冠军
#     → 被当成 SGA（中文「亚历山大」直接命中），红线 ¥2012 / 现价 ¥60
# 这些都是**同姓不同人**，仅靠 exclude_words 一个个补永远追不完
# （Edwards 有 Carsen/Jesse/Justin，Alexander 有 Trey/Neil…）。
COMMON_SURNAMES = {
    "edwards", "alexander", "green", "brown", "walker", "white", "young",
    "thomas", "jackson", "harris", "martin", "robinson", "lewis", "johnson",
    "williams", "smith", "jones", "davis", "miller", "wilson", "moore",
    "taylor", "anderson", "hill", "scott", "clark", "allen", "baker",
    "hall", "king", "wright", "adams", "nelson", "carter", "mitchell",
    "roberts", "turner", "phillips", "campbell", "parker", "evans",
    "collins", "murphy", "rogers", "cooper", "reed", "bell", "bailey",
    "howard", "ward", "bennett", "brooks", "gray", "james", "wiggins",
    # 扩展易撞名球星姓氏（防同姓亲属/同姓边缘球员）
    "djokovic", "cunningham", "korda", "jovic", "alcaraz", "bellingham",
    "mbappe", "antetokounmpo", "curry", "thompson", "sarr", "morant",
    "holmgren", "porter", "murray", "bridges", "wagner", "holiday",
    "coulibaly", "hazard",
}

# 监控球星已知同姓/冲突名单（命中直杀）
CONFLICTING_NAMES_MAP = {
    "德约科维奇": ["lazar", "拉扎尔", "djordje", "乔尔杰", "marko", "马尔科"],
    "坎宁安":     ["jared", "贾里德"],
    "科达":       ["petr", "彼得", "nelly", "奈莉", "jessica", "杰西卡"],
    "阿尔卡拉斯": ["ruben", "鲁本"],
    "贝林厄姆":   ["jobe", "乔布"],
    "姆巴佩":     ["ethan", "伊森"],
    "字母哥":     ["thanasis", "萨纳西斯", "kostas", "科斯塔斯", "alex", "亚历克斯"],
    "爱德华兹":   ["carsen", "卡森", "justin", "贾斯汀"],
    "亚历山大":   ["trey", "特雷", "alexandre", "潘多哈", "pantoja"],
    "阿门":       ["ausar", "奥萨尔", "klay", "克莱"],
    "萨尔":       ["olivier", "奥利维耶", "奥利维亚"],
}


def en_name_verified(player_cfg: Dict[str, Any], text_lower: str) -> bool:
    """标题里能不能找到本球员的英文名（全名 / 首名 / 够长的名 / 非大姓的姓氏）。

    姓氏保持「Gilgeous-Alexander」这种连字符整体，**绝不能再拆成 Alexander**——
    否则 Alexander Bublik 的网球卡会被当成 SGA。
    卖家经常只写名不写姓（「Giannis Daily Dribble」），所以长度 ≥6 的名字也算数；
    Luka/Shai 这种 4 字母的短名不算（它们跟别人撞得太狠）。

    大姓（COMMON_SURNAMES）是例外：只认全名或首名。
    「Carsen Edwards」含 edwards 就通过的话，Carsen 的卡会被算成 Anthony 的。
    """
    en = (player_cfg.get("en_name", "") or "").strip()
    if not en:
        return False
    parts = en.split()
    surname = parts[-1].lower() if len(parts) > 1 else ""

    if surname in COMMON_SURNAMES:
        # 大姓：只认「全名」与「首名」，姓氏本身不作为判据
        tokens = [en] + [p for p in parts[:-1] if len(p) >= 6]
    else:
        tokens = list(en_name_candidates(en))
        tokens += [p for p in parts if len(p) >= 6 and p not in tokens]

    # alt_keywords 里的纯拉丁缩写本身就是强身份标识（SGA 之于 SGA）。
    # 不补这一条的话，「Prizm SGA 亚历山大 银折」在 require_en 下会被误杀。
    for ak in (player_cfg.get("alt_keywords") or []):
        a = (ak or "").strip().lower()
        if len(a) >= 3 and re.match(r"^[a-z0-9\-\.\s']+$", a) and a not in tokens:
            tokens.append(a)

    for tok in tokens:
        if len(tok) >= 3 and word_in_text(tok, text_lower):
            return True
    return False


def is_sealed_product(title: str) -> Tuple[bool, str]:
    """判断标题是否为未拆封整包/整盒/多卡合售。

    返回 (是否命中, 命中的词)。宁可错杀一包，也不要按单卡价去拍一整盒。
    """
    t = (title or "").lower()
    for noise in SHIPPING_NOISE:
        t = t.replace(noise, " ")
    for pat in SEALED_RE:
        m = pat.search(t)
        if m:
            return True, m.group(0).strip()
    for pat in SEALED_WEAK_RE:
        m = pat.search(t)
        if m:
            return True, m.group(0).strip()
    return False, ""


# 本轮词库加载发现的问题（供 run_radar_scan 汇总进 stats / 推送告警）
_WATCHLIST_PROBLEMS: List[str] = []


def _normalize_watchlist(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """校验并规整词库条目，返回 (可用条目, 问题清单)。

    为什么必须做：watchlist.json 是手工维护的热加载文件（线上 200KB 量级），
    一个手误就可能让整轮扫描静默跑偏，或者直接崩掉整晚：

      - 条目缺 keyword  → run_radar_scan 里的 order 字典会 KeyError，
                          而它不在任何 try 内，整轮扫描直接丢失
      - keyword 重复    → 同一球员被扫两遍，白占另一人的请求配额
      - 条目不是对象     → 下游 cfg.get() 直接 AttributeError

    原则：**错一条不能演变成整晚不跑**。剔掉不可用条目并逐条报告，
    让问题出现在日志和推送里，而不是让雷达安静地少扫几个人。
    """
    problems: List[str] = []
    entries: List[Dict[str, Any]] = []
    seen = set()

    if not isinstance(raw, list):
        return [], ["词库根节点应为数组，实际是 %s" % type(raw).__name__]

    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append("第 %d 条不是对象，已跳过" % (idx + 1))
            continue
        kw_raw = item.get("keyword")
        kw = str(kw_raw).strip() if kw_raw is not None else ""
        if not kw:
            problems.append("第 %d 条缺少 keyword，已跳过" % (idx + 1))
            continue
        key = kw.lower()
        if key in seen:
            problems.append("关键词 %r 重复出现，已保留首次" % kw)
            continue
        seen.add(key)
        if item.get("en_name") is not None:
            item = dict(item)
            item["en_name"] = str(item["en_name"]).strip()
        entries.append(item)

    return entries, problems


def load_watchlist() -> List[Dict[str, Any]]:
    """支持从 config/watchlist.json 热加载词库，若不存在则自动生成默认文件。

    回退到内置词库时**必须是深拷贝**：apply_learned_rules 会原地往每个 cfg 里
    写 exclude_words / _calib / _tier_penalty，直接返回模块级全局对象会让这些
    写入跨轮次累积（exclude_words 无限变长），而且外部完全看不出来。
    """
    result: Optional[List[Dict[str, Any]]] = None
    problems: List[str] = []

    if os.path.exists(EXTERNAL_WATCHLIST_PATH):
        try:
            with open(EXTERNAL_WATCHLIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:                  # noqa: BLE001
            data = None
            problems.append(
                "watchlist.json 解析失败（%s）—— 本轮已回退内置词库，"
                "你配置的档位/红线/排除词全部未生效" % exc)
        if data is not None:
            entries, shape_problems = _normalize_watchlist(data)
            problems.extend(shape_problems)
            if entries:
                result = entries
            else:
                problems.append("外部词库没有任何可用条目，本轮回退内置词库")
    else:
        try:
            with open(EXTERNAL_WATCHLIST_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GOLDEN_WATCHLIST, f, ensure_ascii=False, indent=2)
            logger.info(f"已初始化外部热加载词库配置文件: {EXTERNAL_WATCHLIST_PATH}")
        except Exception as exc:                  # noqa: BLE001
            problems.append("写入默认 watchlist.json 失败: %s" % exc)

    if result is None:
        result = copy.deepcopy(DEFAULT_GOLDEN_WATCHLIST)

    # 词库问题必须响亮：静默回退会让用户以为在扫自己的名单，实际扫的是别人的
    for p in problems:
        logger.error("[词库] %s", p)
    _WATCHLIST_PROBLEMS[:] = problems

    # 刷新全局球星名单，供"多人卡"拦截与"串名"判定使用
    _ALL_PLAYER_KEYS[:] = [c.get("keyword", "") for c in result if c.get("keyword")]
    _ALL_EN_TOKENS[:] = [
        (tok, c.get("keyword", ""))
        for c in result
        for tok in en_name_candidates(c.get("en_name", ""))
        if len(tok) >= 4
    ]
    return result


# ==============================================================================
# 三、 SQLite 跨时段状态追踪数据库 (支撑 17:00 与 19:20 差价/热度对比)
# ==============================================================================

SNAPSHOT_JSON_PATH = os.path.join(DATA_DIR, "radar_snapshots.json")

# ------------------------------------------------------------------------------
# 命中流水（hits.jsonl）——供 Card Vault 网页端只读消费
#
# 为什么用 JSONL 而不是共享 SQLite：
#   Card Vault 与雷达是两个独立容器。共享同一个 .db 会有锁冲突，
#   而命中量每天只有几条，append-only 的 JSONL 完全够用，且天然免锁。
# ------------------------------------------------------------------------------
HITS_JSONL_PATH = os.path.join(DATA_DIR, "hits.jsonl")
HITS_MAX_LINES = int(os.getenv("HITS_MAX_LINES", "5000"))   # 超过则裁剪
HITS_KEEP_LINES = int(os.getenv("HITS_KEEP_LINES", "3000"))  # 裁剪时保留最近 N 行

# ------------------------------------------------------------------------------
# 原始候选池（离线回测的燃料）
#
# hits.jsonl 只记「通过了识别层」的卡，用它回测有个致命盲区：
# 你新加的拦截规则如果想拦的那张卡**当年就没进 hits**，回测里根本看不到它，
# 于是"新增拦截 0 条"会让你误以为规则没用。
# 所以每轮把**进识别层之前**的窗口内原始拍品也留一份精简记录。
# ------------------------------------------------------------------------------
RAW_CANDIDATES_PATH = os.path.join(DATA_DIR, "raw_candidates.jsonl")
RAW_CANDIDATES_MAX_LINES = int(os.getenv("RAW_CANDIDATES_MAX_LINES", "15000"))
RAW_CANDIDATES_KEEP_LINES = int(os.getenv("RAW_CANDIDATES_KEEP_LINES", "10000"))

# 整轮原始召回的硬下限：低于它就在心跳标题上喊「疑似限流」
LOW_RECALL_THRESHOLD = int(os.getenv("LOW_RECALL_THRESHOLD", "200"))

# 离线回测的上次结果（用来算「新增拦截/新增放行」）
BACKTEST_REPORT_PATH = os.path.join(DATA_DIR, "backtest_report.json")

# 扫描健康状态（Card Vault 网页端读它判断"安静"还是"坏了"）
STATUS_JSON_PATH = os.path.join(DATA_DIR, "radar_status.json")

# ------------------------------------------------------------------------------
# 手动刷新通道（网页点「刷新」→ 真的重新抓一遍）
#
# 为什么需要：雷达一天只扫两次（17:00 / 19:20），到第二天早上网页上的价格
# 早就脱节了。原来那个「刷新」只是重新读一遍旧 JSONL，越刷越没用。
#
# 方向设计：雷达数据目录对 Card Vault 是 :ro 的，所以反过来也保持对等——
#   雷达只读 Card Vault 的目录，Card Vault 只读雷达的目录。
#   双方各写各的可写目录，谁都不需要对方的数据写权限。
#     Card Vault  → /app/data/radar_rescan.json   （请求）
#     radar       → /app/data/radar_state.json    （扫描中/已完成）
# ------------------------------------------------------------------------------
SCAN_STATE_PATH = os.path.join(DATA_DIR, "radar_state.json")
VAULT_DIR = os.getenv("VAULT_DIR", "/app/data/vault")
RESCAN_REQUEST_PATH = os.path.join(VAULT_DIR, "radar_rescan.json")
RESCAN_SEEN_PATH = os.path.join(DATA_DIR, "rescan_seen.json")
# 请求超过这个时长才被消费，说明期间容器停过，直接丢弃（否则一起来就狂扫一轮）
RESCAN_MAX_AGE_SEC = int(os.getenv("RESCAN_MAX_AGE_SEC", "900"))

# ------------------------------------------------------------------------------
# Card Vault「在库卡片行情」通道（vault_quotes）
#
# 与 rescan 同一套方向约定，但多两个方向相反的文件：
#   Card Vault → vault_inventory.json（在库清单，含 search_query）
#   Card Vault → vault_quote_req.json（用户点了「🔄 刷新库存行情」）
#   radar      → vault_quotes.json   （每张卡的行情样本中位数，写回**雷达自己**的 data 目录）
#
# 为什么结果写在雷达这边：Card Vault 已经把雷达的 data 以 :ro 挂进来了，
# 雷达写那里等于「自己写自己」，两边都不需要对方的写权限。
# ------------------------------------------------------------------------------
VAULT_INVENTORY_PATH = os.path.join(VAULT_DIR, "vault_inventory.json")
VAULT_QUOTE_REQUEST_PATH = os.path.join(VAULT_DIR, "vault_quote_req.json")
VAULT_QUOTE_SEEN_PATH = os.path.join(DATA_DIR, "vault_quote_seen.json")
VAULT_QUOTES_PATH = os.path.join(DATA_DIR, "vault_quotes.json")
# 请求超过 1 小时才被消费就不做了（用户早就刷新过页面了，抓到也是白烧流量）
VAULT_QUOTE_REQ_MAX_AGE_SEC = int(os.getenv("VAULT_QUOTE_REQ_MAX_AGE_SEC", "3600"))
# 没有手动请求时，库存行情最多 72 小时重扫一次
VAULT_QUOTE_INTERVAL_HOURS = float(os.getenv("VAULT_QUOTE_INTERVAL_HOURS", "72"))
# 一轮最多问几张卡：111 张 × 1 次搜索 ≈ 1.5 分钟，留足余量
VAULT_QUOTE_MAX_CARDS = int(os.getenv("VAULT_QUOTE_MAX_CARDS", "220"))
VAULT_QUOTE_SAMPLE_MAX = int(os.getenv("VAULT_QUOTE_SAMPLE_MAX", "3"))

# ------------------------------------------------------------------------------
# 行情口径：**只用已成交记录**，不用在拍当前价。
#
# 2026-10-25 纠错（用户当面指出，之前的设计是错的）：
#   旧实现拿「正在拍卖的 current_price」当行情参考，这在统计上没有意义——
#   开拍早期的现价只是"当前出价位置"，一张会拍到 ¥1,500 的卡在开拍第 1 小时
#   可能只显示 ¥3（出价 13 次也才 ¥3，因为每次加价幅度很小）。
#   用这种数算中位数，等于系统性地把行情压低一到两个数量级。
#
#   更要命的是旧代码里那段"优先用 closed_fills 成交价"根本没生效：
#   搜索接口锁死 Status=1（只返回在售），已结标的 item 永远不会出现在结果里，
#   所以 fills.get(item_id) 一次都命中不了（实跑 basis 全是 active，0 条 fill）。
#
# 现在改走「售出的宝贝」：卡淘网页那个 tab 的真实调用是 SeachByStatus('Status',-2)，
# 即 searchJson=[{"Key":"Status","Value":-2}]。实测关键词 "2023 Prizm Wembanyama"
# 返回 TotalCount=10859 条历史成交，字段 richest：
#   Price       = 起拍价（拍卖类几乎恒为 1.00，不是成交价）
#   LowestPrice = **最终成交价**（这才是行情）
#   PriceCount  = 出价次数（0 = 流拍，必须剔除）
#   EffectiveDate = 截标时间（用来做时间窗）
# ------------------------------------------------------------------------------
SEARCH_JSON_ACTIVE = json.dumps([{"Key": "Status", "Value": 1}], separators=(",", ":"))
SEARCH_JSON_SOLD = json.dumps([{"Key": "Status", "Value": -2}], separators=(",", ":"))
# 只采信最近 N 天的成交。卡市一年能翻一倍也能腰斩，三年前的成交价不是"行情"。
VAULT_QUOTE_SOLD_MAX_DAYS = int(os.getenv("VAULT_QUOTE_SOLD_MAX_DAYS", "120"))
# 每张卡最多翻几页成交（每页 60 条）。**自适应**：样本够了就提前停，
# 热门球员翻 8 页只覆盖两三天，再多也没意义；冷门卡第 2 页就空。
VAULT_QUOTE_SOLD_PAGES = int(os.getenv("VAULT_QUOTE_SOLD_PAGES", "3"))
VAULT_QUOTE_SOLD_MAX_PAGES = int(os.getenv("VAULT_QUOTE_SOLD_MAX_PAGES", "8"))
# 凑够多少笔同款成交就不再往下翻（中位数 8 笔已经足够稳）
VAULT_QUOTE_TARGET_SAMPLES = int(os.getenv("VAULT_QUOTE_TARGET_SAMPLES", "8"))
# 参与中位数的成交样本上限。中位数对离群本来就稳，30 条足够且不会被刷屏。
VAULT_QUOTE_SOLD_MAX_SAMPLES = int(os.getenv("VAULT_QUOTE_SOLD_MAX_SAMPLES", "30"))
# 「起拍价」口径的准入地板。保留给"一条成交都没有"时的兜底展示，
# 且必须明确标注 basis=start，绝不与成交价混算。
VAULT_QUOTE_MIN_START_RMB = float(os.getenv("VAULT_QUOTE_MIN_START_RMB", "10"))
# 1 元起拍且只有 1 次出价的成交 = 无人竞争的地板价，不是"行情"。
# 实测：埃森格 Instinct 特卡两笔都是 ¥1.00 / 1 次出价，中位数报 ¥1，用户第一反应是瞎报。
# 阈值内且无人加价的样本直接丢；正常低价卡（¥3 有 4 次出价）不受影响。
VAULT_QUOTE_JUNK_RMB = float(os.getenv("VAULT_QUOTE_JUNK_RMB", "2"))


def record_hit(hit: Dict[str, Any], mode: str) -> None:
    """把一条命中追加到 hits.jsonl。单写者（仅本进程），无需加锁。"""
    now = _now_cn()
    record = dict(hit)
    record.pop("trend_tag", None)          # 盘口标签属于快照，网页端重新计算
    record["scan_mode"] = mode
    record["scan_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    record["scan_date"] = now.strftime("%Y-%m-%d")
    record["scan_ts"] = int(now.timestamp())
    try:
        with open(HITS_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"[hits] 写入命中流水失败: {exc}")


def trim_hits() -> None:
    """命中流水超过上限时保留最近 N 行，防止无限增长撑爆磁盘。"""
    try:
        if not os.path.exists(HITS_JSONL_PATH):
            return
        with open(HITS_JSONL_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= HITS_MAX_LINES:
            return
        _atomic_write_text(HITS_JSONL_PATH, "".join(lines[-HITS_KEEP_LINES:]))
        logger.info(f"[hits] 命中流水已裁剪：{len(lines)} -> {HITS_KEEP_LINES} 行")
    except Exception as exc:
        logger.warning(f"[hits] 裁剪命中流水失败: {exc}")


def record_raw_candidates(rows: List[Dict[str, Any]], mode: str) -> int:
    """把「进识别层之前」的窗口内原始拍品留一份精简记录，供 --backtest 回放。

    只留重跑 evaluate_auction_item_v2 需要的字段：标题、价格、出价数、剩余分钟、
    运费、卖家信息。image_url / 描述之类的纯展示字段一律不存（体积翻好几倍）。
    """
    if not rows:
        return 0
    now = _now_cn()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(RAW_CANDIDATES_PATH, "a", encoding="utf-8") as f:
            for row in rows:
                slim = {k: row.get(k) for k in (
                    "id", "title", "current_price", "start_price", "lowest_price",
                    "bid_count", "out_count", "seller", "seller_id", "seller_review",
                    "seller_source", "shipping_rmb", "remaining_minutes",
                    "end_time_str", "end_time_full", "by_way", "guarantee")}
                slim["player_kw"] = row.get("player_kw", "")
                slim["scan_mode"] = mode
                slim["scan_time"] = stamp
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")
        return len(rows)
    except Exception as exc:                  # noqa: BLE001
        logger.warning("[raw] 写入原始候选池失败: %s", exc)
        return 0


def trim_raw_candidates() -> None:
    """原始候选池超上限时滚动保留最近 N 行（同 hits 的策略）。"""
    try:
        if not os.path.exists(RAW_CANDIDATES_PATH):
            return
        with open(RAW_CANDIDATES_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= RAW_CANDIDATES_MAX_LINES:
            return
        _atomic_write_text(RAW_CANDIDATES_PATH, "".join(lines[-RAW_CANDIDATES_KEEP_LINES:]))
        logger.info("[raw] 原始候选池已滚动裁剪：%d -> %d 行",
                    len(lines), RAW_CANDIDATES_KEEP_LINES)
    except Exception as exc:                  # noqa: BLE001
        logger.warning("[raw] 裁剪原始候选池失败: %s", exc)


# ==============================================================================
# 六、 成交回填 + 反馈学习闭环
#
# 目标一：用户没抢的卡也要知道最终成交价。
#   参照价是靠词库硬编码基准推出来的，只有真实成交价能验证它准不准。
#   做法：本轮入名单的卡全部登记到 watch_track.json（带 deadline_ts），
#   截标 10 分钟后回抓详情页拿 currentPrice = 最终成交价。
#   （实测：/market/item/{id} 服务端直出 id="currentPrice" 与 id="bidcount"，
#     截标很久的历史 id 依然能读到，bidcount=0 即流拍。）
#
# 目标二：用户点「有误」→ 分级学习。
#   L1 精确拦截：该 item_id 永久不再报（零风险，立即生效）
#   L2 排除词：从被标错的标题里提取"不该出现的词"，进 learned_rules.json。
#              只生成建议，需在网页确认后才生效 —— 自动改词库误杀代价太高。
#   L3 档位校准：同 (球员, 档位) 真实成交样本 >=3 时，按中位数反推参照价，
#              单次调整幅度封顶 ±45%，避免小样本把参照价带飞。
# ==============================================================================

WATCH_TRACK = os.path.join(DATA_DIR, "watch_track.json")
CLOSED_FILLS = os.path.join(DATA_DIR, "closed_fills.jsonl")
CALIBRATION = os.path.join(DATA_DIR, "calibration.json")
LEARNED_RULES = os.path.join(DATA_DIR, "learned_rules.json")
# card-vault 的 radar_ctl 以 :ro 挂在 /app/data/vault
FEEDBACK_PATH = os.path.join(DATA_DIR, "vault", "feedback.jsonl")
# 你在网页上"确认采纳"的排除词（L2 只有进这里才真正生效）
APPROVALS_PATH = os.path.join(DATA_DIR, "vault", "approvals.json")

# 截标后多久抓终局价。卡淘最后时刻会延时竞价，留 10 分钟足够。
FILL_GRACE_MIN = float(os.getenv("FILL_GRACE_MIN", "10"))
# 追踪名单最长保留多久，防止无限增长
FILL_TRACK_HOURS = float(os.getenv("FILL_TRACK_HOURS", "72"))
# 每轮最多回抓多少张，避免一次扫描被详情页请求拖垮
FILL_MAX_PER_ROUND = int(os.getenv("FILL_MAX_PER_ROUND", "40"))
# 同一(球员,档位)至少几个真实成交样本才允许校准参照价。
# 注意：改为 EB 收缩后这个数字**不再是硬闸门**，只作为「多少单以上才敢报给用户看」的
# 展示门槛；真正决定权重的是一单一单的样本数 n 与伪样本数 CALIB_EB_K。
CALIB_MIN_SAMPLES = int(os.getenv("CALIB_MIN_SAMPLES", "3"))
# 单次校准最大调整幅度
CALIB_MAX_SHIFT = float(os.getenv("CALIB_MAX_SHIFT", "0.45"))
# 校准样本池的离群值硬门槛：成交价 / 参照价 落在这个区间之外的一律不参与校准。
# 这些样本几乎全是脏数据（错绑球员、把整盒价记成单卡、参照价本身错档），
# 一两条就能把中位数拽偏 30% 以上。
CALIB_RATIO_FLOOR = float(os.getenv("CALIB_RATIO_FLOOR", "0.18"))
CALIB_RATIO_CEIL = float(os.getenv("CALIB_RATIO_CEIL", "1.80"))
# EB 收缩的伪样本权重。K=4 的含义：一档攒到 4 笔真实成交时，
# 该档自己的证据才和「全局先验」平分秋色——小于 4 笔时主要听全局的。
CALIB_EB_K = float(os.getenv("CALIB_EB_K", "4.0"))

# ---- 全局兜底校准 ----
# tier 级校准要求「同一球员同一档位」攒够 3 笔，可监测面是 34 人 × 十几个档位，
# 靠这个速度永远等不到第一份校准。而实测的偏差根本不是档位差异——
# 是**平台级**偏差：参照价来自 eBay 美元 comps，成交发生在卡淘国内场，
# 两者之间有一个稳定的折价。这种偏差必须允许跨档位合并统计，否则永远不会生效。
CALIB_GLOBAL_KEY = "__global__"
# 跨档位合并时要求的最小样本（比单档门槛高：混了不同档位，容错更低）
CALIB_GLOBAL_MIN_SAMPLES = int(os.getenv("CALIB_GLOBAL_MIN_SAMPLES", "4"))
# 全局因子的下限。刻意做成**只许下调**：目前没有任何证据表明参照价被系统性低估，
# 而"虚高"是单向危害 —— 它会把一堆假漏直接推到你面前，比漏掉几张真漏恶劣得多。
CALIB_GLOBAL_FLOOR = float(os.getenv("CALIB_GLOBAL_FLOOR", "0.45"))


def _atomic_write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())      # 断电时也不留下半截文件
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    """原子写文本：先写同目录临时文件，fsync 落盘，再 os.replace 换上去。

    为什么必须有这个：流水文件一旦用 "w" 直接截断重写，
    进程在 truncate 之后、写完之前被杀（NAS 重启 / OOM / docker kill），
    用户攒了几个月的命中历史就只剩一个空文件。这类损失不可逆。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json_safe(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------- 成交回填


def load_watch_track() -> Dict[str, Any]:
    return _load_json_safe(WATCH_TRACK, {"items": {}})


def register_watch(hits: List[Dict[str, Any]]) -> None:
    """把本轮入名单的卡登记进追踪表，等截标后回抓成交价。"""
    track = load_watch_track()
    items = track.setdefault("items", {})
    now_ts = int(_now_cn().timestamp())
    for h in hits:
        iid = str(h.get("item_id") or "")
        if not iid:
            continue
        deadline = int(h.get("scan_ts") or now_ts) + int(h.get("remaining_minutes") or 0) * 60
        prev = items.get(iid) or {}
        items[iid] = {
            "item_id": iid,
            "deadline_ts": deadline,
            "player_cn": h.get("player_cn", ""),
            "player_en": h.get("player_en", ""),
            "sport": h.get("sport", ""),
            "title": h.get("title", ""),
            "tier_key": h.get("tier_key", ""),
            "tier_desc": h.get("tier_desc", ""),
            "tier_basis": h.get("tier_basis", ""),
            "opportunity_type": h.get("opportunity_type", ""),
            "ref_ebay_rmb": h.get("ebay_ref_rmb", 0),
            "max_buy_rmb": h.get("max_buy_rmb", 0),
            "price_at_scan": h.get("current_price", 0),
            "bid_at_scan": h.get("bid_count", 0),
            "registered_ts": prev.get("registered_ts") or now_ts,
            "fill_done": prev.get("fill_done", False),
        }
    # 过期太久的一律丢弃，别让追踪表变成垃圾场
    cutoff = now_ts - FILL_TRACK_HOURS * 3600
    items = {k: v for k, v in items.items()
             if int(v.get("deadline_ts") or 0) >= cutoff or v.get("fill_done")}
    track["items"] = items
    track["updated_ts"] = now_ts
    _atomic_write_json(WATCH_TRACK, track)


def fetch_final_price(item_id: str) -> Optional[Dict[str, Any]]:
    """抓详情页拿最终成交价。返回 None 表示没抓成功（下轮再试）。"""
    url = CARDHOBBY_ITEM_URL_TPL.format(item_id=item_id)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": CF_HEADERS["User-Agent"],
            "Referer": "https://www.cardhobby.com.cn/",
        })
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001
        logger.warning("[回填] 详情页请求失败 %s: %s", item_id, exc)
        return None

    m = re.search(r'id="currentPrice"[^>]*>(.*?)</span>', body, re.S)
    if not m:
        logger.warning("[回填] %s 页面里找不到 currentPrice（可能已下架）", item_id)
        return None
    raw_price = re.sub(r"\s+", "", m.group(1)).replace(",", "")
    try:
        final_price = float(raw_price)
    except ValueError:
        return None

    bc = re.search(r'id="bidcount"[^>]*>(\d+)</span>', body)
    bid_count = int(bc.group(1)) if bc else 0
    return {"final_price": final_price, "bid_count_final": bid_count,
            "is_unsold": bid_count == 0}


def harvest_closed_fills() -> int:
    """回抓已截标且过了宽限期的卡，写入 closed_fills.jsonl。返回处理条数。"""
    track = load_watch_track()
    items = track.get("items") or {}
    if not items:
        return 0

    now_ts = int(_now_cn().timestamp())
    grace = FILL_GRACE_MIN * 60
    due = [v for v in items.values()
           if not v.get("fill_done") and int(v.get("deadline_ts") or 0) + grace <= now_ts]
    if not due:
        return 0
    due.sort(key=lambda v: int(v.get("deadline_ts") or 0))
    due = due[:FILL_MAX_PER_ROUND]
    logger.info("[回填] 到期可回填 %d 张，开始抓终局价", len(due))

    ok = 0
    for rec in due:
        iid = rec["item_id"]
        info = fetch_final_price(iid)
        if not info:
            continue
        fill = dict(rec)
        fill.update(info)
        fill["closed_at"] = now_ts
        # 雷达当时给的判断 vs 真实成交 —— 这就是校准的依据
        ref = float(rec.get("ref_ebay_rmb") or 0)
        red = float(rec.get("max_buy_rmb") or 0)
        fill["ref_over_final"] = round(ref / info["final_price"], 3) if info["final_price"] else None
        fill["red_over_final"] = round(red / info["final_price"], 3) if info["final_price"] else None
        try:
            with open(CLOSED_FILLS, "a", encoding="utf-8") as f:
                f.write(json.dumps(fill, ensure_ascii=False) + "\n")
        except Exception as exc:                  # noqa: BLE001
            logger.warning("[回填] 写 closed_fills 失败: %s", exc)
            continue
        rec["fill_done"] = True
        rec["final_price"] = info["final_price"]
        ok += 1
        logger.info("  [成交] %s %s | 成交 ¥%.0f / %d 次出价%s | 雷达参照 ¥%.0f",
                    rec.get("player_cn", ""), (rec.get("title") or "")[:32],
                    info["final_price"], info["bid_count_final"],
                    "（流拍）" if info["is_unsold"] else "", ref)

    track["items"] = items
    track["updated_ts"] = now_ts
    _atomic_write_json(WATCH_TRACK, track)
    return ok


# ---------------------------------------------------------------- 档位校准


def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def compute_global_factor(ratios: List[float]) -> Dict[str, Any]:
    """把一堆「成交 / 参照」比值折成**全局折价因子**（纯函数，方便自检）。

    只许下调：没有任何证据表明参照价被系统性低估，而虚高是单向危害 ——
    它会把假漏直接推到眼前。所以上限锁死 1.0，下限由 CALIB_GLOBAL_FLOOR 兜住。
    """
    if len(ratios) < CALIB_GLOBAL_MIN_SAMPLES:
        return {"samples": len(ratios), "factor": None}
    raw = _median(ratios)
    return {
        "samples": len(ratios),
        "median_final_over_ref": round(raw, 3),
        "factor": round(max(CALIB_GLOBAL_FLOOR, min(1.0, raw)), 3),
    }


def recalibrate_tiers() -> Dict[str, Any]:
    """用真实成交反推各档位参照价的合理倍数，产出 calibration.json。

    三步，顺序不能换：
      1. **洗样本**：剔掉「你在网页点过有误」的拍品，再剔掉 成交/参照 比例落在
         [CALIB_RATIO_FLOOR, CALIB_RATIO_CEIL] 之外的极端离群。脏样本的破坏力
         远大于样本量不足 —— 一两条错绑球员的成交能把中位数拽偏 30%。
      2. **算先验**：跨档位合并的平台级折价（只许下调，见 compute_global_factor）。
      3. **EB 收缩**：每档自己的比值与全局先验按样本数加权，在对数空间插值。
         factor = exp( (Σlog r_i + K·log g) / (n + K) )

    为什么第 3 步不能用原来的「满 3 单硬切换」：
    n=2 时用全局、n=3 时突然全用自己的中位数——第 3 单只要比前两单高 50%，
    红线就跟着跳 50%，而且从此再也不回头。这是价格跳变悬崖，不是学习。
    对数空间 + 伪样本权重 K=4 让权重连续变化，第 3、4、5 单各自贡献 1/(n+K)。
    """
    if not os.path.exists(CLOSED_FILLS):
        return {}

    # ---- 1a. 脏样本来源之一：网页上被你标记「有误」的拍品 ----
    # 认错人 / 不是单卡 / 没签字 这几类的成交价根本不属于这个档位，
    # 拿它校准等于用别人的成交价改你自己的红线。
    excluded_ids = {str(fb.get("item_id") or "").strip()
                    for fb in load_feedback() if str(fb.get("item_id") or "").strip()}

    by_tier: Dict[str, List[float]] = {}
    detail: Dict[str, Any] = {}
    all_ratios: List[float] = []
    n_total = n_used = n_excluded = n_outlier = 0
    try:
        with open(CLOSED_FILLS, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                n_total += 1
                if rec.get("is_unsold"):
                    continue
                if str(rec.get("item_id") or "").strip() in excluded_ids:
                    n_excluded += 1
                    continue
                ref = float(rec.get("ref_ebay_rmb") or 0)
                final = float(rec.get("final_price") or 0)
                if ref <= 0 or final <= 0:
                    continue
                # ---- 1b. 极端离群：成交价与参照价差 5 倍以上，几乎必然是脏数据 ----
                ratio = final / ref
                if ratio < CALIB_RATIO_FLOOR or ratio > CALIB_RATIO_CEIL:
                    n_outlier += 1
                    logger.debug("[校准] 离群样本剔除 %.3f（¥%s / 参照¥%s）",
                                 ratio, final, ref)
                    continue
                key = "%s|%s" % (rec.get("player_cn", ""), rec.get("tier_key", ""))
                by_tier.setdefault(key, []).append(ratio)
                detail.setdefault(key, {"player_cn": rec.get("player_cn", ""),
                                        "tier_key": rec.get("tier_key", ""),
                                        "tier_desc": rec.get("tier_desc", ""),
                                        "finals": [], "refs": []})
                detail[key]["finals"].append(final)
                detail[key]["refs"].append(ref)
                all_ratios.append(ratio)
                n_used += 1
    except OSError:
        return {}

    # ---- 2. 全局先验 ----
    g_calc = compute_global_factor(all_ratios)
    global_factor = float(g_calc.get("factor") or 1.0)
    log_prior = math.log(global_factor) if global_factor > 0 else 0.0

    # ---- 3. 对数空间 EB 收缩 ----
    tiers: Dict[str, Any] = {}
    k = max(0.0, CALIB_EB_K)
    for key, ratios in by_tier.items():
        n = len(ratios)
        if n <= 0:
            continue
        logs = [math.log(r) for r in ratios if r > 0]
        if not logs:
            continue
        shrunk = (sum(logs) + k * log_prior) / (n + k)
        factor = math.exp(shrunk)
        # 再封一次边：EB 已经连续了，但这道闸门防的是「一周内 5 单全撞上同一个黄牛」
        factor = max(1.0 - CALIB_MAX_SHIFT, min(1.0 + CALIB_MAX_SHIFT, factor))
        tiers[key] = {
            "player_cn": detail[key]["player_cn"],
            "tier_key": detail[key]["tier_key"],
            "tier_desc": detail[key]["tier_desc"],
            "samples": n,
            # 「自己证据占多大权重」——0.43 表示这一档自己的成交只说了 43% 的话，
            # 剩下 57% 听全局先验的。看着这个数字就知道该不该信这一档。
            "eb_weight": round(n / (n + k), 2) if (n + k) > 0 else 0.0,
            "median_final_rmb": round(_median(detail[key]["finals"])),
            "median_ref_rmb": round(_median(detail[key]["refs"])),
            "median_ratio": round(_median(ratios), 3),
            "factor": round(factor, 3),
        }

    # ---- 全局兜底因子：所有档位共享的平台级折价 ----
    # 只有 tier 级样本不足时才会用上，所以它永远打不过"更了解这张卡"的档位级校准。
    g_payload: Dict[str, Any] = {
        "samples": len(all_ratios),
        "min_samples": CALIB_GLOBAL_MIN_SAMPLES,
        "floor": CALIB_GLOBAL_FLOOR,
        "factor": None,
    }
    if g_calc.get("factor"):
        g_payload.update({
            "factor": g_calc["factor"],
            "median_final_over_ref": g_calc["median_final_over_ref"],
        })

    payload = {
        "generated_ts": int(_now_cn().timestamp()),
        "fills_total": n_total,
        "fills_used": n_used,
        "excluded_feedback": n_excluded,
        "outliers_dropped": n_outlier,
        "min_samples": CALIB_MIN_SAMPLES,
        "eb_k": k,
        "ratio_window": [CALIB_RATIO_FLOOR, CALIB_RATIO_CEIL],
        "tiers": tiers,
        "global": g_payload,
    }
    _atomic_write_json(CALIBRATION, payload)
    if n_excluded or n_outlier:
        logger.info("[校准] 洗样本：剔除 %d 条「你标过有误」、%d 条极端离群（比值不在 %.2f~%.2f）",
                    n_excluded, n_outlier, CALIB_RATIO_FLOOR, CALIB_RATIO_CEIL)
    if g_payload.get("factor"):
        logger.info("[校准] 全局折价 ×%.2f（%d 笔跨档位成交，中位数 成交/参照=%.2f，地板 %.2f）",
                    g_payload["factor"], g_payload["samples"],
                    g_payload["median_final_over_ref"], CALIB_GLOBAL_FLOOR)
    if tiers:
        logger.info("[校准] 累计真实成交 %d 条，%d 个档位有样本（EB 收缩 K=%.0f，向全局 ×%.2f 靠拢）",
                    n_used, len(tiers), k, global_factor)
        for t in list(tiers.values())[:6]:
            logger.info("  [校准] %s %s | 实测中位数 ¥%s vs 参照 ¥%s → ×%.2f（%d 样本）",
                        t["player_cn"], t["tier_desc"], t["median_final_rmb"],
                        t["median_ref_rmb"], t["factor"], t["samples"])
    return payload


def load_calibration_factors() -> Dict[str, float]:
    data = _load_json_safe(CALIBRATION, {})
    out = {k: float(v.get("factor") or 1.0) for k, v in (data.get("tiers") or {}).items()}
    g = data.get("global") or {}
    if g.get("factor"):
        out[CALIB_GLOBAL_KEY] = float(g["factor"])
    return out


# ---------------------------------------------------------------- 反馈学习


FEEDBACK_REASONS = ("认错人", "不是单卡", "没签字", "档位偏高", "重复/已下架")

# 提炼「建议排除词」时不该被当成"别人名字"的通用词。
# 不排掉这些的话，你点几次有误，系统就会建议把 panini / chrome 加进排除词，
# 那等于把整条卡种都封了。
SET_WORDS_LOWER = {
    "panini", "topps", "chrome", "prizm", "select", "optic", "donruss", "mosaic",
    "immaculate", "national", "treasures", "origins", "obsidian", "noir", "encased",
    "pristine", "inception", "sapphire", "finest", "flawless", "eminence",
    "hoops", "contenders", "chronicles", "revolution", "absolute", "certified",
    "score", "leaf", "bowman", "stadium", "heritage", "gallery", "midnight",
    "signature", "class", "elite", "prestige", "spectra", "luminance",
    "unparalleled", "merlin", "match", "attax", "world", "cup", "bowman",
    "base", "insert", "refractor", "parallel", "auto", "autograph", "patch",
    "relic", "rookie", "card", "cards", "psa", "bgs", "sgc", "jsa", "grade",
    "gem", "mint", "gold", "silver", "green", "red", "blue", "orange", "purple",
    "black", "white", "wave", "shimmer", "sparkle", "holo", "prizm", "die",
    "cut", "parallel", "checklist", "team", "set", "box", "pack", "case",
    "jersey", "number", "authentic", "memorabilia", "tribute", "legend",
}


def load_feedback() -> List[Dict[str, Any]]:
    """读 card-vault 写出的反馈流水（雷达侧只读）。"""
    if not os.path.exists(FEEDBACK_PATH):
        return []
    out = []
    try:
        with open(FEEDBACK_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def rebuild_learned_rules(watchlist: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把反馈折算成可执行规则。每一类反馈都必须有**真实后果**，否则等于没点。

    L1（blocked_ids）：同一条拍品永不再报。零风险，立即生效。
    L2（suggest_exclude）：从「认错人」的标题里找本球员词库没有、但标题里有的
        英文人名 token，作为该球员的候选排除词。**不自动生效**，等网页确认。
    L2b（suggest_non_auto）：从「不是签字卡」的标题里找出**系列/insert 名**，
        建议加入非签字名单。采纳后这类卡仍会报，但只按折射轨估价 ——
        降档不拦卡，方向是安全的（宁可少赚，不可错给签字价）。
    L3a（tier_penalty）：「档位偏高」按 (球员,档位) 累计，每次 ×0.9，地板 0.60。
        成交校准是市场事实，人工判断是"你觉得贵"，权重低但不能为零。
    L3b（serial_suspect）：「编号认错」的拍品进存疑清单，连解析出的片段一起记，
        供人工看规律（大多数情况是卖家货号冒充编号，改解析比改规则有用）。
    """
    fb = load_feedback()
    if not fb:
        return _load_json_safe(LEARNED_RULES, {"blocked_ids": [], "suggest_exclude": {}})

    cfg_map = {c.get("keyword", ""): c for c in watchlist}
    # 人名 token 全集：球员的英文名/别名绝不能被当成"系列名"提出来。
    # 线上踩过：亚马尔那条反馈把 lamine / yamal 也提成了非签字候选词，
    # 采纳后会让所有亚马尔卡都降档 —— 学习学反了比不学更糟。
    person_tokens = set()
    for c in watchlist:
        for w in [c.get("en_name", ""), c.get("keyword", "")] + list(c.get("alt_keywords") or []):
            for tok in re.findall(r"[a-z][a-z'\-\.]{2,}", (w or "").lower()):
                person_tokens.add(tok)
    # 「不是签字卡」的候选词必须排除所有球员英文名 + 系列通用词，
    # 否则学反了：把球员名当"非签字系列"，会让这位球员的所有卡一起降档。
    non_auto_block = set(person_tokens) | set(SET_WORDS_LOWER)
    blocked: Dict[str, Dict[str, Any]] = {}
    suggest: Dict[str, Dict[str, int]] = {}
    non_auto: Dict[str, int] = {}
    tier_pen: Dict[str, Dict[str, Any]] = {}
    serial_suspect: List[Dict[str, Any]] = []

    for rec in fb:
        iid = str(rec.get("item_id") or "").strip()
        if iid:
            blocked[iid] = {"item_id": iid, "reason": rec.get("reason", ""),
                            "player_cn": rec.get("player_cn", ""),
                            "title": (rec.get("title") or "")[:80],
                            "ts": rec.get("ts", 0)}

        reason = rec.get("reason") or ""
        kw = rec.get("player_cn") or ""
        title_l = (rec.get("title") or "").lower()
        cfg = cfg_map.get(kw)

        # 编号存疑：连解析片段一起记，否则只知道"错了"却不知道错在哪一段
        if reason == "编号认错":
            parsed = parse_serial_details(rec.get("title") or "")
            serial_suspect.append({
                "item_id": iid, "player_cn": kw,
                "title": (rec.get("title") or "")[:100],
                "parsed_badge": parsed[2], "parsed_denom": parsed[0],
                "note": (rec.get("note") or "")[:80],
                "ts": rec.get("ts", 0),
            })

        # 档位偏高：按 (球员,档位) 累计人工投票
        if reason == "档位偏高":
            tk = (rec.get("tier_key") or "").strip()
            tk_desc = (rec.get("tier_desc") or "").strip()
            if kw and tk:
                key = "%s|%s" % (kw, tk)
                slot = tier_pen.setdefault(key, {"player_cn": kw, "tier_key": tk,
                                                 "tier_desc": tk_desc, "count": 0})
                slot["count"] += 1
                slot["tier_desc"] = slot["tier_desc"] or tk_desc

        # 「认错人」才谈得上提炼排除词；只有它有"别人名字"这个抓手
        if reason == "认错人":
            if not cfg:
                continue
            known = set()
            for w in ([cfg.get("keyword", ""), cfg.get("en_name", "")]
                      + list(cfg.get("alt_keywords") or [])
                      + list(cfg.get("exclude_words") or [])):
                for tok in re.findall(r"[a-z][a-z'\-\.]{2,}", (w or "").lower()):
                    known.add(tok)
            for tok in re.findall(r"[a-z][a-z'\-\.]{3,}", title_l):
                if tok in known or len(tok) < 4:
                    continue
                # 系列名/通用词不算"别人"
                if tok in SET_WORDS_LOWER:
                    continue
                suggest.setdefault(kw, {})
                suggest[kw][tok] = suggest[kw].get(tok, 0) + 1

        # 「不是签字卡」：抽系列/insert 名当"非签字"候选词
        if reason in ("不是签字卡", "没签字"):
            for tok in re.findall(r"[a-z][a-z'\-\.]{4,}", title_l):
                if tok in non_auto_block:
                    continue
                non_auto[tok] = non_auto.get(tok, 0) + 1

    # 同一 token 出现 >=2 次才建议，单次多为噪声
    suggest_out = {kw: [{"word": w, "count": c} for w, c in sorted(v.items(), key=lambda x: -x[1])
                        if c >= 2]
                   for kw, v in suggest.items()}
    suggest_out = {k: v for k, v in suggest_out.items() if v}

    # 非签字词门槛刻意放到 1：你亲眼看过图才点的，一次就够。
    # 但降档不拦卡，误收的代价只是少赚，所以敢放宽。
    non_auto_out = [{"word": w, "count": c}
                    for w, c in sorted(non_auto.items(), key=lambda x: -x[1])][:8]

    # 人工"档位偏高"→ 每次 ×0.9，地板 0.60
    penalties = {}
    for key, slot in tier_pen.items():
        n = int(slot["count"])
        penalties[key] = dict(slot, factor=round(max(0.60, 0.90 ** n), 3))

    approvals = _load_json_safe(APPROVALS_PATH, {})
    payload = {
        "generated_ts": int(_now_cn().timestamp()),
        "feedback_total": len(fb),
        "blocked_ids": sorted(blocked.values(), key=lambda x: -int(x.get("ts") or 0))[:800],
        "blocked_count": len(blocked),
        "suggest_exclude": suggest_out,
        "suggest_non_auto": non_auto_out,
        "tier_penalty": penalties,
        "serial_suspect": serial_suspect[-40:],
        # 网页上确认过的词：{球员关键词: [词, ...]}
        "approved_exclude": approvals.get("approved") or {},
        # 非签字名单是全局的（系列名不隶属某位球员）
        "approved_non_auto": approvals.get("approved_non_auto") or [],
    }
    _atomic_write_json(LEARNED_RULES, payload)
    if blocked:
        logger.info("[学习] 已生效拦截 %d 条（你标记过有误的拍品不再出现）", len(blocked))
    if suggest_out:
        logger.info("[学习] %d 位球员有新排除词建议，待网页确认后生效", len(suggest_out))
    if non_auto_out:
        logger.info("[学习] %d 个「特卡冒充签字」候选词待确认", len(non_auto_out))
    if penalties:
        logger.info("[学习] 人工判定档位偏高 %d 个档位，已按 ×0.9^n 下调",
                    len(penalties))
    return payload


def apply_learned_rules(watchlist: List[Dict[str, Any]],
                        rules: Dict[str, Any]) -> Dict[str, Any]:
    """把 L1/L2/L3 规则注入词库，返回本次生效的摘要。

    L1 blocked_ids → 直接跳过（在扫描循环里查）
    L2 approved_exclude → 并入该球员 exclude_words（网页确认过的才在 approvals 里）
    L2b approved_non_auto → 全局非签字名单，命中且无强签字证据时降级到折射轨
    L3 校准 factor → 乘到参照价上；人工「档位偏高」再乘一次
    """
    approvals = rules.get("approved_exclude") or {}
    non_auto = [str(w).lower() for w in (rules.get("approved_non_auto") or []) if str(w).strip()]
    penalties = rules.get("tier_penalty") or {}
    applied = {"blocked": 0, "exclude": 0, "calib": 0, "non_auto": 0, "penalty": 0}
    calib = load_calibration_factors()

    # 人工"档位偏高"按球员归拢：{player_cn: {tier_key: factor}}
    pen_by_player: Dict[str, Dict[str, float]] = {}
    for key, slot in penalties.items():
        factor = float(slot.get("factor") or 1.0)
        if factor >= 0.999:
            continue
        pen_by_player.setdefault(slot.get("player_cn", ""), {})[slot.get("tier_key", "")] = factor

    for cfg in watchlist:
        kw = cfg.get("keyword", "")
        extra = approvals.get(kw) or []
        if extra:
            ex = cfg.setdefault("exclude_words", [])
            for w in extra:
                if w not in ex:
                    ex.append(w)
                    applied["exclude"] += 1
        # 校准系数挂到球员上，评估时按 tier_key 取
        if calib:
            cfg["_calib"] = calib
        if non_auto:
            cfg["_non_auto"] = non_auto
        pen = pen_by_player.get(kw) or {}
        if pen:
            cfg["_tier_penalty"] = pen

    applied["blocked"] = len(rules.get("blocked_ids") or [])
    applied["calib"] = len(calib)
    applied["non_auto"] = len(non_auto)
    applied["penalty"] = sum(len(v) for v in pen_by_player.values())
    return applied


def _blocked_ids(rules: Dict[str, Any]) -> set:
    return {str(r.get("item_id") or "") for r in (rules.get("blocked_ids") or [])}


def init_db() -> None:

    """使用原子化 JSON 状态库（彻底规避群晖 Docker 挂载目录 SQLite 文件锁 I/O 报错）"""
    if not os.path.exists(SNAPSHOT_JSON_PATH):
        try:
            _atomic_write_json(SNAPSHOT_JSON_PATH, {})
        except Exception as exc:
            logger.warning(f"初始化状态库异常: {exc}")


def record_and_compare_snapshot(
    item_id: str,
    scan_mode: str,
    title: str,
    current_price: float,
    bid_count: int,
    remaining_minutes: int
) -> Dict[str, Any]:
    """
    记录当前拍品快照，并在 19:20 模式下自动查询当天 17:00 的历史快照，
    计算 2.5 小时内的价格涨幅与新增出价次数，评估暗盘热度。
    """
    today_str = _today_cn()
    now_ts = _now_cn().strftime("%Y-%m-%d %H:%M:%S")

    store: Dict[str, Any] = {}
    if os.path.exists(SNAPSHOT_JSON_PATH):
        try:
            with open(SNAPSHOT_JSON_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            store = {}

    day_bucket = store.setdefault(today_str, {})
    item_bucket = day_bucket.setdefault(str(item_id), {})

    prev_1700 = item_bucket.get("1700")
    prev_row = (prev_1700["price"], prev_1700["bid_count"], prev_1700["scan_timestamp"]) if prev_1700 else None

    item_bucket[scan_mode] = {
        "title": title,
        "price": current_price,
        "bid_count": bid_count,
        "remaining_minutes": remaining_minutes,
        "scan_timestamp": now_ts
    }

    # 仅保留最近 7 天数据，防止文件膨胀
    if len(store) > 7:
        for old_k in sorted(store.keys())[:-7]:
            store.pop(old_k, None)

    try:
        # 原子替换：网页端随时在读这个文件，绝不能让它读到半截 JSON
        _atomic_write_json(SNAPSHOT_JSON_PATH, store)
    except Exception as exc:
        logger.warning(f"写入快照异常: {exc}")

    if prev_row is None:
        if scan_mode == "1920":
            return {
                "has_1700_baseline": False,
                "trend_tag": "🆕 晚间新入窗口",
                "price_delta": 0.0,
                "bid_delta": 0
            }
        return {
            "has_1700_baseline": False,
            "trend_tag": "📡 17:00 首次建档",
            "price_delta": 0.0,
            "bid_delta": 0
        }

    prev_price, prev_bids, _ = prev_row
    price_delta = round(current_price - float(prev_price), 1)
    bid_delta = max(0, int(bid_count) - int(prev_bids))

    if price_delta == 0 and bid_delta == 0:
        trend_tag = f"🧊 冷门死水 (17点至今维持¥{prev_price:.0f}·0人加价·极易偷鸡)"
    elif price_delta <= 25 and bid_delta <= 3:
        trend_tag = f"🔥 温和异动 (17点¥{prev_price:.0f}➔现¥{current_price:.0f}, +{bid_delta}次出价)"
    else:
        trend_tag = f"⚠️ 多人盯盘 (17点¥{prev_price:.0f}➔现¥{current_price:.0f}, 暴涨¥{price_delta:.0f}·严守红线勿追高)"

    return {
        "has_1700_baseline": True,
        "trend_tag": trend_tag,
        "price_delta": price_delta,
        "bid_delta": bid_delta
    }


# ==============================================================================
# 四、 多维卡片属性显微解析引擎 (系列/编号/首尾编/卡签vs贴纸/评级壳/代卖行)
# ==============================================================================

def extract_consignor(title: str, seller_name: str = "") -> str:
    """提取代卖商家名称，用于同店合并运费计算"""
    combined = f"{title} {seller_name}"
    for name in KNOWN_CONSIGNORS:
        if name in combined:
            return f"{name}代卖"
    m = re.search(r"【([^】]{2,8})】", title)
    if m:
        return m.group(1)
    return seller_name if seller_name else "独立卖家"


def parse_serial_details(title: str) -> Tuple[Optional[int], bool, str]:
    """
    精确提取卡片限量编号分母，消除卡号#10、PSA评级分10/10、截标日期、特卡套号等干扰。
    返回: (denom, is_special_serial, serial_badge)
    """
    if not title:
        return None, False, "无编"

    clean_title = title

    # 0.1 清洗评级双 10/10 及分数（避免 PSA10/10, BGS 9.5/10 被当成尾编 10/10）
    clean_title = re.sub(
        r"(?i)\b(?:PSA|BGS|SGC|CGC|CSG|双|评级)\s*(?:10\s*/\s*10|9(?:\.5)?\s*/\s*10|\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*分)",
        " ", clean_title
    )

    # 0.2 清洗截标/拍卖日期（必须包含截标等后缀，防止误杀 2/5 这种低编）
    clean_title = re.sub(
        r"(?<!\d)(?:0?[1-9]|1[0-2])\s*/\s*(?:0?[1-9]|[12]\d|3[01])\s*(?:截标|结标|截单|截|结|截止|晚|截日|号截)(?!\d)",
        " ", clean_title
    )

    # 0.3 清洗卡册号/Checklist编号（核心修复：卡号 #10, No.10, NO:10, 卡号 10）
    clean_title = re.sub(r"(?i)(?:#|no\.?|no:|卡号|编号)\s*[a-z0-9\-]+", " ", clean_title)

    # 0.4 清洗多张/特卡套号说明（如 1/5张, 1/2图, 特卡 1/5）
    clean_title = re.sub(r"(?<!\d)\d{1,2}\s*/\s*\d{1,2}\s*(?:张|图|套|包|盒|连)(?!\d)", " ", clean_title)
    clean_title = re.sub(r"(?i)\b(?:hei|my\s*house|net\s*marvels)\s*\d{1,2}\s*/\s*\d{1,2}\b", " ", clean_title)

    # 0.5 清洗年份与卖家长编码/代卖货号尾巴
    clean_title = re.sub(r"202[0-9]|2[0-9]-2[0-9]|199[0-9]|201[0-9]", " ", clean_title)
    clean_title = re.sub(r"(?<!\d)\d{4,}\s*编\s*[(（][^)）]{0,20}[)）]", " ", clean_title)
    clean_title = re.sub(r"(?<!\d)\d{4,}\s*编", " ", clean_title)
    clean_title = re.sub(r"(?<!\d)\d{5,}(?!\d)", " ", clean_title)
    clean_title = re.sub(r"/(?:大头总|老水母|风知意|伏草|兵库|小幻|拔脚|大鱼|甲子|梦里|蛋卷|炽焰|浩行|寻觅)[^/]{0,10}/\d{1,2}", " ", clean_title)
    clean_title = re.sub(r"/(?:[^\s/]{1,10})/\d{1,2}(?!\d)", " ", clean_title)

    # 1. 带分子的标准分数形式 (01/25, 25/25, 111/150)
    # 负向后行断言扩充：排除月、日、号、分、点、期、连、图、批、截等非限量语境
    frac_matches = re.findall(
        r"(?<!\d)(\d{1,3})\s*/\s*(\d{1,3})(?!\d)(?!\s*[级张个包盒箱套枚月日号分点期连图批截])",
        clean_title
    )
    for num_s, den_s in frac_matches:
        num_v, den_v = int(num_s), int(den_s)
        if not (1 <= den_v <= 499 and 1 <= num_v <= den_v):
            continue
        # 极低分母 (<=5) 防套号二重验证
        if den_v <= 5 and not re.search(r"(?:编|限量|折|签|auto|parallel|serial)", title, re.IGNORECASE):
            continue

        if den_v == 1 and num_v == 1:
            return 1, True, "🔥1/1唯一编"
        if num_v == 1:
            return den_v, True, f"🌟首编(01/{den_v})"
        if num_v == den_v and den_v > 1:
            return den_v, True, f"🌟尾编({num_v}/{den_v})"
        return den_v, False, f"/{den_v}编({num_v}/{den_v})"

    # 2. 独立 1/1 形式
    if (re.search(r"(?<![\d/])1\s*/\s*1(?![\d/])", clean_title)
            or re.search(r"(?<![a-z0-9])1\s*of\s*1(?![a-z0-9])", clean_title, re.IGNORECASE)
            or re.search(r"(?<![a-z0-9])one\s*of\s*one(?![a-z0-9])", clean_title, re.IGNORECASE)):
        return 1, True, "🔥1/1唯一编"

    # 3. 单独分母形式 (/25橙折, /99绿签)
    slash_matches = re.findall(
        r"(?<!\d)/(\d{1,3})(?!\d)(?!\s*[级张个包盒箱套枚月日号分点期连图批截])",
        clean_title
    )
    for den_s in slash_matches:
        den_v = int(den_s)
        if 2 <= den_v <= 499:
            return den_v, False, f"/{den_v}编"

    # 4. 中文明确编数 (25编, 10编低编)
    cn_matches = re.findall(r"(?:限量|编数)?(\d{1,3})\s*编(?!号|辑|组|队)", clean_title)
    for den_s in cn_matches:
        den_v = int(den_s)
        if 1 <= den_v <= 499:
            is_spec = bool(re.search(r"(?:首编|尾编|球衣编|同号)", clean_title))
            badge = f"🌟特殊编(/{den_v}编)" if is_spec else f"/{den_v}编"
            return den_v, is_spec, badge

    return None, False, "无编"
def parse_grading_info(title: str) -> Tuple[float, str]:
    """
    识别评级卡分数并返回估值乘数/附加价值说明：
    - PSA 10 / BGS 9.5 / SGC 10: 裸卡基准 x1.6 + 120元壳费补偿
    - PSA 9 / BGS 9: 裸卡基准 x1.15
    - PSA 8 / BGS 8.5 及以下: 裸卡基准 x0.8 (低分壳预警)
    - Raw 裸卡: x1.0
    """
    upper_t = title.upper()
    if re.search(r"(?:PSA\s*10|BGS\s*9\.5|BGS\s*10|SGC\s*10|CGC\s*10)", upper_t):
        return GRADE10_MULTIPLIER, f"🏆10分/9.5分高评壳(x{GRADE10_MULTIPLIER:g})"
    if re.search(r"(?:PSA\s*9|BGS\s*9|SGC\s*9\.5)", upper_t):
        return 1.15, "🛡️9分评级保真壳"
    if re.search(r"(?:PSA\s*[1-8]|BGS\s*[1-8])", upper_t):
        return 0.80, "⚠️低分评级壳(已自动下调红线)"
    return 1.0, "裸卡(Raw)"


# ------------------------------------------------------------------------------
# 卡品与竞争烈度评估
# ------------------------------------------------------------------------------
# 卡品是接口拿不到的最大盲区：API 只给标题和一张主图，品相全靠卖家自觉。
# 「卡品如图」= 不保证。低价往往正是因为角损、划痕、印刷线。
BAD_CONDITION_WORDS = [
    "角损", "折角", "圆角", "钝角", "划痕", "刮痕", "折痕", "压痕", "白边", "白角",
    "印刷线", "有瑕疵", "小瑕疵", "瑕疵品", "瑕疵", "微瑕", "卡品差", "品相差", "破损", "弯曲",
    "污渍", "压坏", "褪色", "偏印", "错版", "残卡", "压箱底瑕疵",
]
GOOD_CONDITION_WORDS = ["完美品相", "卡品完美", "品相完美", "无瑕疵", "卡品极佳", "卡品很好"]


def assess_condition(title: str) -> Tuple[bool, str]:
    """从标题推断卡品。返回 (是否疑似有瑕疵, 命中的词)。

    先把「无瑕疵 / 没有划痕」这类否定表述抹掉再判，否则「无瑕疵」会被
    「瑕疵」两个字判成残卡，反而把好卡打了 75 折。
    """
    lower = title.lower()
    lower = re.sub(r"(?:无|没有|不带|非|未见)\s*(?:瑕疵|折痕|划痕|白边|角损|折角)", " ", lower)
    for w in BAD_CONDITION_WORDS:
        if w in lower:
            return True, w
    for w in GOOD_CONDITION_WORDS:
        if w in lower:
            return False, "卡品自述优良"
    return False, ""


def assess_competition(out_count: int, bid_count: int, remaining_minutes: int) -> Tuple[str, str]:
    """竞争烈度：区分"真无人问津"与"有人蹲守还没出手"。

    卡淘是竞价制。¥1 起拍 + 0 出价 **不等于**能 ¥1 捡到——
    决定最终成交价的是截标前有多少人盯着，而不是当前价。
    围观数(OutCount)比出价数更早反映这一点，所以两者结合看。
    """
    out_count = max(int(out_count or 0), 0)
    bid_count = max(int(bid_count or 0), 0)

    # 阈值按"修正后的真实现价"重标（2026-10-20）：以前 Price 全是 ¥1，
    # 3~4 次出价就显得很热；现在看到的是真实竞价，8 次才算白热。
    if bid_count >= 8 or out_count >= 120:
        return "hot", "🔥 多人竞价·勿追"
    if bid_count >= 3 or out_count >= 40:
        return "warm", "🌤 已热身·严格按红线出价"
    if bid_count >= 1:
        return "watching", "👀 已有人出价·尚未白热"
    if out_count >= 25:
        return "watching", "👀 有人围观·尚未出手"
    return "cold", "🧊 无人问津·可埋伏"


def evaluate_auction_item_v2(item: Dict[str, Any], player_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    双轨显微评估单条拍品：

    0.   标题必须真的包含该球员（防跨球员误绑）
    0.5  密封产品（整包/整盒/未拆/多卡合售）直接拦掉
    1.   全局黑名单 & 球员专属排除词（英文一律走词边界）
    2.   签字识别 → 走【签字轨 auto_tiers】；无签带编 → 走【折射轨 parallel_tiers】
    3.   卡签/高端系列/首尾编/评级壳 系数修正 + 卡品瑕疵降权
    4.   变现率折算 → 含运总成本 <= 红线 = 真漏(steal)；<= 红线×1.2 = 准漏(near_miss)
    """
    title = item.get("title", "").strip()
    lower_t = title.lower()

    # ---------- 0. 标题必须包含当前球员，否则一律不认 ----------
    # 之前没有这道校验：一条标题里出现英文 amen 之类短词，就会被别的球员"抢走"
    # （真实踩过：科达的卡被标成【阿门】）。
    candidates = ([player_cfg.get("keyword", "")]
                  + list(player_cfg.get("alt_keywords", []))
                  + en_name_candidates(player_cfg.get("en_name", "")))

    hit_pos = -1
    for cand in candidates:
        if len(cand) < 2 or not player_name_matched(cand, lower_t, candidates):
            continue
        pos = lower_t.find(cand.lower())
        if pos >= 0 and (hit_pos < 0 or pos < hit_pos):
            hit_pos = pos
    if hit_pos < 0:
        return None

    # ---------- 0.2 英文名为准（短中文名尤其需要）----------
    # 中文 2~3 字简称的撞名是结构性问题，靠排除词永远追不完：
    #   亚历山大·布勃利克 / 阿罗科达雷（含"科达"）/ 马尔科达格拉卡 / 科达雷尔·帕特森
    # 实测 12 位球员 1686 条中文命中：37.5% 标题根本没有本球员英文名，
    # 其中 422 条已被整包/多人规则拦掉，剩下抽样几乎全是上面这类"别人的卡"，
    # 真正会误伤的纯中文单卡接近零。所以：只要标题出现拉丁字母，
    # 就必须能找到本球员英文名（全名或姓氏），否则不认。
    # ---------- 0.2 英文名全称为准（所有球员，默认强制）----------
    # 中文名的撞名是**结构性**的，靠排除词补不完：
    #   亚历山大  → Alexandre Pantoja（UFC）/ Trey Alexander（NBA 双向）
    #   爱德华兹  → Carsen Edwards（凯尔特人）
    #   贝林厄姆  → 若昂贝兰热（音译前缀）
    # 每补一条排除词，市场就会冒出下一个同姓的人。所以判定标准整体切到英文名：
    #   中文名只负责「检索召回」，不再负责「身份确认」。
    # 标题里连一个拉丁字母都没有的纯中文挂牌，默认也不认——除非该球员在词库里
    # 显式写了 "allow_cn_only": true（适用于卡淘几乎只写中文的冷门球员）。
    allow_cn_only = bool(player_cfg.get("allow_cn_only"))
    has_latin = bool(re.search(r"[a-z]", lower_t))
    if not en_name_verified(player_cfg, lower_t):
        if has_latin or not allow_cn_only:
            logger.info("  [无英文名] 标题里找不到 %s 的英文名 → 跳过：%s",
                        player_cfg.get("en_name", ""), title[:56])
            return None

    # ---------- 0.25 亲属/冲突名硬性排除（针对同姓不同人） ----------
    current_kw = player_cfg.get("keyword", "")
    conflict_list = CONFLICTING_NAMES_MAP.get(current_kw, [])
    for bad_name in conflict_list:
        if word_in_text(bad_name, lower_t):
            logger.info("  [同姓他人拦截] 标题包含同姓冲突人名「%s」→ 排除：%s", bad_name, title[:56])
            return None

    # ---------- 0.3 跨球星串名与蹭热度检测 ----------
    for tok, other_kw in _ALL_EN_TOKENS:
        if other_kw != current_kw and word_in_text(tok, lower_t):
            consignor_match = re.search(r"【([^】]+)】", title)
            if consignor_match:
                prefix = title[:consignor_match.start()].lower()
                suffix = title[consignor_match.end():].lower()
                if (word_in_text(current_kw.lower(), prefix) or word_in_text(player_cfg.get("en_name", "").lower(), prefix))                         and (word_in_text(other_kw.lower(), suffix) or word_in_text(tok, suffix)):
                    logger.info("  [蹭热搜串名] %s 为前缀引流词，主体拍品为 %s → 跳过：%s",
                                current_kw, other_kw, title[:56])
                    return None
            else:
                if not multi_player_hit(title):
                    logger.info("  [多球星冲突] 标题同时出现 %s 与 %s，非双人卡 → 跳过：%s",
                                current_kw, other_kw, title[:56])
                    return None

    # 「姆巴佩奥利塞队友」「哈兰德队友」「约老师副手」—— 全是蹭热度，卡不是他的
    window = lower_t[hit_pos:hit_pos + 14]
    for rw in RIVAL_WORDS:
        if rw in window:
            logger.debug("  [蹭队友] 「%s」→ 跳过：%s", rw, title[:48])
            return None

    # 「同届 文班」「同期 东契奇」这类是卖家在蹭热度做搜索权重，卡的主体不是他。
    # 命中位置落在「同届」之后的一律跳出（线上实测：Whitehead 的球票签字
    # 因为标题写了"同届 文班"，被按文班亚马 ¥3045 红线算出 321 倍）。
    for noise in ("同届", "同期新秀", "同届新秀"):
        no_pos = lower_t.find(noise)
        if no_pos >= 0 and hit_pos > no_pos:
            logger.debug("  [蹭热度] 「%s」之后才出现 %s → 跳过：%s",
                         noise, player_cfg.get("keyword", ""), title[:48])
            return None

    # 一条标题里挤了 3 个及以上监控球星 = 多人卡 / 合集 / 拆卡，
    # 单卡比价模型不适用（线上实测：「博新秀文班亚马 晚安库里 詹姆斯 东契奇 欧文等」）。
    if count_matched_players(lower_t) >= 3:
        logger.debug("  [多人卡] 标题含 3 个以上监控球星 → 跳过：%s", title[:48])
        return None

    # 明确的多人卡语义（双人/双签/合辑…）：卡只有一张，但不是"某个人的卡"
    mp_word = multi_player_hit(title)
    if mp_word:
        logger.debug("  [多人卡] 命中「%s」→ 跳过：%s", mp_word, title[:48])
        return None

    # ---------- 0.5 密封产品拦截 ----------
    # 买整包/整盒时，包里有没有这张卡都不确定，按单卡红线比价毫无意义。
    sealed, sealed_word = is_sealed_product(title)
    if sealed:
        logger.info("  [整包拦截] 命中「%s」→ 跳过：%s", sealed_word, title[:56])
        return None

    # ---------- 1. 黑名单 / 排除词 / 必含词 ----------
    for bad in GLOBAL_BLACKLIST:
        if word_in_text(bad, lower_t):
            return None

    for ex_w in player_cfg.get("exclude_words", []):
        if word_in_text(ex_w, lower_t):
            return None

    must_any = player_cfg.get("must_contain_any", [])
    if must_any and not any(word_in_text(w, lower_t) for w in must_any):
        return None

    # ---------- 2. 识别签字 / 特卡 / 物料 / 编号 ----------
    is_printed_auto = bool(re.search(
        r"(?:印刷|印制|烫金|烫银|压印|激光|机印|盖章|板签|非亲|非实|非真|无亲|不是亲)\s*(?:签|签名|签字|auto)"
        r"|facsimile(?:\s*(?:signature|autograph|auto))?"
        r"|printed\s*(?:signature|autograph|auto)"
        r"|stamp(?:ed)?\s*(?:signature|autograph|auto)",
        lower_t
    ))
    if is_printed_auto:
        logger.debug("  [印签拦截] 识别为印刷/印制签名特卡，绝不进入签字档：%s", title[:48])
        has_auto = False
    else:
        # 系列名消歧：剥离“Topps Signature Class” / “Panini Signature Series”产品名中普卡
        title_for_auto = re.sub(
            r"(?i)\b(?:topps|panini)\s+signature\s+(?:class|series|edition|collection)\b|签名系列|签名版系列",
            " ", lower_t
        )
        clean_for_auto = re.sub(r"(?:无签字?|非签字?|不带签|未签|印刷签|印签)", " ", title_for_auto)
        base_has_auto = bool(re.search(r"(?:签|auto|autograph)", clean_for_auto))
        
        # 针对常见“蹭搜签字”的特卡系列进行实签正向强校验
        INSERT_NON_AUTO_SERIES = [
            "prized footballers", "hard wired", "top of the world", "unstoppable",
            "stratospheric stars", "my house", "net marvels", "emergent", "dominance",
            "inspirational", "all-clear", "instant impact"
        ]
        is_known_insert = any(ins in lower_t for ins in INSERT_NON_AUTO_SERIES)
        has_strong_auto_signal = bool(re.search(
            r"(?:卡签|贴纸签|贴签|球衣签|亲签|实签|on-?card|certified\s*auto|#at-|#ao-)",
            lower_t
        ))
        if is_known_insert and not has_strong_auto_signal:
            has_auto = False
            logger.info("  [特卡蹭搜签字] 命中纯特卡系列且无实签证据，改按折射/特卡估：%s", title[:48])
        else:
            has_auto = base_has_auto
    # 2.1 「签字」是卖家最爱堆的词。线上真实案例：Topps Finest 的
    #     Prizmed Footballers 特卡，标题写「新秀 RC 签字」，实物没有签名 ——
    #     一张几百块的特卡被按「基础无编签字」估到 ¥2414。
    #     名单靠你在网页点「不是签字卡」学出来（L2 采纳后才生效），
    #     但「卡签 / autograph / on-card」是强证据，宁可留着也不降档。
    learned_non_auto_note = ""
    learned_non_auto = player_cfg.get("_non_auto") or []
    if has_auto and learned_non_auto:
        strong_auto = bool(re.search(r"(?:卡签|autograph|on-?card|球衣签|贴纸签)", lower_t))
        hit_word = next((w for w in learned_non_auto if word_in_text(w, lower_t)), "")
        if hit_word and not strong_auto:
            has_auto = False
            learned_non_auto_note = f"🚫特卡非签字({hit_word})"
            logger.info("  [非签字特卡] 「%s」已标注为非签字系列，改按折射轨估：%s",
                        hit_word, title[:48])
    has_case_hit = any(ck in lower_t for ck in CASE_HIT_KEYWORDS)
    is_relic_only = bool(re.search(r"(?:球衣|物料|patch|relic|swatch|jersey)", lower_t)) and not has_auto
    serial_denom, is_special_serial, serial_badge = parse_serial_details(title)

    # 纯物料卡（无签字、非极低编）在 eBay 溢价极低，不值得碰
    if is_relic_only and (serial_denom is None or serial_denom > 25):
        return None

    # 低端纸卡的普通高编（无签字且高于 /25）也不值得碰
    is_low_paper = (any(lp in lower_t for lp in LOW_END_PAPER_SETS)
                    and "chrome" not in lower_t and "prizm" not in lower_t)
    if is_low_paper and not has_auto and (serial_denom is None or serial_denom > 25):
        return None

    # ---------- 2.5 Base 普卡：没有折射特征就不进池子 ----------
    # 折射轨的每一档都给的是「低编平行」的价（p_35_50 = /50 金折），
    # 一张 Prizm 世界杯 Base 平卡被套上去，参照直接从 ¥20 涨到 ¥1015。
    # 线上真实踩过：标题里 `16985编(8/45)` 是卖家货号+批次备注，
    # 被解析成 8/45 编 → 归入「/50 金折低编平行」→ 现价 ¥16 报 17 倍假漏。
    if not has_auto and is_base_plain_card(title):
        logger.info("  [Base 普卡] 无折射特征，不进捡漏池：%s", title[:56])
        return None

    # 折射轨准入：有编号但标题里找不到任何平行特征 → 不知道是哪个颜色。
    # 但**只对 /35 以上的中高编降档**：/10、/25 这种极低编，
    # 价格主要由"只有 10 张/25 张"决定，颜色是次要的，压到最低档属于矫枉过正。
    # Prizm 一个系列二十多种平行，中高编之间价格差十倍，猜"金折"就是凭空造漏。
    parallel_feature_ok = has_parallel_feature(title)
    if not has_auto and serial_denom is not None and serial_denom > 25 \
            and not parallel_feature_ok:
        logger.info("  [平行未明] /%d编 认不出折射颜色，按最低档估：%s",
                    serial_denom, title[:56])

    # ---------- 3. 双轨档位归属 ----------
    auto_tiers = player_cfg.get("auto_tiers") or player_cfg.get("tiers") or {}
    parallel_tiers = player_cfg.get("parallel_tiers") or auto_tiers

    tier_key = None
    if has_auto:
        tier_basis = "auto"
        table = auto_tiers
        if serial_denom is None:
            tier_key = "base_auto"
        elif serial_denom <= 10:
            tier_key = "s_1_10"
        elif serial_denom <= 25:
            tier_key = "s_15_25"
        elif serial_denom <= 50:
            tier_key = "s_35_50"
        elif serial_denom <= 99:
            tier_key = "s_75_99"
        elif serial_denom <= 199:
            tier_key = "s_125_199"
        elif serial_denom <= 399:
            tier_key = "s_250_399"
        else:
            tier_key = "base_auto"
    else:
        # 无签字轨道：必须带编（<=399）或属于 Case Hit 特卡，否则不估
        tier_basis = "parallel"
        table = parallel_tiers
        if serial_denom is not None and serial_denom <= 399:
            if not parallel_feature_ok and serial_denom > 25:
                # 中高编且认不出折射颜色 → 一律按最低档，绝不给"金折"的价
                tier_key = "p_250_399" if "p_250_399" in table else None
            elif serial_denom <= 10:
                tier_key = "p_1_10"
            elif serial_denom <= 25:
                tier_key = "p_15_25"
            elif serial_denom <= 50:
                tier_key = "p_35_50"
            elif serial_denom <= 99:
                tier_key = "p_75_99"
            elif serial_denom <= 199:
                tier_key = "p_125_199"
            else:
                tier_key = "p_250_399"

    tier_cfg = table.get(tier_key) if tier_key else None

    # 平行认不出来时，档位描述也要说实话，不能顶着「/250紫折」的名头
    # 去给一张 /25 的卡报价，否则用户看到的就是自相矛盾的标签。
    if tier_cfg and not has_auto and serial_denom is not None \
            and serial_denom > 25 and not parallel_feature_ok:
        tier_cfg = dict(tier_cfg, desc="/%d编·颜色未明(按最低档估)" % serial_denom)

    # Case Hit 特卡：拿编号档与特卡档里更高的那个
    if has_case_hit and "case_hit" in table:
        ch_cfg = table["case_hit"]
        if not tier_cfg or float(ch_cfg["max_buy_rmb"]) > float(tier_cfg["max_buy_rmb"]):
            tier_cfg = ch_cfg
            tier_key = "case_hit"

    if not tier_cfg:
        return None

    # ---------- 4. 系数修正 ----------
    # 正向溢价（卡签 / 高端系列 / 首尾编）**加算后统一封顶**；
    # 评级壳单独算、最后一起并进正向总额；负向折价（瑕疵 / 兑换板）继续连乘。
    positive_delta = 0.0
    bonus_notes = []

    # (a) 卡签 On-Card（#AT- 代码或标题写明「卡签」）
    #     天然卡签系列（国宝/Flawless/Immaculate/Eminence）不再叠这 15%：
    #     那些系列出厂就带签，标题写「卡签」是在描述规格，不是溢价来源。
    on_card_codes = [c.lower() for c in player_cfg.get("on_card_codes", [])]
    has_on_card = ("卡签" in lower_t or any(code in lower_t for code in on_card_codes))
    natural_auto_set = next((s for s in NATURAL_AUTO_SETS if s in lower_t), "")
    if has_on_card:
        if natural_auto_set:
            bonus_notes.append("卡签(本系列天然带签,不加价)")
        else:
            positive_delta += ON_CARD_BONUS
            bonus_notes.append("卡签+%d%%" % round(ON_CARD_BONUS * 100))

    # (b) 顶级系列（Sapphire / Finest / 国宝…）
    if any(ps in lower_t for ps in PREMIUM_SETS):
        positive_delta += PREMIUM_SET_BONUS
        bonus_notes.append("高端系列+%d%%" % round(PREMIUM_SET_BONUS * 100))

    # (c) 首编/尾编/1-1
    if is_special_serial:
        positive_delta += SPECIAL_SERIAL_BONUS
        bonus_notes.append(f"{serial_badge}+%d%%" % round(SPECIAL_SERIAL_BONUS * 100))

    # (d) 评级壳（PSA/BGS 等，按分数段加权）—— 属于正向，并入同一个封顶
    grade_mult, grade_label = parse_grading_info(title)
    has_slab = abs(grade_mult - 1.0) >= 0.02
    if has_slab:
        bonus_notes.append(grade_label)

    raw_positive = grade_mult * (1.0 + positive_delta)
    positive_cap = POSITIVE_CAP_GRADED if has_slab else POSITIVE_CAP_BARE
    positive = min(raw_positive, positive_cap)
    if raw_positive > positive_cap + 1e-9:
        bonus_notes.append("⛔正向溢价封顶×%.2f(原×%.2f)" % (positive_cap, raw_positive))

    multiplier = positive

    # (e) 卡品瑕疵降权：标题自述角损/白边的，不能按完好卡的价收
    is_flawed, condition_word = assess_condition(title)
    if is_flawed:
        multiplier *= CONDITION_PENALTY
        bonus_notes.append(f"⚠️残卡折价(-{(1 - CONDITION_PENALTY) * 100:.0f}%)")

    # (f) 兑换板降权：不是实物，要等厂商寄卡，价值显著低于同档实物
    is_redemption = bool(REDEMPTION_RE.search(title))
    if is_redemption:
        multiplier *= REDEMPTION_FACTOR
        bonus_notes.append(f"⚠️兑换板非实物(-{(1 - REDEMPTION_FACTOR) * 100:.0f}%)")

    adj_max_buy_rmb = round(float(tier_cfg["max_buy_rmb"]) * multiplier)
    adj_ebay_ref_rmb = round(float(tier_cfg["ebay_ref_rmb"]) * multiplier)

    # (g) 真实成交校准：用截标价反推的档位倍数修正参照价。
    #     优先级：本档位自己的因子 > 跨档位全局因子 > 1.0（不校准）。
    #     本档位的样本直接来自"这张卡这个档"，可信度最高，永远压过全局。
    #     全局因子兜的是「档位样本永远攒不够」这个死结 —— 它衡量的是平台级折价
    #     （卡淘国内成交 vs eBay 美元 comps），本来就该跨档位合并。
    calib_map = player_cfg.get("_calib") or {}
    tier_calib_key = "%s|%s" % (player_cfg.get("keyword", ""), tier_key)
    calib_factor = float(calib_map.get(tier_calib_key)
                         or calib_map.get(CALIB_GLOBAL_KEY) or 1.0)
    if abs(calib_factor - 1.0) >= 0.02:
        adj_ebay_ref_rmb = round(adj_ebay_ref_rmb * calib_factor)
        adj_max_buy_rmb = round(adj_max_buy_rmb * calib_factor)
        scope = "本档" if tier_calib_key in calib_map else "全局"
        bonus_notes.append(f"📊实盘校准×{calib_factor:.2f}({scope})")

    # (h) 人工判定「档位偏高」：每次 -10%，地板 0.60。
    #     和成交校准分开算 —— 成交是市场事实，人工是"你觉得贵"。
    #     人的判断没那么硬，所以只给温和权重；但必须真能咬人，
    #     否则点了"档位偏高"跟没点一样，那才是最伤信任的。
    penalty_map = player_cfg.get("_tier_penalty") or {}
    penalty_factor = float(penalty_map.get(tier_key) or 1.0)
    if abs(penalty_factor - 1.0) >= 0.02:
        adj_ebay_ref_rmb = round(adj_ebay_ref_rmb * penalty_factor)
        adj_max_buy_rmb = round(adj_max_buy_rmb * penalty_factor)
        bonus_notes.append(f"👤人工判高×{penalty_factor:.2f}")

    if learned_non_auto_note:
        bonus_notes.append(learned_non_auto_note)

    current_price = float(item.get("current_price", 0.0))
    # 运费优先用接口真实值（NewPostageMoney），缺失才回退默认首重
    shipping_rmb = float(item.get("shipping_rmb", DEFAULT_SHIPPING_RMB) or DEFAULT_SHIPPING_RMB)
    total_cost_single = current_price + shipping_rmb

    # 竞争烈度：¥1 起拍 + 0 出价 ≠ 能 ¥1 买到，关键看多少人盯着
    comp_level, comp_label = assess_competition(
        item.get("out_count", 0), item.get("bid_count", 0),
        item.get("remaining_minutes", 0))

    # 卖家风险：评价数过低 = 新卖家，卡品纠纷概率高（提示而不拦截）
    seller_review = int(item.get("seller_review", 0) or 0)
    seller_risk = ""
    if seller_review and seller_review < MIN_SELLER_REVIEW:
        seller_risk = f"新卖家({seller_review}条评价)"

    # 变现折算：eBay 成交价要扣平台佣金/国际运费/汇损才是回收价
    realizable_rmb = round(adj_ebay_ref_rmb * REALIZATION_RATE)

    # ---------- 4.5 收益地板（不是所有"低于红线"都值得动手）----------
    # 红线 ¥4 的 /399 平行卡在卡淘 ¥1 起拍时也满足"低于红线"，但赚不到一顿饭。
    profit_at_red = realizable_rmb - adj_max_buy_rmb
    roi_at_red = realizable_rmb / max(adj_max_buy_rmb, 1.0)
    if profit_at_red < MIN_PROFIT_RMB or roi_at_red < MIN_ROI_MULTIPLE:
        logger.debug("  [档位太薄] %s 红线¥%s 期望利¥%s ×%.2f", title[:36],
                     adj_max_buy_rmb, profit_at_red, roi_at_red)
        return None

    # ---------- 5. 真漏 / 准漏 / 起拍观察 / 放弃 ----------
    bid_count_int = int(item.get("bid_count", 0) or 0)
    if bid_count_int <= 0:
        # 0 出价 = 现价只是起拍价，成交价还没被市场验证。
        # 不能算「低于红线的漏」，但红线比起拍价高一大截的仍值得盲拍。
        if profit_at_red < BLIND_MIN_PROFIT_RMB:
            return None
        # 起拍价必须明显低于红线，否则「0 出价」只说明没人要，不是漏
        if current_price > adj_max_buy_rmb * BLIND_MAX_START_RATIO:
            logger.debug("  [起拍过高] ¥%s > 红线¥%s×%.2f，不入观察区",
                         current_price, adj_max_buy_rmb, BLIND_MAX_START_RATIO)
            return None
        opportunity_type = "blind"
    elif total_cost_single <= adj_max_buy_rmb:
        opportunity_type = "steal"
    elif total_cost_single <= adj_max_buy_rmb * NEAR_MISS_TOLERANCE:
        # 差一点点够红线：同店合邮、卖家降价、或轻微还价就能进
        opportunity_type = "near_miss"
    else:
        return None

    safe_snipe_bid = max(1, int(adj_max_buy_rmb - shipping_rmb))
    # 合邮第 2 张起免首重但仍需付续重，故上限 = 红线 - 续重
    combined_snipe_bid = max(1, int(adj_max_buy_rmb - COMBINED_EXTRA_SHIPPING))
    roi_multiple = round(realizable_rmb / max(total_cost_single, 1.0), 1)
    consignor = extract_consignor(title, item.get("seller", ""))

    return {
        "item_id": str(item.get("id", "")),
        "player_cn": player_cfg["keyword"],
        "player_en": player_cfg["en_name"],
        "sport": player_cfg["sport"],
        "title": title,
        "consignor": consignor,
        "tier_key": tier_key,
        "tier_desc": tier_cfg["desc"],
        "tier_basis": tier_basis,          # auto=签字轨 / parallel=折射轨
        "serial_badge": serial_badge,
        "bonus_str": " | ".join(bonus_notes) if bonus_notes else "标准裸卡基准",
        "current_price": current_price,
        "start_price": float(item.get("start_price", 0) or 0),
        "lowest_price": float(item.get("lowest_price", 0) or 0),
        "bid_count": int(item.get("bid_count", 0)),
        "out_count": int(item.get("out_count", 0) or 0),
        "shipping_rmb": shipping_rmb,
        "safe_snipe_bid": safe_snipe_bid,
        "combined_snipe_bid": combined_snipe_bid,
        "max_buy_rmb": adj_max_buy_rmb,
        "ebay_ref_rmb": adj_ebay_ref_rmb,
        "realizable_rmb": realizable_rmb,
        "roi_multiple": roi_multiple,
        "opportunity_type": opportunity_type,
        # 按红线买入还能赚多少 —— 衡量"这个档位值不值得追"，排序与封顶都用它。
        # 不能用「现价买入的倍数」：¥1 起拍时那个数字是几百倍，纯属幻觉。
        "profit_at_red": profit_at_red,
        # 正数 = 当前还差多少才够红线（准漏用），负数/0 = 已经低于红线
        "gap_rmb": round(total_cost_single - adj_max_buy_rmb, 1),
        "competition_level": comp_level,
        "competition_label": comp_label,
        "condition_flag": is_flawed,
        "condition_note": condition_word,
        "redemption_flag": is_redemption,
        "seller_name": item.get("seller", ""),
        "seller_review": seller_review,
        "seller_risk": seller_risk,
        "seller_source": item.get("seller_source", ""),
        "remaining_minutes": int(item.get("remaining_minutes", 0)),
        "end_time_str": item.get("end_time_str", ""),
        "end_time_full": item.get("end_time_full", ""),
        "url": CARDHOBBY_ITEM_URL_TPL.format(item_id=item.get("id", "")),
        "image_url": item.get("image_url", ""),
    }


# ==============================================================================
# 五、 数据抓取模块（卡淘官方搜索接口，纯标准库，无浏览器依赖）
# ==============================================================================
# 说明：本模块已用真实浏览器实测校准，接口/参数/字段映射见文件头常量注释。
#       原 v2.0 的 Playwright 双引擎已整体废弃：真实站点是 ASP.NET MVC + Vue，
#       列表由 /NewCommodity/SearchCommodity 直接以 JSON 返回，无需渲染浏览器。

CF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.cardhobby.com.cn/market/search",
    "Origin": "https://www.cardhobby.com.cn",
}

# 搜索固定筛选：Status=1 即"出售中"。缺了这个参数服务端会返回兜底 HTML 而不是 JSON。
SEARCH_JSON_FILTER = SEARCH_JSON_ACTIVE     # 历史名字，主扫描继续用它（Status=1 出售中）


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_effective_date(raw: str) -> Optional[datetime.datetime]:
    """解析 EffectiveDate（形如 '2026-10-19 23:39:15'，站点本地时间）。
    容器 TZ=Asia/Shanghai，与 datetime.now() 同源，可直接相减。"""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_raw_item_list(raw_list: List[Dict[str, Any]], max_hours: float) -> List[Dict[str, Any]]:
    """把卡淘原始 JSON 行清洗成内部结构，并丢弃超出截标窗口的拍品。

    注意：接口已按 EffectiveTimeStamp 升序返回，超窗条目集中在末尾。
    这里刻意用 continue 而非 break —— 60 条数据遍历成本为零，
    但 break 在服务端夹带置顶/推荐位时会静默漏单，代价是致命的。
    """
    results: List[Dict[str, Any]] = []
    max_minutes = max_hours * 60.0
    now = _now_cn()

    for row in raw_list:
        if not isinstance(row, dict):
            continue

        title = str(row.get("Title") or row.get("title") or "").strip()
        item_id = str(row.get("ID") or row.get("id") or "")
        if not title or not item_id:
            continue

        end_raw = str(row.get("EffectiveDate") or row.get("EndTime") or "")
        dt_end = _parse_effective_date(end_raw)
        if dt_end is None:
            continue

        rem_minutes = (dt_end - now).total_seconds() / 60.0
        if rem_minutes <= 0:
            continue
        if rem_minutes > max_minutes:
            continue

        # 真实运费优先；为空时回退默认首重
        postage = _to_float(row.get("NewPostageMoney"), -1.0)
        if postage < 0:
            postage = DEFAULT_SHIPPING_RMB

        # ------------------------------------------------------------------
        # 价格字段（2026-10-20 实测纠正，本项目最严重的一个 bug）
        #
        #   Price       = 起拍价。卡淘绝大多数拍品都是 1.00，且**全程不变**。
        #   LowestPrice = 当前最低可成交价（有人出价后即为当前价）。
        #
        # 旧代码拿 Price 当现价，等于把「¥1 起拍」当成「¥1 成交」：
        # 每张卡都低于红线，68 人跑一轮刷出 985 条「真漏」，
        # 而用户看到的卡价永远是 ¥1，与站上完全不同步。
        # ------------------------------------------------------------------
        start_price = _to_float(row.get("Price"))
        real_price = _to_float(row.get("LowestPrice"))
        if real_price <= 0:
            real_price = start_price          # 一口价商品可能没有 LowestPrice

        results.append({
            "id": item_id,
            "title": title,
            "current_price": real_price,      # 当前价（真实可成交价）
            "start_price": start_price,       # 起拍价，仅用于展示
            "lowest_price": start_price,      # 兼容旧字段：模板里的「起拍」
            "bid_count": _to_int(row.get("PriceCount")),
            # 围观人数：比出价数更早反映"是否有人在等这张卡"
            "out_count": _to_int(row.get("OutCount")),
            "seller": str(row.get("SellRealName") or ""),
            "seller_id": _to_int(row.get("SellMemberID")),
            # 卖家累计评价数：低值 = 新卖家，卡品纠纷概率高
            "seller_review": _to_int(row.get("ReviewCount")),
            # 卖家所在地：CN / 海外，影响运费与到货周期
            "seller_source": str(row.get("SellSource") or ""),
            "shipping_rmb": postage,
            "remaining_minutes": int(rem_minutes),
            "end_time_str": dt_end.strftime("%H:%M"),
            "end_time_full": end_raw,
            "image_url": str(row.get("TitImg") or ""),
            "by_way": _to_int(row.get("ByWay")),
            "guarantee": _to_int(row.get("IsGuarantee")),
        })

    return results


def parse_sold_item_list(raw_list: List[Dict[str, Any]],
                         max_days: float = VAULT_QUOTE_SOLD_MAX_DAYS
                         ) -> List[Dict[str, Any]]:
    """把「售出的宝贝」原始行清洗成**成交样本**。

    与 parse_raw_item_list 的差别是根本性的：
      · 在拍列表丢掉 EffectiveDate 已过去的；成交列表**只保留**已过去的。
      · 在拍用 current_price（当前出价位置，随时会变）；成交用 LowestPrice（最终成交价，事实）。
      · 在拍看"剩余多久截标"；成交看"多久之前成交"（时间窗 max_days）。

    流拍（PriceCount == 0）必须剔除：无人出价的"结标"不是"按这个价成交了"，
    放进中位数等于凭空造出一个极低的行情。
    """
    results: List[Dict[str, Any]] = []
    now = _now_cn()
    oldest_ts = now.timestamp() - max_days * 86400.0

    for row in raw_list:
        if not isinstance(row, dict):
            continue

        title = str(row.get("Title") or row.get("title") or "").strip()
        item_id = str(row.get("ID") or row.get("id") or "")
        if not title or not item_id:
            continue

        # 防御：万一本接口哪天真支持了"混合返回"，也不能把在售的当成交
        if _to_int(row.get("Status")) != -2:
            continue

        bid_count = _to_int(row.get("PriceCount"))
        if bid_count <= 0:
            continue                              # 流拍，不是成交

        final_price = _to_float(row.get("LowestPrice"))
        if final_price <= 0:
            continue

        # 1 元起拍、1 次出价 → 只有起拍价本身，没有任何人竞争。
        # 这种"成交"反映的是"没人要"，不是"这张卡值这个价"；
        # 放进中位数会把整张卡打成 ¥1（实测埃森格 Instinct 特卡）。
        start_price = _to_float(row.get("Price"))
        if (bid_count <= 1 and final_price <= VAULT_QUOTE_JUNK_RMB
                and final_price <= max(start_price, 1.0)):
            continue

        dt_end = _parse_effective_date(str(row.get("EffectiveDate") or ""))
        if dt_end is None:
            continue
        closed_ts = dt_end.timestamp()
        if closed_ts > now.timestamp() + 3600:
            continue                              # 时间在未来 = 脏数据
        if closed_ts < oldest_ts:
            continue                              # 超出时间窗

        results.append({
            "id": item_id,
            "title": title,
            "sold_price": final_price,            # 最终成交价（行情本尊）
            "bid_count": bid_count,
            "out_count": _to_int(row.get("OutCount")),
            "closed_ts": int(closed_ts),
            "closed_str": dt_end.strftime("%Y-%m-%d"),
            "seller": str(row.get("SellRealName") or ""),
            "image_url": str(row.get("TitImg") or ""),
            "by_way": _to_int(row.get("ByWay")),
        })

    results.sort(key=lambda s: s["closed_ts"], reverse=True)   # 最近成交优先
    return results


def _grade_bucket(text: str) -> str:
    """把一条标题/描述归到评级档：gem(10分/9.5) / nine(9分) / low(低分壳) / raw(裸卡)。

    复用 parse_grading_info 的正则，不另起一套 —— 那套已经被 313 笔真实成交校准过。
    """
    mult, _desc = parse_grading_info(str(text or ""))
    if mult >= 1.45:
        return "gem"
    if abs(mult - 1.15) < 0.01:
        return "nine"
    if abs(mult - 0.80) < 0.01:
        return "low"
    return "raw"


def _card_grade_bucket(card: Dict[str, Any]) -> str:
    """判断**用户自己那张卡**是裸卡还是有壳。

    档案库里没有独立的评级字段，PSA/BGS 信息通常写在 card_type 或备注里
    （例："Rookie Card #136 (PSA 10 GEM MINT)"）。拼起来一起判。
    """
    blob = " ".join(str(card.get(k) or "") for k in
                    ("card_type", "serial", "year_set", "notes"))
    return _grade_bucket(blob)


def _request_search_page(keyword: str, page_index: int,
                         sold: bool = False) -> List[Dict[str, Any]]:
    """请求单页搜索结果，返回 data.PagedMarketItemList。失败抛异常。

    `sold=False`（默认）：在售/在拍，最快结束升序 —— 主扫描用这个。
    `sold=True`：**已成交**，最近成交降序 —— 库存行情用这个。

    两者是同一个端点，只差 searchJson 里的 Status：
      1 = 出售中；-2 = 售出的宝贝（网页 tab 的真实调用见 SeachByStatus('Status',-2)）
    实测 Status 填 0/2/3/4… 全都返回 0 条，只有 1 和 -2 有效。
    """
    query = urllib.parse.urlencode({
        "userId": "",
        "pageIndex": page_index,
        "pageSize": CARDHOBBY_PAGE_SIZE,
        "searchKey": keyword,
        "searchJson": SEARCH_JSON_SOLD if sold else SEARCH_JSON_FILTER,
        "sort": "EffectiveTimeStamp",
        # 已成交要"最近成交优先"；在拍要"最快结束优先"。方向反了等于拿到最老的数据。
        "sortType": "desc" if sold else "asc",
    })
    url = "%s?%s" % (CARDHOBBY_SEARCH_API, query)

    headers = dict(CF_HEADERS)
    if CARDHOBBY_COOKIE:
        headers["Cookie"] = CARDHOBBY_COOKIE
    if CARDHOBBY_TOKEN:
        headers["Authorization"] = CARDHOBBY_TOKEN

    req = urllib.request.Request(url, headers=headers, method="GET")
    # 网络抖动 / 服务端偶发 5xx 不值得让整个关键词作废：
    # 一个关键词挂了等于这个球员这一轮静默消失，而用户看到的只是"今天没漏"。
    # 退避 1.5s、3s，两次都失败才放弃（总共最多约 4.5 秒，68 人全挂才是 5 分钟量级）。
    body = ""
    last_exc: Optional[Exception] = None
    for attempt, backoff in enumerate((0.0, 1.5, 3.0)):
        if backoff:
            time.sleep(backoff)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            last_exc = None
            break
        except Exception as exc:                  # noqa: BLE001
            last_exc = exc
            logger.warning("  [接口] 关键词 '%s' 第 %d 页第 %d 次请求失败: %s",
                           keyword, page_index, attempt + 1, exc)
    if last_exc is not None:
        raise last_exc

    try:
        payload = json.loads(body)
    except ValueError:
        # 极少数情况下（参数被服务端判定异常）会返回 HTML 兜底页
        logger.warning(
            "  [接口] 关键词 '%s' 第 %d 页返回非 JSON（%d 字节，疑似兜底 HTML）",
            keyword, page_index, len(body))
        return []

    root = payload.get("data") or payload.get("Data") or {}
    rows = root.get("PagedMarketItemList") or []
    return rows if isinstance(rows, list) else []


def fetch_via_http(keyword: str, max_hours: float, max_pages: int = 3) -> Tuple[List[Dict[str, Any]], int]:
    """按"最快结束"排序翻页抓取，直到本页出现超窗条目或翻页到上限。

    服务端按截标时间升序返回，因此第 1 页即最早结束的一批。
    只有当整页 60 条都仍在截标窗口内时，才需要继续翻页。

    返回 (窗口内拍品, 接口原始行数合计)。
    原始行数是判断"接口是否还活着"的唯一硬指标：
    窗口内 0 条可能是单纯没货，而原始 0 行意味着接口失效。
    """
    max_minutes = max_hours * 60.0
    collected: List[Dict[str, Any]] = []
    seen = set()
    raw_total = 0

    for page in range(1, max_pages + 1):
        try:
            rows = _request_search_page(keyword, page)
        except Exception as exc:
            logger.warning("  [接口] 关键词 '%s' 第 %d 页请求失败: %s", keyword, page, exc)
            break

        if not rows:
            break

        raw_total += len(rows)
        page_items = parse_raw_item_list(rows, max_hours=max_hours)
        for it in page_items:
            if it["id"] not in seen:
                seen.add(it["id"])
                collected.append(it)

        # 满页 + 本页最后一条仍在窗口内 → 可能还有窗口内拍品，继续翻
        if len(rows) < CARDHOBBY_PAGE_SIZE:
            break
        last_dt = _parse_effective_date(str(rows[-1].get("EffectiveDate") or ""))
        if last_dt is None:
            break
        if (last_dt - _now_cn()).total_seconds() / 60.0 > max_minutes:
            break

        time.sleep(REQUEST_DELAY_SEC)

    return collected, raw_total


def fetch_player_auctions(player_cfg: Dict[str, Any], max_hours: float) -> Tuple[List[Dict[str, Any]], int]:
    """检索「英文全名 + 中文主关键词 + 备选中文名」，按 id 去重合并。

    英文全名放第一位，是因为身份判定已经以英文名为准：
      - 英文名能搜到卡淘上只写英文、根本不带中文译名的挂牌
      - 中文名继续用来兜住"只写中文"的卖家，召回不塌
    两者结果合并去重，判定统一交给 en_name_verified()。

    返回 (去重后拍品, 接口原始行数合计)。
    """
    keywords = []
    en_name = (player_cfg.get("en_name") or "").strip()
    if en_name:
        keywords.append(en_name)
    keywords.append(player_cfg.get("keyword", ""))
    keywords.extend(player_cfg.get("alt_keywords") or [])
    # 去重且保持顺序（英文名可能与某个 alt 重复）
    uniq_kw: List[str] = []
    seen_kw = set()
    for k in keywords:
        kl = k.strip().lower()
        if not kl or kl in seen_kw:
            continue
        seen_kw.add(kl)
        uniq_kw.append(k.strip())
    keywords = uniq_kw

    all_items: List[Dict[str, Any]] = []
    seen = set()
    raw_total = 0

    for kw in keywords:
        items, raw = fetch_via_http(kw, max_hours=max_hours)
        raw_total += raw
        for it in items:
            if it["id"] not in seen:
                seen.add(it["id"])
                all_items.append(it)
        time.sleep(REQUEST_DELAY_SEC)

    return all_items, raw_total

# ==============================================================================
# 六、 同代卖商家合并运费赋能 & 三通道消息推送 (PushPlus + Bark + 企业微信)
# ==============================================================================

def enrich_same_seller_shipping(steals: List[Dict[str, Any]]) -> None:
    """
    统计同一代卖商家命中的拍品数量：
    若同一商家（如拔脚代卖、战斧代卖）今晚命中 >= 2 张漏卡，
    自动标记【同店可合邮】，提示第 2 张起安全埋价上限可 +¥12！
    """
    seller_counts: Dict[str, int] = {}
    for s in steals:
        c = s["consignor"]
        if c != "独立卖家":
            seller_counts[c] = seller_counts.get(c, 0) + 1

    for s in steals:
        cnt = seller_counts.get(s["consignor"], 1)
        s["same_seller_count"] = cnt
        if cnt >= 2:
            s["shipping_tip"] = (
                f"📦 <b>【{s['consignor']}】今晚共命中 {cnt} 张漏！</b>"
                f"合并发货第2张起免运费，最高埋价可放宽至 <b>¥{s['combined_snipe_bid']}</b>"
            )
        else:
            s["shipping_tip"] = f"📦 卖家：{s['consignor']} (已按单张首重运费 ¥{s['shipping_rmb']:.0f} 扣减算价)"


def build_pushplus_html(steals: List[Dict[str, Any]], mode_label: str, max_hours: float) -> str:
    now_str = _now_cn().strftime("%Y-%m-%d %H:%M")
    if not steals:
        return (
            f"<h3>📡 {mode_label} ({now_str})</h3>"
            f"<p>已扫描全部重点球星（未来 {max_hours} 小时内截标），"
            f"暂未发现低于抄底红线的拍品。</p>"
        )

    cards_html = []
    for idx, s in enumerate(steals, 1):
        rem_h = s["remaining_minutes"] // 60
        rem_m = s["remaining_minutes"] % 60
        rem_str = f"{rem_h}h{rem_m}m" if rem_h > 0 else f"<b style='color:#dc2626;'>{rem_m}分钟</b>"

        # 卡品瑕疵警示条（有则显示）
        cond_bar = ""
        if s.get("condition_flag"):
            cond_bar = (
                f"<div style='background:#fef2f2; border-left:3px solid #dc2626; "
                f"padding:5px 8px; margin:6px 0; font-size:12px; color:#b91c1c;'>"
                f"⚠️ <b>卡品警示</b>：标题含「{s.get('condition_note', '')}」，已按残卡折价</div>"
            )
        if s.get("redemption_flag"):
            cond_bar += (
                f"<div style='background:#fffbeb; border-left:3px solid #d97706; "
                f"padding:5px 8px; margin:6px 0; font-size:12px; color:#92400e;'>"
                f"⚠️ <b>兑换板非实物</b>：要寄回厂商换卡，参照价已按 "
                f"{int(REDEMPTION_FACTOR * 100)}% 折算，且存在换不到的风险</div>"
            )
        # 卖家风险提示
        seller_bar = ""
        if s.get("seller_risk"):
            seller_bar = (
                f"<div style='font-size:11px; color:#b45309; margin-top:4px;'>"
                f"⚠️ {s['seller_risk']}，建议看清实拍图再出价</div>"
            )
        # 缩略图（卡淘图床可能有防盗链，故加 no-referrer）
        img_html = ""
        if s.get("image_url"):
            img_html = (
                f"<img src=\"{s['image_url']}\" referrerpolicy=\"no-referrer\" "
                f"style=\"width:76px;height:106px;object-fit:cover;border-radius:5px;"
                f"border:1px solid #e2e8f0;flex-shrink:0;\" alt=\"card\">"
            )

        cards_html.append(f"""
        <div style="border:1px solid #cbd5e1; border-radius:10px; padding:12px; margin-bottom:12px; background:#ffffff; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <div style="font-size:15px; font-weight:bold; color:#0f172a; border-bottom:1px dashed #e2e8f0; padding-bottom:6px;">
                #{idx} [{s['sport']}] {s['player_cn']} ({s['player_en']}) · <span style="color:#dc2626;">{s['tier_desc']}</span>
            </div>
            <div style="display:flex; gap:10px; margin:8px 0;">
                {img_html}
                <div style="font-size:13px; color:#334155; flex:1;">
                    <b>拍品标题：</b>{s['title']}<br>
                    <b>属性加成：</b><span style="color:#0284c7;">{s['serial_badge']} | {s['bonus_str']}</span><br>
                    <b>竞争状态：</b>{s.get('competition_label', '')} <span style="color:#94a3b8;">(围观 {s.get('out_count', 0)} · 出价 {s['bid_count']})</span>
                </div>
            </div>
            {cond_bar}
            <div style="font-size:13px; line-height:1.7; background:#f8fafc; padding:8px; border-radius:6px; color:#1e293b;">
                💰 <b>当前拍价：</b><span style="color:#16a34a; font-size:16px; font-weight:bold;">¥{s['current_price']:.0f}</span> ({s['bid_count']}次出价 · 起拍¥{s.get('lowest_price', 0):.0f})<br>
                🎯 <b style="color:#d97706; font-size:14px;">建议单张安全埋价：≤ ¥{s['safe_snipe_bid']}</b> (含运红线 ¥{s['max_buy_rmb']})<br>
                🌎 <b>eBay实售：</b>¥{s['ebay_ref_rmb']} → 扣佣金运费后 <b>可变现 ¥{s.get('realizable_rmb', s['ebay_ref_rmb'])}</b><br>
                📈 <b>实际回收倍数：</b><b style="color:#0284c7;">{s['roi_multiple']}x</b> (已含 {int(REALIZATION_RATE * 100)}% 变现率折算)<br>
                📊 <b>盘口追踪：</b>{s.get('trend_tag', '首次扫描')}<br>
                {s.get('shipping_tip', '')}
                {seller_bar}<br>
                ⏰ <b>截标时间：</b>今晚 <b>{s['end_time_str']}</b> (剩余 {rem_str})
            </div>
            <div style="margin-top:8px; text-align:right;">
                <a href="{s['url']}" style="display:inline-block; padding:6px 14px; background:#2563eb; color:#ffffff; text-decoration:none; border-radius:6px; font-size:12px; font-weight:bold;">👉 打开卡淘拍品页</a>
            </div>
        </div>
        """)

    return (
        f"<div style='font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f1f5f9; padding:10px; border-radius:8px;'>"
        f"<h3 style='color:#1e3a8a; margin:0 0 4px 0;'>🚨 {mode_label}</h3>"
        f"<p style='font-size:12px; color:#475569; margin:0 0 10px 0;'>扫描时间：{now_str} | 截标窗口：≤ {max_hours}h | 命中真漏：<b>{len(steals)}</b> 张</p>"
        f"{''.join(cards_html)}"
        f"</div>"
    )


def send_notifications(steals: List[Dict[str, Any]], mode_label: str, max_hours: float) -> None:
    # 空结果默认静默（PUSH_WHEN_EMPTY=1 可打开），避免每天两条"暂无漏"噪音
    if not steals and not PUSH_WHEN_EMPTY:
        logger.info("本轮 0 命中，按配置跳过推送（如需推送请设 PUSH_WHEN_EMPTY=1）。")
        return

    title = f"【卡淘捡漏雷达】{mode_label}：命中 {len(steals)} 张跨市场高差价目标！"
    # PushPlus 正文硬上限 2 万字，超了直接 code 999 整条丢失（线上真踩过）。
    # 固定"最多几张"兜不住——单卡 HTML 长度随标题浮动很大，所以按字符预算逐张试排，
    # 能塞几张塞几张，剩下的引导去网页（网页才是完整名单）。
    top_hits = steals[:PUSH_MAX_HITS]
    html_body = ""
    shown = 0
    for n in range(len(top_hits), 0, -1):
        cand = build_pushplus_html(top_hits[:n], mode_label, max_hours)
        rest = len(steals) - n
        if rest > 0:
            cand += (
                f"<p style='color:#64748b;font-size:12px;'>另有 {rest} 张未在此列出，"
                f"点开 Card Vault 的「🎯 捡漏雷达」看完整名单。</p>"
            )
        if len(cand) <= PUSH_MAX_CHARS:
            html_body, shown = cand, n
            break
    if not shown:
        # 极端情况（单张卡标题巨长）也要保证能推出去，退化成纯文本摘要
        lines = [
            f"{i}. {s['player_cn']} ¥{s['current_price']:.0f} 埋≤¥{s['safe_snipe_bid']} {s['end_time_str']}"
            for i, s in enumerate(steals[:PUSH_MAX_HITS], 1)
        ]
        html_body = (
            f"<h3>🚨 {mode_label}</h3><p>命中 {len(steals)} 张，标题过长已转为摘要：</p><p>"
            + "<br>".join(lines) + "</p>"
        )
        shown = len(steals[:PUSH_MAX_HITS])
    if shown < len(steals):
        logger.info("  [推送裁剪] 命中 %d 张，正文只列 %d 张（字符预算 %d），其余去网页看",
                    len(steals), shown, PUSH_MAX_CHARS)

    # 1. PushPlus 微信图文卡片推送
    if PUSHPLUS_TOKEN:
        try:
            payload = json.dumps({
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": html_body,
                "template": "html"
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://www.pushplus.plus/send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            # PushPlus 超限/欠费这类业务失败仍回 HTTP 200，只在 body 里给 code != 200。
            # 以前一律记"推送成功"，等于把故障藏起来——必须按 code 判。
            compact = raw.replace(" ", "")
            if '"code":200' in compact:
                logger.info("PushPlus 推送成功: %s", raw[:200])
            else:
                logger.error("PushPlus 推送被拒（正文 %d 字符）: %s", len(html_body), raw[:300])
        except Exception as exc:
            logger.error(f"PushPlus 推送异常: {exc}")
    else:
        logger.info("未检测到 PUSHPLUS_TOKEN，跳过 PushPlus 推送。")

    # 2. Bark iPhone 锁屏强提醒
    if BARK_URL and steals:
        try:
            summary = " / ".join([f"{s['player_cn']}¥{s['current_price']:.0f}(埋≤¥{s['safe_snipe_bid']})" for s in steals[:4]])
            bark_msg = f"命中{len(steals)}张真漏：{summary}。请点开微信PushPlus查看详情！"
            url = f"{BARK_URL.rstrip('/')}/{urllib.parse.quote(title)}/{urllib.parse.quote(bark_msg)}?sound=calypso&group=CardRadar"
            with urllib.request.urlopen(url, timeout=10) as resp:
                logger.info(f"Bark 推送成功: {resp.read().decode('utf-8', errors='ignore')}")
        except Exception as exc:
            logger.error(f"Bark 推送异常: {exc}")

    # 3. 企业微信群机器人 Webhook (可选)
    if WECHAT_WORK_WEBHOOK and steals:
        try:
            md_lines = [f"### 🚨 {title}"]
            for idx, s in enumerate(steals[:8], 1):
                md_lines.append(
                    f"**{idx}. [{s['sport']}] {s['player_cn']} ({s['tier_desc']})**\n"
                    f"> 标题：{s['title']}\n"
                    f"> 当前价：**¥{s['current_price']:.0f}** | 建议埋价：**≤¥{s['safe_snipe_bid']}** (合邮≤¥{s['combined_snipe_bid']}) | eBay基准：¥{s['ebay_ref_rmb']}\n"
                    f"> 状态：{s.get('trend_tag', '')} | [点击打开卡淘]({s['url']})"
                )
            wx_payload = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": "\n\n".join(md_lines)}
            }).encode("utf-8")
            req = urllib.request.Request(
                WECHAT_WORK_WEBHOOK,
                data=wx_payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"企业微信推送成功: {resp.read().decode('utf-8', errors='ignore')}")
        except Exception as exc:
            logger.error(f"企业微信推送异常: {exc}")


def _send_plain(title: str, html_body: str, bark_msg: str = "") -> None:
    """轻量推送通道：心跳与告警共用，不发战报卡片。"""
    if PUSHPLUS_TOKEN:
        try:
            payload = json.dumps({
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": html_body,
                "template": "html",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://www.pushplus.plus/send", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=12) as resp:
                logger.info(f"[心跳] PushPlus 推送成功: {resp.read().decode('utf-8', errors='ignore')}")
        except Exception as exc:
            logger.error(f"[心跳] PushPlus 推送异常: {exc}")
    else:
        logger.info("[心跳] 未配置 PUSHPLUS_TOKEN，跳过。")

    if BARK_URL and bark_msg:
        try:
            url = (f"{BARK_URL.rstrip('/')}/{urllib.parse.quote(title)}"
                   f"/{urllib.parse.quote(bark_msg)}?sound=calypso&group=CardRadar")
            with urllib.request.urlopen(url, timeout=10) as resp:
                logger.info(f"[心跳] Bark 推送成功: {resp.read().decode('utf-8', errors='ignore')}")
        except Exception as exc:
            logger.error(f"[心跳] Bark 推送异常: {exc}")


def low_recall_reason(stats: Dict[str, Any]) -> str:
    """判断这一轮是不是「静默塌方」，返回原因（空串 = 正常）。

    为什么心跳要单独看这个：raw_total == 0 才触发告警太晚了。
    接口被限流时通常不是全 0，而是**大幅缩水**（68 个关键词只剩零星几条），
    此时雷达照样"跑完了、没报错、没命中"，手机上与你今天真的没漏长得一模一样。
    所以给一个下限：整轮原始召回 < 200 行，或过半球员返回 0 条，就要在标题上喊出来。
    """
    players = int(stats.get("players") or 0)
    raw = int(stats.get("raw") or 0)
    zero = int(stats.get("players_zero") or 0)
    if players > 0 and zero * 2 > players:
        return "%d/%d 个关键词返回 0 条" % (zero, players)
    if raw < LOW_RECALL_THRESHOLD:
        return "整轮原始召回仅 %d 条（下限 %d）" % (raw, LOW_RECALL_THRESHOLD)
    return ""


def _watchlist_warning_html(stats: Dict[str, Any]) -> str:
    """词库加载异常的统一告警片段。

    单独抽出来是因为这个错误的危害被严重低估：
    词库解析失败时雷达会**静默回退到内置名单**，照常扫描、照常出结果，
    但用的是内置球员和内置红线 —— 用户以为在跑自己的策略，其实不是。
    """
    problems = stats.get("watchlist_problems") or []
    if not problems:
        return ""
    items = "<br>".join("- " + str(p) for p in problems[:5])
    more = ("<br>- 等共 %d 条" % len(problems)) if len(problems) > 5 else ""
    return (
        f"<p style='font-size:12px; color:#b91c1c; margin:10px 0 0 0; "
        f"background:#fef2f2; border:1px solid #fca5a5; border-radius:8px; padding:10px;'>"
        f"🚨 <b>词库异常（{len(problems)} 条）：本轮扫描可能没有使用你配置的名单</b><br>"
        f"{items}{more}</p>"
    )


def send_heartbeat(stats: Dict[str, Any]) -> None:
    """每日心跳：证明雷达还活着，且顺手告诉你今天扫了多少货、为什么没命中。

    没有这条，"今天没漏"和"接口挂了"在手机上长得一模一样。
    """
    now_str = _now_cn().strftime("%Y-%m-%d %H:%M")
    low = low_recall_reason(stats)
    title = "【卡淘捡漏雷达】今日无漏 · 系统正常"
    if low:
        title = "【⚠️ 预警：原始召回量异常偏低，疑似限流或接口变更】" + low
    html_body = (
        f"<div style='font-family:-apple-system,sans-serif; background:#f0fdf4; "
        f"border:1px solid #86efac; border-radius:10px; padding:14px;'>"
        f"<h3 style='color:#15803d; margin:0 0 8px 0;'>✅ 雷达运行正常</h3>"
        f"<p style='font-size:13px; color:#334155; line-height:1.9; margin:0;'>"
        f"扫描对象：<b>{stats['players']}</b> 人<br>"
        f"接口返回：<b>{stats['raw']}</b> 条原始拍品<br>"
        f"窗口内可评估：<b>{stats['window']}</b> 条<br>"
        f"返回 0 条的关键词：<b>{stats.get('players_zero', 0)}</b> 个<br>"
        + (f"因封顶未写入流水：<b style='color:#b45309;'>{stats['dropped_by_cap']}</b> 条"
           f"（想放宽调 MAX_HITS_PER_PLAYER / MAX_STEAL_TOTAL）<br>"
           if stats.get("dropped_by_cap") else "")
        + f"命中真漏：<b style='color:#15803d;'>0</b> 张<br>"
        f"异常球员：<b>{stats['errors']}</b> 个<br>"
        f"<span style='color:#64748b;'>下次扫描：明日 17:00（摸底）/ 19:20（终极名单）</span>"
        f"</p>"
        + (f"<p style='font-size:12px; color:#b45309; margin:8px 0 0 0;'>"
           f"⚠️ 原始召回量异常偏低：{low}。这更像是限流/接口变更，而不是今天真没货。"
           f"</p>" if low else "")
        + _watchlist_warning_html(stats)
        + f"<p style='font-size:11px; color:#94a3b8; margin:8px 0 0 0;'>"
          f"本消息为每日存活心跳，仅代表抓取与算法链路正常，不代表今日无漏可捡。"
          f"<br>生成时间：{now_str}</p></div>"
    )
    bark_msg = (f"⚠️ 召回异常：{low}" if low
                else f"雷达正常：扫{stats['players']}人/{stats['raw']}条，今日无漏")
    _send_plain(title, html_body, bark_msg=bark_msg)


def send_api_alert(stats: Dict[str, Any]) -> None:
    """接口全面失效告警：所有关键词都拿不到任何原始数据。"""
    now_str = _now_cn().strftime("%Y-%m-%d %H:%M")
    title = "⚠️ 卡淘雷达疑似失效，请立即检查"
    html_body = (
        f"<div style='font-family:-apple-system,sans-serif; background:#fef2f2; "
        f"border:1px solid #fca5a5; border-radius:10px; padding:14px;'>"
        f"<h3 style='color:#b91c1c; margin:0 0 8px 0;'>🚨 抓取链路异常</h3>"
        f"<p style='font-size:13px; color:#334155; line-height:1.9; margin:0;'>"
        f"本轮扫描 <b>{stats['players']}</b> 个关键词，"
        f"接口原始返回 <b style='color:#b91c1c;'>{stats['raw']}</b> 条。<br>"
        f"这通常意味着：接口改版 / IP 被限流 / 需要登录态 / 站点故障。<br>"
        f"<b>雷达现在处于空转状态，不会产出任何结果。</b>"
        f"</p>"
        + _watchlist_warning_html(stats)
        + f"<p style='font-size:12px; color:#475569; margin:8px 0 0 0;'>"
        f"排查命令：<code>docker compose run --rm cardhobby-radar "
        f"python3 /app/cardhobby_sniper_radar.py --mode probe</code>"
        f"<br>检测时间：{now_str}</p></div>"
    )
    _send_plain(title, html_body, bark_msg="⚠️ 卡淘雷达抓取链路异常，已空转，请检查")


# ==============================================================================
# 七、 主扫描调度与自检演练入口
# ==============================================================================

def write_scan_status(stats: Dict[str, Any], mode_label: str) -> None:
    """把最近一轮的扫描体检结果落成 radar_status.json，供 Card Vault 网页端展示。

    网页端和手机推送面临同一个问题：界面上"没有待抢的卡"到底是今天真没漏，
    还是雷达根本没跑？落一份状态文件，网页就能直接显示"最近扫描 19:21 · 接口 949 行 ·
    命中 0"，一眼分清「安静」和「坏了」。
    """
    now = _now_cn()
    payload = {
        "scan_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "scan_ts": int(now.timestamp()),
        "scan_date": now.strftime("%Y-%m-%d"),
        "mode": stats.get("mode", ""),
        "mode_label": mode_label,
        "players": stats.get("players", 0),
        "raw": stats.get("raw", 0),
        "window": stats.get("window", 0),
        "hits": stats.get("hits", 0),
        # 「准漏」与「整包拦截」是 v3 新增的两条口径，网页端要能看到，
        # 否则用户看到命中变少会以为雷达坏了，而不是"过滤变准了"。
        "near_miss": stats.get("near_miss", 0),
        "blind": stats.get("blind", 0),
        "candidates": stats.get("candidates", 0),
        "sealed_blocked": stats.get("sealed_blocked", 0),
        "max_hours": stats.get("max_hours", 0),
        "errors": stats.get("errors", 0),
        # 全关键词 0 条原始返回 = 接口挂了，网页端要标红
        "api_dead": stats.get("raw", 0) == 0,
    }
    try:
        tmp = STATUS_JSON_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_JSON_PATH)      # 原子替换，网页永远读不到半截文件
    except Exception as exc:
        logger.warning(f"[status] 写入扫描状态失败: {exc}")


def write_scan_state(running: bool, mode: str = "", mode_label: str = "",
                     req_id: str = "") -> None:
    """把"当前是否正在扫描"落成 radar_state.json，供网页端做进度反馈。

    没有这个文件，用户点了刷新之后界面毫无变化，只能靠猜——
    68 人并发抓一轮大概 40 秒，什么都不显示就等于"刷新没用"。
    """
    now = _now_cn()
    prev: Dict[str, Any] = {}
    try:
        if os.path.exists(SCAN_STATE_PATH):
            with open(SCAN_STATE_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f) or {}
    except Exception:
        prev = {}

    payload = dict(prev)
    payload["running"] = bool(running)
    payload["req_id"] = req_id or prev.get("req_id", "")
    if running:
        payload["mode"] = mode
        payload["mode_label"] = mode_label
        payload["started_ts"] = int(now.timestamp())
        payload["finished_ts"] = 0
    else:
        payload["finished_ts"] = int(now.timestamp())
        payload["started_ts"] = 0
        payload["last_mode"] = mode or prev.get("mode", "")
        payload["last_mode_label"] = mode_label or prev.get("mode_label", "")
    payload["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        tmp = SCAN_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCAN_STATE_PATH)
    except Exception as exc:
        logger.warning(f"[state] 写入扫描状态失败: {exc}")


def _load_seen_req() -> str:
    try:
        with open(RESCAN_SEEN_PATH, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("req_id") or "")
    except Exception:
        return ""


def _save_seen_req(req_id: str) -> None:
    try:
        tmp = RESCAN_SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"req_id": req_id, "handled_at": _now_cn().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        os.replace(tmp, RESCAN_SEEN_PATH)
    except Exception as exc:
        logger.warning(f"[rescan] 记录已消费请求失败: {exc}")


def handle_rescan_request() -> bool:
    """轮询 Card Vault 投递的手动刷新请求；有新的就跑一轮 manual 扫描。

    返回 True 表示本轮真的扫了（守护循环据此跳过其它判断）。
    """
    try:
        if not os.path.exists(RESCAN_REQUEST_PATH):
            return False
        with open(RESCAN_REQUEST_PATH, "r", encoding="utf-8") as f:
            req = json.load(f) or {}
    except Exception:
        return False                      # 半截文件 / 目录没挂上，下一轮再看

    req_id = str(req.get("req_id") or "").strip()
    if not req_id or req_id == _load_seen_req():
        return False

    try:
        req_ts = int(req.get("requested_ts") or 0)
    except (TypeError, ValueError):
        req_ts = 0
    age = _now_cn().timestamp() - req_ts if req_ts else 0
    if req_ts and age > RESCAN_MAX_AGE_SEC:
        logger.warning("[手动] 请求已过期 %.0f 分钟，丢弃 req=%s", age / 60, req_id)
        _save_seen_req(req_id)
        return False

    # 先记账再执行：扫描中途抛异常也不会被无限重试拖成死循环
    _save_seen_req(req_id)
    logger.info("[手动] 收到网页刷新请求 req=%s", req_id)
    try:
        run_radar_scan("manual", req_id=req_id)
    except Exception as exc:                       # noqa: BLE001
        logger.error("[手动] 刷新执行失败: %s", exc)
        write_scan_state(running=False, mode="manual", mode_label="手动刷新失败")
        return False
    return True


def _load_closed_fills_index() -> Dict[str, Dict[str, Any]]:
    """把 closed_fills.jsonl 读成 item_id → 记录 的索引（后写的覆盖先写的）。

    行情扫描要「优先用真实截标价」，因为卡淘在拍价是**出价意愿**，
    而截标价是**成交事实**。同一张卡既在 closed_fills 里又在搜索结果里出现时，
    用成交价，不用现价。
    """
    idx: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(CLOSED_FILLS):
        return idx
    try:
        with open(CLOSED_FILLS, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                item_id = str(rec.get("item_id") or "").strip()
                if item_id:
                    idx[item_id] = rec
    except OSError:
        return idx
    return idx


def _card_serial_denom(serial: Any) -> Optional[int]:
    """把档案库里的 `serial` 字段（如 "48/49" / "1/1" / "待查背面"）解析成分母。

    解析不出来就返回 None —— 这时候不做同款校验（宁可宽一点，也不要因为
    "卖家没写编" 就把整张卡的所有样本全丢掉）。
    """
    text = str(serial or "").strip()
    if not text:
        return None
    denom, _special, _badge = parse_serial_details(text)
    return denom


def vault_quote_candidate_ok(title: str, denom: Optional[int]) -> Tuple[bool, str]:
    """判断一条搜索结果是不是「这张卡的同一个型号」。

    三道闸，全部复用识别层现成的规则，不另起一套：
      1. 整包/整盒/整箱 → 不是单卡价；
      2. 双人/多人/合辑 → 不是这一张卡的价；
      3. 原卡带分母（/99）时，候选标题必须解析出**相同分母**才算同款。
         这条最容易被忽略：/99 的银折和 /10 的金折能差 5 倍，
         混在一起取中位数，等于给用户一个毫无意义的数字。
    """
    sealed, word = is_sealed_product(title)
    if sealed:
        return False, "整包(%s)" % word
    mp = multi_player_hit(title)
    if mp:
        return False, "多人卡(%s)" % mp
    if denom is not None:
        cand_denom, _special, _badge = parse_serial_details(title)
        if cand_denom != denom:
            return False, "分母不符(%s≠/%d)" % (cand_denom, denom)
    return True, ""


# ------------------------------------------------------------------------------
# 成交样本的「同一个球员 / 同一个系列」校验
#
# 为什么必须有：成交搜索是按 **EffectiveTimeStamp 排序**（最近成交优先），
# 不是按相关度排序。翻到第 2、3 页时同一系列里全是**别的球员**的卡。
# 2026-10-25 实跑教训：`2026 Chrome Minaur`（用户估值 ¥1,550）被 30 笔杂卡
# 拉到 ¥12（-99%）；`2025 Chrome Flagg`（¥1,450）被拉到 ¥61。
# vault_quote_candidate_ok 只管整包/多人/同分母，从来没管过"是不是这个人"。
# ------------------------------------------------------------------------------
_PLAYER_PAREN_RE = re.compile(r"^(.*?)\s*[（(]\s*(.*?)\s*[)）]\s*$")
# 这些词出现在几乎每条标题里，拿来当"系列一致性"判据没有区分度。
# 注意 **不能** 把 Prizm / Chrome / Optic 这类系列名放进来 —— 那才是区分点。
_SERIES_NOISE = {
    "the", "and", "with", "for", "from", "series", "card", "cards",
    "topps", "panini", "bowman", "upper", "deck", "donruss",
    "rookie", "auto", "autograph", "patch", "refractor", "parallel",
}
_NAME_PARTICLES = {"de", "van", "der", "den", "di", "da", "la", "le", "del", "von"}


def _split_player_names(player: Any) -> Tuple[str, str]:
    """把档案库的 player 字段拆成 (中文名, 英文全名)。

    库里的标准写法是 `亚历克斯·德米纳尔 (Alex de Minaur)`，
    但历史数据有 `科达 Sebastian Korda` 这种混写，也可能只有一种语言。
    """
    text = str(player or "").strip()
    if not text:
        return "", ""
    m = _PLAYER_PAREN_RE.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    cn = "".join(re.findall(r"[\u4e00-\u9fff·]+", text)).strip()
    en = " ".join(re.findall(r"[A-Za-z][A-Za-z.'\-]*", text)).strip()
    return cn, en


def _title_has_player(title: str, cn: str, en: str) -> bool:
    """标题里是否真的出现了这个球员。

    中文名走识别层现成的 `player_name_matched`（带音译块防嵌名守卫）。
    英文名允许卖家只写姓（"Flagg"），但要求命中 ≥2 个词或名字本身只有一个词，
    避免 "de" / "van" 这类介词蒙对，也避免同姓的另一个人混进来。
    """
    low = str(title or "").lower()
    if cn and player_name_matched(cn, low, [cn]):
        return True
    if not en:
        return False
    if en.lower() in low:
        return True
    words = [w for w in re.findall(r"[A-Za-z]{2,}", en)
             if w.lower() not in _NAME_PARTICLES]
    if not words:
        return False
    hits = sum(1 for w in words if w.lower() in low)
    if len(words) == 1:
        return hits == 1
    return hits >= 2


def _keyword_series_terms(keyword: str, en: str) -> List[str]:
    """从询价词里取出「系列/版本」英文词（剔除球员名与通用噪声词）。

    例：`2026 Chrome Minaur` + en=`Alex de Minaur` → `['chrome']`
        `2022 Champions League Ronaldo`        → `['champions', 'league']`
    纯中文询价词取不到英文词时返回空列表，此时**不做**系列校验（避免误杀）。
    """
    name_tokens = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", en or "")}
    terms: List[str] = []
    for word in re.findall(r"[A-Za-z]{3,}", str(keyword or "")):
        low = word.lower()
        if low in name_tokens or low in _SERIES_NOISE:
            continue
        if low.isdigit():
            continue
        terms.append(low)
    return terms


def _title_hits_series(title: str, terms: List[str]) -> bool:
    """标题是否命中询价词里的系列词。

    只要求**过半**命中（向上取整，至少 1 个）：卖家写法差异太大，
    要求全中会把 "2022 World Cup" 这类双词系列全杀掉。
    """
    if not terms:
        return True
    low = str(title or "").lower()
    need = max(1, (len(terms) + 1) // 2)
    return sum(1 for t in terms if t in low) >= need


# ------------------------------------------------------------------------------
# 签字 / 普卡 一致性校验（2026-10-25 第三处纠错）
#
# 实测代价最大的不是"抓错人"，而是"抓对人和系列、但抓错版本"：
#   · 科达 `Orange Geometric Refractor **Auto** /25 (卡签)` 估值 ¥200，
#     同系列同编数同颜色的**非签字**橙折成交只有 ¥2 / ¥5 / ¥8 —— 差 40 倍。
#   · 夏思凝 `Future Star 金折亲笔直签` 估值 ¥9,000，抓到的是红闪折/蓝折（全非签），¥56。
#   · 弗拉格 `Base Refractor` 抓到 Chrome **Update** 的 base，¥61。
# 这三类不校验，「成交行情」反而比旧的在拍价更误导 —— 因为它看起来更权威。
# ------------------------------------------------------------------------------
_AUTO_PAT = re.compile(r"auto|autograph|签字|卡签|签名|on-?card|亲笔", re.I)
_BASE_PAT = re.compile(r"\bbase\b|base\s*card|普卡|基础卡", re.I)


def _auto_like(text: Any) -> bool:
    """是不是签字卡（含卡签 / On-Card Auto / 亲笔直签）。"""
    return bool(_AUTO_PAT.search(str(text or "")))


def _base_like(text: Any) -> bool:
    """是不是最基础的普卡（Base / 普卡）。"""
    return bool(_BASE_PAT.search(str(text or "")))


# 物料卡（球衣/球皮/木片/地板/实物切）是另一个价位体系：
# 一张 /2026 金折平行折 ¥90，一张同年的普通 GU 球衣物料卡只值 ¥6。
# 实测：吉尔伯特 `Gold Parallel RC`（估值 ¥90）抓到「City Connect 特卡 GU球衣物料切」
# 「队徽边框变化版 SP」「小窗 球衣物」—— 全是物料/特卡，中位数被打到 ¥6。
_MEMO_PAT = re.compile(
    r"球衣|物料|实物|切割|切片|球皮|地板|木片|gu\b|relic|memorabilia|patch|jersey|swatch",
    re.I)


def _memo_like(text: Any) -> bool:
    """是不是物料/实物切卡（GU / Patch / Relic）。"""
    return bool(_MEMO_PAT.search(str(text or "")))


def _memo_matches(title: str, want_memo: bool) -> Tuple[bool, str]:
    """物料属性要一致：普通平行折不能拿 GU 球衣物料卡的成交价当参考（差十倍）。"""
    got = _memo_like(title)
    if got != want_memo:
        return False, "物料属性不符(样本%s/本卡%s)" % (
            "有物料" if got else "无物料", "有物料" if want_memo else "无物料")
    return True, ""


# card_type 里这些词是"卡种描述"或"颜色"，不构成身份；剩下的才是子系列专有名。
# 例：`Solus SSP Case Hit Insert` → 只剩 `solus`（这才是这张卡的身份）
#     `Orange Geometric Refractor Auto /25` → 全被过滤 → 不做子系列校验（避免拿中文
#     "橙几何折" 去对英文 "Geometric" 而大面积误杀）
_TYPE_GENERIC = {
    "base", "rc", "rookie", "auto", "autograph", "refractor", "insert",
    "case", "hit", "ssp", "sp", "parallel", "oncard", "on", "card", "cards",
    "signature", "patch", "jersey", "gem", "mint", "psa", "bgs", "sgc", "cgc",
    "orange", "gold", "red", "blue", "green", "pink", "purple", "black",
    "silver", "yellow", "teal", "white", "aqua", "bronze",
    "geometric", "mosaic", "shimmer", "prism", "wave", "ice", "scope",
    "mojo", "choice", "future", "star", "stars", "the", "and", "with",
    # 母品牌不是"子系列身份"：`1980 Topps 45th Anniversary Insert` 里真正的身份是
    # 45th Anniversary，把 Topps 当判据会让标题里的任何一张 Topps 卡都通过
    #（实测：这张特卡被 Topps Match Attax / Topps Now 的成交拉低到 ¥23）。
    "topps", "panini", "bowman", "upper", "deck", "donruss", "leaf", "sage",
    "fleer", "skybox", "score", "press", "pass",
}


def _proper_type_tokens(card_type: Any) -> List[str]:
    """从 card_type 里取「子系列专有名」，用于要求成交样本命中同一个子系列。

    只对特卡/箱货这类「换个设计就是另一张卡」的卡生效 —— 普通平行折
    （橙折 / 金折 / 几何折）本来就靠分母+签字校验约束，再加英文专有名
    只会因为卖家写中文而误杀。
    """
    text = str(card_type or "")
    tokens = []
    for word in re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", text):
        low = word.lower()
        if low in _TYPE_GENERIC:
            continue
        if low not in tokens:
            tokens.append(low)
    return tokens


def _type_matches_user(title: str, want_auto: bool, want_base: bool,
                       want_tokens: Optional[List[str]] = None) -> Tuple[bool, str]:
    """样本的「签字 / 普卡 / 子系列」属性必须和用户那张卡一致。

    签字卡和非签字卡是两个价位体系（实测差 10~40 倍），绝不能混算中位数。
    普卡 vs 平行折同理：Base 和折射能差一个数量级。
    子系列（Solus SSP / 箱货特卡）更是「换个设计就是另一张卡」。
    """
    got_auto = _auto_like(title)
    if got_auto != want_auto:
        return False, "签字属性不符(样本%s/本卡%s)" % (
            "有签" if got_auto else "无签", "有签" if want_auto else "无签")
    # 只有"用户是 base"时才硬要求样本也写 base；反向不强求
    # （卖家经常把平行折写成 "Base 特卡"，硬要求会大面积误杀）
    if want_base and not _base_like(title):
        return False, "本卡是普卡但样本不是"
    if want_tokens:
        low = str(title or "").lower()
        if not any(t in low for t in want_tokens):
            return False, "子系列不符(需含 %s)" % "/".join(want_tokens[:2])
    return True, ""


# ------------------------------------------------------------------------------
# 平行折颜色一致性（2026-10-25 第四处纠错）
#
# 前三道校验解决的是"是不是这个人 / 这个系列 / 这个版本"，平行折还有第四个
# 身份维度：**颜色**。同一张卡换颜色就是换价位，实测差距：
#   · 德鲁·吉尔伯特 `Gold Parallel RC 0149/2026`（估值 ¥90）
#     → 抓到绿几何折 99编 ¥3.2 / 蓝宝石 ¥3.2 / 黄折 75编 ¥3.08（-97%）
#   · 登贝莱 `Orange Cracked Ice Prizm`（¥220）
#     → 抓到黄波纹折 ¥6.6 / 红圈折 ¥12 / 橙折 ✓（-96%）
#   · 夏思凝 `Future Star 金折亲笔直签 8/10`（¥9,000）
#     → 抓到橙折 10编 ¥277 / 乾坤 49编 ¥345（-92%）
#
# 处理原则（宁可没行情，也不给错行情）：
#   样本**没写颜色** → 放行（卖家经常只写"折"，硬要求会把行情全灭）
#   样本写了颜色，且和本卡一个都不重合 → 判为不同版本，剔除
# ------------------------------------------------------------------------------
# 归一色名 → 别名表。英文用词边界匹配（避免 refractor 这类词误伤），
# 中文**必须带颜色语境词**才算（见 _CN_COLOR_CTX 的注释）。
_COLOR_ALIASES = [
    ("orange", ("orange",), ("橙",)),
    ("red", ("red",), ("红",)),
    ("blue", ("blue",), ("蓝",)),
    ("green", ("green",), ("绿",)),
    ("gold", ("gold",), ("金",)),
    ("purple", ("purple", "violet"), ("紫",)),
    ("black", ("black",), ("黑",)),
    ("silver", ("silver",), ("银",)),
    ("yellow", ("yellow",), ("黄",)),
    ("pink", ("pink", "magenta"), ("粉",)),
    ("teal", ("teal",), ("青",)),
    ("aqua", ("aqua", "turquoise"), ("水蓝", "湖蓝")),
    ("white", ("white",), ("白",)),
    ("bronze", ("bronze",), ("铜",)),
    ("rainbow", ("rainbow",), ("彩虹",)),
]
_COLOR_EN_PATS = [(canon, re.compile(r"\b(?:%s)\b" % "|".join(ws), re.I))
                  for canon, ws, _cn in _COLOR_ALIASES]
# ⚠️ 中文单字颜色词**不能裸匹配**，否则队名/地名会大面积误判：
#   旧金山（金）、金州勇士（金）、波士顿红袜（红）、白袜（白）、
#   绿军（绿）、蓝鸟（蓝）、黄蜂（黄）、紫金军团（紫）、黑龙（黑，代卖号名）。
# 实测直接踩到：「2026 Topps Pristine … 旧金山巨人」被判成金折，
# 于是这张橙折样本混进了"金折 /2026"的中位数里。
# 解法：单字颜色词后面必须（隔着最多 3 个汉字）跟上颜色语境词。
# 为什么留 3 个字的空档：卖家写「蓝**碎冰**折」「金**几何**折」「银**平行**」——
# 卡死在"颜色词紧邻折"会把这类全部漏掉（实测漏掉夏思凝的金折/蓝碎冰折）。
# 语境缺失 → 当作"没写颜色" → 放行（宁可少拦，也不误杀真行情）。
_CN_COLOR_CTX = r"折|色|边|圈|光|闪|波|几|脉|纹|箔|镜|平"
_COLOR_CN_PATS = []
for _canon, _ws, _cs in _COLOR_ALIASES:
    for _c in _cs:
        if len(_c) >= 2:                    # 水蓝/湖蓝/彩虹 这类自带区分度
            _COLOR_CN_PATS.append((_canon, re.compile(re.escape(_c))))
        else:
            _COLOR_CN_PATS.append((_canon, re.compile(
                r"%s[\u4e00-\u9fff]{0,3}\s*(?:%s)" % (re.escape(_c), _CN_COLOR_CTX))))


def _color_set(text: Any, drop_words: Any = ()) -> frozenset:
    """取出文本里提到的所有颜色（归一化后）。

    drop_words 用来先把**球员名**从标题里剥掉 ——
    否则 `Jalen Green` 这种姓氏会被当成绿色折、`金河成` 的"金"会被当成金折。
    """
    s = str(text or "")
    for w in drop_words:
        w = str(w or "").strip()
        if not w:
            continue
        if re.fullmatch(r"[A-Za-z .'\-]+", w):
            s = re.sub(r"\b%s\b" % re.escape(w), " ", s, flags=re.I)
        else:
            s = s.replace(w, " ")
    low = s.lower()
    found = set()
    for canon, pat in _COLOR_EN_PATS:
        if pat.search(low):
            found.add(canon)
    for canon, pat in _COLOR_CN_PATS:
        if pat.search(s):
            found.add(canon)
    return frozenset(found)


def _parallel_matches(title: str, want_colors: Any, drop_words: Any = (),
                      require_hit: bool = False) -> Tuple[bool, str]:
    """样本的平行折颜色必须和本卡兼容。

    `want_colors` 为空（本卡是普卡/无颜色描述）时不做校验。

    `require_hit=False`（默认，宽松）：只拦「样本明确写了别的颜色」，
      样本没写颜色一律放行。这是召回兜底用的。
    `require_hit=True`（严格）：本卡写了颜色，样本就必须命中其中一个，
      「样本什么都没写」也算不命中。

    ⚠️ 为什么必须补严格模式（2026-10-25 第五处纠错）：
      旧逻辑是**单向**的 —— 只拦"样本有颜色、本卡没有"。反过来，
      本卡写着 `Blue Geometric Refractor`、样本是一张**什么都没写的 base**，
      单向校验完全不拦，于是 base 的成交（¥6）把蓝几何折的中位数打到 ¥6。
      实测受害者：琴·格雷 / 牌皇 / 塔隆 三张蓝几何折全是这个死法。
      而这两张卡的 card_type 里明明写死了 Blue Geometric —— 系统自己有答案不用。
    """
    want = frozenset(want_colors or ())
    if not want:
        return True, ""
    got = _color_set(title, drop_words)
    if got & want:
        return True, ""
    if not got:
        if require_hit:
            return False, "样本未写颜色(本卡%s)" % "/".join(sorted(want))
        return True, ""
    return False, "颜色不符(本卡%s/样本%s)" % ("/".join(sorted(want)),
                                              "/".join(sorted(got)))


# ------------------------------------------------------------------------------
# 中文子系列一致性（2026-10-25 第六处纠错）
#
# 中国体育卡（奇卡 QICA 中国体育冠军系列）的**子系列名只写中文**：
#   李梦 `青花瓷系列 亲笔直签 /15`（估值 ¥4,200）
# 抓回来的是 `凌云 李梦 /15 龙吟签 金笔卡签`（¥625~775）——
# 同球员、同系列、同编数、同是签字卡，**唯一不同的是子系列（青花瓷 vs 凌云）**，
# 前面七道校验一道都拦不住，中位数被打到 1/6。
#
# 为什么不自动从 card_type 里抓汉字：中文没有词边界，`巴黎奥运比赛服双联书卡直签`
# 这种描述性长串抓出来当 token 会大面积误杀真行情。所以走**白名单**，
# 只认确实构成"身份"的子系列名，没命中就不做校验（宁可少拦，不误杀）。
# ------------------------------------------------------------------------------
_CJK_LINE_WORDS = [
    ("青花瓷", ("青花瓷",)),
    ("凌云", ("凌云", "凌雲")),
    ("龙吟", ("龙吟", "龍吟")),
    ("锦绣", ("锦绣", "錦繡")),
    ("琥珀", ("琥珀",)),
    ("虎纹", ("虎纹", "虎紋")),
    ("银河", ("银河", "銀河")),
    ("黑曜", ("黑曜",)),
    ("铂金大师", ("铂金", "白金大师")),
    ("折扇", ("折扇",)),
    ("赤焰", ("赤焰",)),
    ("赛博", ("赛博",)),
]
_CJK_LINE_PATS = [(canon, re.compile("|".join(re.escape(w) for w in ws)))
                  for canon, ws in _CJK_LINE_WORDS]


def _cjk_line_set(text: Any) -> frozenset:
    """取出文本里出现的中文子系列名（白名单内）。"""
    s = str(text or "")
    return frozenset(c for c, pat in _CJK_LINE_PATS if pat.search(s))


def _cjk_line_matches(title: str, want_lines: Any) -> Tuple[bool, str]:
    """样本的中文子系列必须和本卡一致。

    `want_lines` 为空（本卡没写中文子系列）时不校验。
    本卡写了就必须命中 —— 这类卡的中文子系列名是**唯一身份**，没有"宽松"余地。
    """
    want = frozenset(want_lines or ())
    if not want:
        return True, ""
    got = _cjk_line_set(title)
    if got & want:
        return True, ""
    return False, "中文子系列不符(本卡%s/样本%s)" % (
        "/".join(sorted(want)), "/".join(sorted(got)) or "无")


# ------------------------------------------------------------------------------
# 产品线一致性（同日第四处纠错的另一半）
#
# 弗拉格 `2025-26 Topps Chrome Basketball Base Refractor RC`（估值 ¥1,450）
# 抓到的 30 笔全是 **Topps Chrome Update（TCU）的 base**，稳定 ¥58~61。
# 这不是"同一张卡卖便宜了"，是**另一个产品**的普卡。
# 德鲁·吉尔伯特 `2026 Topps Series 1 Baseball 金折 /2026`（¥90）
# 抓到的却是 Chrome / Chrome Sapphire / Bowman 的平行折，¥3 上下。
#
# 判定方向刻意做成**单向**：只看"样本里有没有本卡没有的产品线标志"。
# 反向（本卡写了 Chrome、样本没写）不拦 —— 卖家经常只写 "2026 网球 Topps 折射"，
# 反向硬拦会把真行情全灭。这条原则和颜色校验一致：只拦**明确冲突**。
# ------------------------------------------------------------------------------
_LINE_PATS = [
    ("chrome", re.compile(r"\bchrome\b|\btc\b(?!\w)", re.I)),
    ("update", re.compile(r"\bupdate\b|\btcu\b", re.I)),
    ("sapphire", re.compile(r"sapphire|蓝宝石", re.I)),
    ("prizm", re.compile(r"\bprizm\b|\bpz\b", re.I)),
    ("optic", re.compile(r"\boptics?\b", re.I)),
    ("select", re.compile(r"\bselect\b", re.I)),
    ("mosaic", re.compile(r"\bmosaic\b|马赛克", re.I)),
    ("donruss", re.compile(r"\bdonruss\b", re.I)),
    ("contenders", re.compile(r"\bcontenders?\b", re.I)),
    ("immaculate", re.compile(r"\bimmaculate\b", re.I)),
    ("eminence", re.compile(r"\beminence\b", re.I)),
    ("obsidian", re.compile(r"\bobsidian\b|黑曜石", re.I)),
    ("treasures", re.compile(r"national\s*treasures|国宝", re.I)),
    ("revolution", re.compile(r"\brevolution\b", re.I)),
    ("spectra", re.compile(r"\bspectra\b", re.I)),
    ("flawless", re.compile(r"\bflawless\b", re.I)),
    ("finest", re.compile(r"\bfinest\b", re.I)),
    ("heritage", re.compile(r"\bheritage\b|传承", re.I)),
    ("tribute", re.compile(r"\btribute\b", re.I)),
    ("inception", re.compile(r"\binception\b", re.I)),
    ("sterling", re.compile(r"\bsterling\b", re.I)),
    ("bowman", re.compile(r"\bbowman\b", re.I)),
    ("merlin", re.compile(r"\bmerlin\b", re.I)),
    # 漫威卡的两个不同产品体系：Topps Chrome Marvel ≠ SkyBox Marvel 大师系列。
    # 实测牌皇 `Blue Geometric Refractor`（¥85）抓到 SkyBox 铂金大师蓝色空间折 ¥39，
    # 产品名里同样有 "Marvel"，不拦就会被中位数采纳。
    ("skybox", re.compile(r"skybox|sky\s*box|大师系列|铂金大师", re.I)),
    ("hoops", re.compile(r"\bhoops\b", re.I)),
    # 2026-10-25 再补一轮：弗拉格 `1980 Topps 45th Anniversary Insert`（估值 ¥140）
    # 抓到的是 `2025-26 Topps **Pristine**` 的同名特卡 ¥74 —— 又一个产品线混淆。
    ("pristine", re.compile(r"\bpristine\b", re.I)),
    ("noir", re.compile(r"\bnoir\b", re.I)),
    ("museum", re.compile(r"museum\s*collection", re.I)),
    ("luminaries", re.compile(r"\bluminaries\b", re.I)),
    ("definitive", re.compile(r"\bdefinitive\b", re.I)),
    ("transcendent", re.compile(r"\btranscendent\b", re.I)),
    ("gilded", re.compile(r"\bgilded\b", re.I)),
    ("impeccable", re.compile(r"\bimpeccable\b", re.I)),
    ("cosmic", re.compile(r"\bcosmic\b", re.I)),
    ("archives", re.compile(r"\barchives\b", re.I)),
    ("chronicles", re.compile(r"\bchronicles\b", re.I)),
    ("tierone", re.compile(r"tier\s*one", re.I)),
    ("courtkings", re.compile(r"court\s*kings", re.I)),
    ("stadiumclub", re.compile(r"stadium\s*club", re.I)),
    ("allenginter", re.compile(r"allen\s*(?:&|and)?\s*ginter", re.I)),
    # 游戏卡 / 打印版 / 分辑：都不是"同一条产品线上的同一张卡"
    ("matchattax", re.compile(r"match\s*attax|游戏版", re.I)),
    ("toppshow", re.compile(r"topps\s*now", re.I)),
    ("flagship", re.compile(r"flagship|旗舰版?", re.I)),
    ("series2", re.compile(r"series\s*(?:2|two|二)|第二[辑批版]", re.I)),
]


def _line_tokens(text: Any) -> frozenset:
    """文本里出现的产品线标志（chrome / update / sapphire / prizm ...）。"""
    s = str(text or "")
    return frozenset(name for name, pat in _LINE_PATS if pat.search(s))


def _line_matches(card_text: Any, title: str) -> Tuple[bool, str]:
    """样本是否出现了本卡没有的产品线标志。

    单向：`样本有、本卡没有` → 拦（TCU 之于 Chrome、Sapphire 之于 Chrome、
    Chrome 之于 Topps Series 1）。`本卡有、样本没写` → 放行。
    """
    extra = _line_tokens(title) - _line_tokens(card_text)
    if extra:
        return False, "产品线不符(样本是 %s)" % "/".join(sorted(extra))
    return True, ""


# ------------------------------------------------------------------------------
# 特卡 / insert 身份（2026-10-25 第五处纠错，用户拿自己那张卡点出来的）
#
# 郑钦文 `1st Card Gold Refractor 09/50`（估价 ¥4,800），系统报 ¥968。
# 把样本标题摊开看，8 条里全是**年内的特卡**：
#   「定格瞬间特卡 ¥630」「Captured 奖杯特卡 ¥765/¥736」「ACES 扑克牌特卡 ¥913/¥1,023」
# 而真正的同款（1st 标 金折 /50 非签非特卡）在那 120 天里只有 3 笔：
#   ¥2,500（用户自己卖的，标题把 Chrome 拼成 Chorme）、¥9,111（PSA9）、¥3,836（PSA9）。
# 特卡换个设计就是另一张卡，绝不能和本体平行折混算。
# ------------------------------------------------------------------------------
_INSERT_PAT = re.compile(
    r"特卡|insert|aces|王牌|captured|定格瞬间|抓拍|奖杯|"
    r"future\s*stars|rise\s*to\s*fame|alter\s*ego|solus|instinct|"
    r"pulsar|hyper|mojo|mt-?\d+", re.I)


def _insert_like(text: Any) -> bool:
    """是不是特卡 / insert（换个设计就是另一张卡的那种）。"""
    return bool(_INSERT_PAT.search(str(text or "")))


def _insert_matches(title: str, want_insert: bool) -> Tuple[bool, str]:
    """特卡属性要一致。

    本卡是本体平行折（1st 金折 / 普折 / Base）时，ACES / Captured / 定格瞬间
    这类特卡的成交价不能拿来当参考 —— 它们便宜且完全是另一张卡。
    """
    got = _insert_like(title)
    if got != want_insert:
        return False, "特卡属性不符(样本%s/本卡%s)" % (
            "是特卡" if got else "不是特卡", "是特卡" if want_insert else "不是特卡")
    return True, ""


# ------------------------------------------------------------------------------
# 询价词：召回优先（2026-10-25 第六处纠错，代价最大的一处）
#
# 卡淘的 searchKey 是**逐词 AND**，少一个词整条就消失。而自动拼的词塞了
# 年份+品牌+英文名+分母四个词：
#   `2024 Chrome Zheng /50` → 只召回 22 行，且**漏掉了用户自己 ¥2,500 的成交**
#   （那条标题把 Chrome 拼成 `Chorme`，一个词不中 → 整条被踢）
#   `2024 Topps Chrome 郑钦文 正金折` → 只剩 8 行（加"正金折"反而更窄）
#   `2024 郑钦文` → 240 行，**第 1 页就是那笔 ¥2,500**
# 结论：召回只用「年份 + 中文名」两个几乎必然出现的词，
# 品牌/系列/颜色/分母/签字全部交给代码校验。
# 用户手改过的 search_query 仍然会跑（作为第二个候选），不会白填。
# ------------------------------------------------------------------------------
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _recall_queries(card: Dict[str, Any]) -> List[str]:
    """给出这张卡的询价词候选。

    有年份 → **宽词在前**（宽词自带年份锚，召回率最高）。
    没年份（如「中国体育 CHINA SPORTS」）→ **存的词在前**：此时宽词就只剩
    球员名，2024/2025/2026 三个年份的产品会全混进来（实测夏思凝的
    金折签字被拉进一堆银平行/蓝碎冰），存的词至少还带一个系列锚。
    """
    stored = str(card.get("search_query") or "").strip()
    p_cn, p_en = _split_player_names(card.get("player"))
    year_m = _YEAR_RE.search(str(card.get("year_set") or ""))
    year = year_m.group(0) if year_m else ""
    name = p_cn or p_en
    broad = " ".join(x for x in (year, name) if x).strip()

    out: List[str] = []
    if year:
        for q in (broad, stored):
            if q and q.lower() not in [x.lower() for x in out]:
                out.append(q)
    else:
        for q in (stored, broad):
            if q and q.lower() not in [x.lower() for x in out]:
                out.append(q)
    return out


def _trim_extremes(pool: List[Dict[str, Any]],
                   factor: float = 20.0) -> List[Dict[str, Any]]:
    """按倍数剔除明显不是同一张卡的极端成交，返回剔除后的池子。

    防止的是"一条脏数据毁掉整张卡的行情"：
      实测 C罗 `Silver Wave Prizm`（估值 ¥145）的成交池里混进一条
      **¥1,500,000** 的售出记录（占位/整包标价），中位数不受影响（¥130），
      但"最近一笔"直接变成 ¥1,500,000，前端主数字一步登天。

    为什么是 20 倍：夏思凝那张 ¥9,000 的金折签字在同池中位数 ¥884 上
    是 10 倍——**那是真的**，不能被砍掉；而 ¥1.5M/¥130 是 11538 倍。
    20 倍这条线两头都能过，且只在池子 ≥3 笔时启用（1~2 笔时中位数本身不可信，
    谈不上"离群"）。
    """
    if len(pool) < 3:
        return pool
    mid = _median([s["sold_price"] for s in pool])
    if mid <= 0:
        return pool
    hi, lo = mid * factor, mid / factor
    kept = [s for s in pool if lo <= s["sold_price"] <= hi]
    return kept or pool


def _recent_median(pool: List[Dict[str, Any]], n: int = 3) -> float:
    """最近 n 笔成交的中位数。

    为什么不能只给"最近一笔"：单笔成交受品相/卖家信誉/深夜截标影响很大，
    一张中位数 ¥374 的卡最新一笔可能是 ¥2,049（蛛网折 /399 混进来）。
    最近 3 笔的中位数既保留"时效性"，又不会被单笔带跑。
    """
    ordered = sorted(pool, key=lambda s: s["closed_ts"], reverse=True)[:n]
    return _median([s["sold_price"] for s in ordered])


def run_vault_quote_scan(req_id: str = "", reason: str = "") -> Dict[str, Any]:
    """给 Card Vault 的在库卡片抓一轮「最近成交行情」，产出 vault_quotes.json。

    **口径：只认已成交记录，绝不用在拍当前价。**

    为什么（2026-10-25 纠错，用户当面指出旧设计是错的）：
      在拍的 current_price 只是「当前出价位置」，开拍早期离成交价差一到两个数量级——
      一张最终拍到 ¥1,500 的卡，开拍第 1 小时可能只显示 ¥3（出价 13 次也才 ¥3，
      因为每次加价幅度很小）。拿这种数算中位数，等于系统性把行情压低。
      更糟的是旧代码写了「优先用 closed_fills 成交价」，但搜索锁死 Status=1（只返回在售），
      已结标的 item 永远不会出现在结果里，`fills.get(item_id)` 一次都命中不了 —— 死代码。

      正解：卡淘网页「售出的宝贝」tab 的真实调用是 SeachByStatus('Status',-2)，
      即 searchJson=[{"Key":"Status","Value":-2}]。实测同一关键词返回上万条历史成交，
      每条带 LowestPrice（最终成交价）+ PriceCount（出价数）+ EffectiveDate（截标时间）。

    七道「同款」校验 + 一道壳一致性（每道都对应一次实跑踩过的坑）：
      · 整包/整盒 → 不是单卡；多人合卡 → 不是同款；同分母 → 编数不同不是同款
      · 球员点名 → 成交搜索按截标时间排序，翻页后全是同系列别的球员
      · 系列词   → 同球员不同系列（Prizm / Chrome / Immaculate）价位不互通
      · 签字/普卡 → 差 10~40 倍，绝不能混算
      · 物料     → GU 球衣切片 vs 普通平行折，差 10 倍
      · 颜色     → 金折 /25 vs 绿几何折 99编，差 30 倍
      · 产品线   → Chrome / Chrome Update(TCU) / Sapphire / Pristine 是不同产品
      · 评级档一致 → PSA10 不能拿裸卡成交价当参考（差一倍以上）
    同档样本 ≥2 记为 basis=sold；不足则混档使用并记 basis=sold_mixed（前端弱化展示）。

    绝不写 cards 表，也绝不写 /app/data/vault（那边对我们是只读挂载）。
    """
    inventory = _load_json_safe(VAULT_INVENTORY_PATH, {})
    cards = inventory.get("items") or []
    if not isinstance(cards, list):
        cards = []

    now_ts = int(_now_cn().timestamp())
    logger.info("[库存行情] 开始：清单 %d 张卡（%s），口径=最近 %d 天成交",
                len(cards), reason or "定时", VAULT_QUOTE_SOLD_MAX_DAYS)
    started = time.time()

    quotes: Dict[str, Dict[str, Any]] = {}
    n_skip_empty = n_fail = n_no_sample = n_mixed = 0
    n_name_reject = n_series_reject = n_type_reject = 0
    n_color_reject = n_line_reject = n_memo_reject = n_insert_reject = 0
    n_spec_reject = 0

    for card in cards[:VAULT_QUOTE_MAX_CARDS]:
        card_id = str(card.get("card_id") or "").strip()
        queries = _recall_queries(card)
        if not card_id or not queries:
            n_skip_empty += 1
            continue

        denom = _card_serial_denom(card.get("serial"))
        my_grade = _card_grade_bucket(card)
        p_cn, p_en = _split_player_names(card.get("player"))
        # 签字/普卡属性取自 card_type（库里 PSA 分数、Auto、Base 都写在那个字段）
        want_auto = _auto_like(card.get("card_type")) or _auto_like(card.get("notes"))
        want_base = _base_like(card.get("card_type"))
        want_tokens = _proper_type_tokens(card.get("card_type"))
        want_memo = _memo_like(card.get("card_type"))
        want_colors = _color_set(card.get("card_type"))
        want_insert = _insert_like(card.get("card_type")) or _insert_like(card.get("notes"))
        # 中文子系列：中国体育卡的子系列名只写中文，英文 token 一个都抓不到
        want_cjk = _cjk_line_set("%s %s" % (card.get("card_type") or "",
                                            card.get("notes") or ""))
        card_line_text = "%s %s" % (card.get("year_set") or "", card.get("card_type") or "")
        # 提颜色前先把球员名从标题里剥掉：Jalen Green 的 Green 不是绿折，
        # 金河成的"金"不是金折。
        name_words = [p_cn] + re.findall(r"[A-Za-z]{2,}", p_en or "")

        verdict: Dict[Any, Tuple[bool, str]] = {}

        def _judge(s: Dict[str, Any], terms: List[str]) -> bool:
            """六道同款校验。结果按 (item_id, 系列词) 记忆化 ——
            翻页是增量评估的，同一个 item 会被反复看到，计数器不能重复加。"""
            nonlocal n_name_reject, n_series_reject, n_type_reject
            nonlocal n_memo_reject, n_color_reject, n_line_reject, n_insert_reject
            nonlocal n_spec_reject
            key = (s["id"], tuple(terms))
            hit = verdict.get(key)
            if hit is not None:
                return hit[0]
            ok, why, which = True, "", ""
            check, why = vault_quote_candidate_ok(s["title"], denom)
            if not check:
                ok, which = False, "cand"
            elif not _title_has_player(s["title"], p_cn, p_en):
                ok, which = False, "name"
            elif not _title_hits_series(s["title"], terms):
                ok, which = False, "series"
            else:
                check, why = _type_matches_user(s["title"], want_auto, want_base,
                                                want_tokens)
                if not check:
                    ok, which = False, "type"
                else:
                    check, why = _memo_matches(s["title"], want_memo)
                    if not check:
                        ok, which = False, "memo"
                    else:
                        # 换颜色就是换价位：金折 /25 和绿几何折 99编 差 30 倍。
                        # require_hit=True：本卡写了颜色，样本**必须**命中某一个 ——
                        # 「样本什么都没写」也算不命中（那就是一张 base）。
                        check, why = _parallel_matches(s["title"], want_colors,
                                                       name_words, require_hit=True)
                        if not check:
                            ok, which = False, "color"
                        else:
                            # 中文子系列（青花瓷 vs 凌云）：同球员同编同签字，
                            # 唯一区别就是这个中文名，没有别的能拦住
                            check, why = _cjk_line_matches(s["title"], want_cjk)
                            if not check:
                                ok, which = False, "cjk"
                            else:
                                # 同一年的 Chrome / Chrome Update / Sapphire 是三条产品线
                                check, why = _line_matches(card_line_text, s["title"])
                                if not check:
                                    ok, which = False, "line"
                                else:
                                    # 特卡/insert 是另一张卡：ACES / Captured / 定格瞬间
                                    check, why = _insert_matches(s["title"], want_insert)
                                    if not check:
                                        ok, which = False, "insert"
            verdict[key] = (ok, why)
            if not ok:
                if which == "name":
                    n_name_reject += 1
                elif which == "series":
                    n_series_reject += 1
                elif which == "type":
                    n_type_reject += 1
                elif which == "memo":
                    n_memo_reject += 1
                elif which == "color":
                    n_color_reject += 1
                elif which == "cjk":
                    n_spec_reject += 1
                elif which == "line":
                    n_line_reject += 1
                elif which == "insert":
                    n_insert_reject += 1
                logger.debug("[库存行情]   跳过(%s%s): %s", which,
                             (": " + why) if why else "", s["title"][:44])
            return ok

        # ---- 1) 多询价词 + 自适应翻页抓「已成交」 ----
        # 先宽后窄：`年份 + 中文名` 是召回率最高的组合（品牌词/分母只会砍召回，
        # 卡淘 searchKey 是逐词 AND —— 卖家把 Chrome 拼成 Chorme，整条就消失了，
        # 实测那正好漏掉了用户自己 £2,500 的成交）。
        oldest_ts = now_ts - VAULT_QUOTE_SOLD_MAX_DAYS * 86400
        pool_by_id: Dict[str, Dict[str, Any]] = {}
        ## 关键：kept 是**并集**，不是"最后一次过滤的结果"。
        #  踩过的坑：宽词已经把 ¥2,500 捞进 kept，紧接着跑存的窄词
        #  （terms=['chrome']）时把整个池子重判一遍 —— 那条标题把 Chrome 拼成
        #  Chorme，系列校验不中，于是又被踢出去，最终报 ¥1,161。
        #  语义应该是"任何一个询价词认可它，它就算同款"。
        kept_by_id: Dict[str, Dict[str, Any]] = {}
        kept: List[Dict[str, Any]] = []
        query_used = ""
        failed = False
        for keyword in queries:
            terms = _keyword_series_terms(keyword, p_en)
            for page in range(1, VAULT_QUOTE_SOLD_MAX_PAGES + 1):
                try:
                    rows = _request_search_page(keyword, page, sold=True)
                except Exception as exc:          # noqa: BLE001
                    failed = True
                    logger.warning("[库存行情] %s 第 %d 页抓取失败: %s",
                                   keyword, page, exc)
                    break
                if not rows:
                    break
                for row in rows:
                    rid = str(row.get("ID") or row.get("id") or "")
                    if rid:
                        pool_by_id.setdefault(rid, row)
                for s in parse_sold_item_list(list(pool_by_id.values()),
                                              VAULT_QUOTE_SOLD_MAX_DAYS):
                    if s["id"] not in kept_by_id and _judge(s, terms):
                        kept_by_id[s["id"]] = s
                kept = list(kept_by_id.values())
                # 记下"真正捞到样本"的那个词（宽词优先，所以第一个命中的就是它）
                if kept and not query_used:
                    query_used = keyword
                # 样本够了就停：热门球员翻 8 页只覆盖两三天，对中位数没帮助
                if len(kept) >= VAULT_QUOTE_TARGET_SAMPLES:
                    break
                # 本页已出现窗口外的成交 → 后面的页只会更老，不必再翻
                page_times = [d for d in (_parse_effective_date(
                    str(r.get("EffectiveDate") or "")) for r in rows) if d]
                if page_times and min(d.timestamp() for d in page_times) < oldest_ts:
                    break
                time.sleep(REQUEST_DELAY_SEC)
            if len(kept) >= VAULT_QUOTE_TARGET_SAMPLES:
                break
        if failed and not pool_by_id:
            n_fail += 1
            continue
        if not kept:
            n_no_sample += 1
            logger.debug("[库存行情]   %s 近 %d 天无同款成交（试过 %s）",
                         card_id, VAULT_QUOTE_SOLD_MAX_DAYS, " / ".join(queries))
            continue

        # 严格校验已经在 _judge 里做掉了（require_hit=True + 中文子系列），
        # 这里的 spec 只用来告诉前端"这张卡的样本是按版本严格筛过的"。
        pool0, spec_applied = kept, bool(want_colors or want_cjk)

        # ---- 3) 壳一致性：有同档成交就只认同档 ----
        # PSA10 的成交价是裸卡的 1.5~2 倍，混在一起算中位数必然失真。
        # 旧逻辑要求同档 ≥2 笔才启用，结果裸卡只卖了 1 笔时，
        # 那张裸卡的成交被两张 PSA9 拉高成 ¥3,836（真实 1 笔就是 ¥2,500）。
        same = [s for s in pool0 if _grade_bucket(s["title"]) == my_grade]
        if same:
            pool, basis = same, "sold"
        else:
            pool, basis = pool0, "sold_mixed"
            n_mixed += 1
        # ---- 4) 剔极端值，只用于「点估计」（最近一笔 / 最近 3 笔） ----
        # 中位数本身对离群免疫，不能拿剔完的池子算 —— 剔完会改变样本构成，
        # 反而让中位数漂移（实测：池子 [4,480,500,510,520] 剔掉 ¥4 后
        # 中位数从 500 变 495，属于"为了修 A 弄坏 B"）。
        point_pool = _trim_extremes(pool)
        pool = pool[:VAULT_QUOTE_SOLD_MAX_SAMPLES]

        prices = [s["sold_price"] for s in pool]
        median_rmb = _median(prices)
        recent_rmb = _recent_median(point_pool)
        # 展示样本挑「离中位数最近的」，不是最便宜的：中位数 ¥1,544 的卡下面
        # 紧跟一条 ¥4 的杂卡，用户第一反应是这功能瞎报（真实踩过）。
        samples = sorted(pool, key=lambda s: abs(s["sold_price"] - median_rmb))
        latest = max(point_pool, key=lambda s: s["closed_ts"])
        # 最近一笔和中位数差 2.5 倍以上 → 标脏。前端这时改用近期中位数当主数字，
        # 把这一笔作为「存疑」展示，而不是让 ¥2,049 冒充 ¥374 的行情。
        outlier = bool(median_rmb > 0 and (
            latest["sold_price"] > median_rmb * 2.5
            or latest["sold_price"] < median_rmb / 2.5))

        quotes[card_id] = {
            "median_rmb": int(round(median_rmb)),
            "sample_count": len(prices),
            # 最新一笔单独给出来：用户最认"上一张卖了多少钱"，中位数是补强
            "latest_rmb": int(round(latest["sold_price"])),
            "latest_closed": latest["closed_str"],
            # 最近 3 笔中位数：时效性 + 抗单笔噪声，前端在 latest 存疑时用它
            "recent_rmb": int(round(recent_rmb)),
            "latest_outlier": outlier,
            "updated_ts": now_ts,
            "basis": basis,
            "spec_applied": spec_applied,
            "grade_bucket": my_grade,
            "window_days": VAULT_QUOTE_SOLD_MAX_DAYS,
            "search_query": query_used,
            "samples": [{
                "title": s["title"],
                "price": s["sold_price"],
                "type": "sold",
                "bid_count": s["bid_count"],
                "closed_str": s["closed_str"],
                "url": CARDHOBBY_ITEM_URL_TPL.format(item_id=s["id"]),
            } for s in samples[:VAULT_QUOTE_SAMPLE_MAX]],
        }
        logger.info("[库存行情] %s | %s → 中位 ¥%s / 最近3笔 ¥%s / 最新 ¥%s%s"
                    "（%d 笔成交, %s, 档=%s%s）",
                    card_id, query_used, quotes[card_id]["median_rmb"],
                    quotes[card_id]["recent_rmb"], quotes[card_id]["latest_rmb"],
                    "(存疑)" if outlier else "", len(prices), basis, my_grade,
                    ", 严格规格" if spec_applied else "")
        time.sleep(REQUEST_DELAY_SEC)

    payload = {
        "scan_ts": now_ts,
        "generated_at": _now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "req_id": req_id,
        "reason": reason or "schedule",
        # 口径写在结果里：前端不靠猜，看到 basis 就知道这个数是成交价还是别的
        "basis": "sold",
        "window_days": VAULT_QUOTE_SOLD_MAX_DAYS,
        "card_total": len(cards),
        "quoted": len(quotes),
        "skipped_empty_query": n_skip_empty,
        "fetch_failed": n_fail,
        "no_sample": n_no_sample,
        "mixed_grade": n_mixed,
        # 同款校验的拦截计数：这几个数变大说明"搜索词太宽"或"季节性没货"，
        # 是调搜索词最直接的信号，必须写进结果而不是只躺在日志里。
        "name_rejected": n_name_reject,
        "series_rejected": n_series_reject,
        "type_rejected": n_type_reject,
        "color_rejected": n_color_reject,
        "line_rejected": n_line_reject,
        "memo_rejected": n_memo_reject,
        "insert_rejected": n_insert_reject,
        # 严格规格（颜色必须命中 + 中文子系列必须命中）剔掉的样本数。
        # 这个数大通常意味着"库里这张卡的 card_type 写得比卖家细"，
        # 不是错，但值得抽查——所以单独记一笔，不和 color_rejected 混。
        "spec_rejected": n_spec_reject,
        "duration_sec": round(time.time() - started, 1),
        "quotes": quotes,
    }
    _atomic_write_json(VAULT_QUOTES_PATH, payload)
    logger.info("[库存行情] 完成：%d/%d 张卡拿到成交行情"
                "（失败 %d，近%d天无成交 %d，跳过 %d，含异级档 %d，"
                "非本球员 %d，系列不符 %d，签字/普卡不符 %d，"
                "物料不符 %d，颜色不符 %d，产品线不符 %d，特卡不符 %d，"
                "规格不符 %d，用时 %.0fs）",
                len(quotes), len(cards), n_fail, VAULT_QUOTE_SOLD_MAX_DAYS,
                n_no_sample, n_skip_empty, n_mixed,
                n_name_reject, n_series_reject, n_type_reject,
                n_memo_reject, n_color_reject, n_line_reject, n_insert_reject,
                n_spec_reject, payload["duration_sec"])
    return payload


def _vault_quote_age_hours() -> float:
    """距上次库存行情扫描过了多少小时（没有记录 → 一个很大的数，表示"该跑了"）。"""
    data = _load_json_safe(VAULT_QUOTES_PATH, {})
    ts = int(data.get("scan_ts") or 0)
    if not ts:
        return 1e9
    return max(0.0, (_now_cn().timestamp() - ts) / 3600.0)


def _is_off_peak() -> bool:
    """避开 17:00 / 19:20 两个定时轮的前后半小时。

    库存行情要逐卡串行请求 100+ 次，跟主扫描抢带宽会把真正要抢的漏卡挤晚。
    """
    hm = _now_cn().strftime("%H:%M")
    if "16:50" <= hm <= "17:25":
        return False
    if "19:10" <= hm <= "19:45":
        return False
    return True


def _load_seen_quote_req() -> str:
    try:
        with open(VAULT_QUOTE_SEEN_PATH, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("req_id") or "")
    except Exception:                             # noqa: BLE001
        return ""


def _save_seen_quote_req(req_id: str) -> None:
    try:
        tmp = VAULT_QUOTE_SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"req_id": req_id,
                       "handled_at": _now_cn().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        os.replace(tmp, VAULT_QUOTE_SEEN_PATH)
    except Exception as exc:                      # noqa: BLE001
        logger.warning("[库存行情] 记录已消费请求失败: %s", exc)


def handle_vault_quote_request() -> bool:
    """轮询 Card Vault 投递的「刷新库存行情」请求；有新的就跑一轮。

    与 rescan 同一套「先记账再执行」的纪律：中途抛异常也不会被无限重试拖死。
    """
    try:
        if not os.path.exists(VAULT_QUOTE_REQUEST_PATH):
            return False
        with open(VAULT_QUOTE_REQUEST_PATH, "r", encoding="utf-8") as f:
            req = json.load(f) or {}
    except Exception:                             # noqa: BLE001
        return False                              # 半截文件 / 没挂上，下一轮再看

    req_id = str(req.get("req_id") or "").strip()
    if not req_id or req_id == _load_seen_quote_req():
        return False

    try:
        req_ts = int(req.get("requested_ts") or 0)
    except (TypeError, ValueError):
        req_ts = 0
    if req_ts and (_now_cn().timestamp() - req_ts) > VAULT_QUOTE_REQ_MAX_AGE_SEC:
        logger.warning("[库存行情] 请求已过期，丢弃 req=%s", req_id)
        _save_seen_quote_req(req_id)
        return False

    _save_seen_quote_req(req_id)
    logger.info("[库存行情] 收到网页刷新请求 req=%s（%s 张卡）", req_id, req.get("count"))
    try:
        run_vault_quote_scan(req_id=req_id, reason="manual")
    except Exception as exc:                      # noqa: BLE001
        logger.error("[库存行情] 执行失败: %s", exc)
    return True


def maybe_run_scheduled_vault_quote_scan() -> bool:
    """没有手动请求时，库存行情最多 VAULT_QUOTE_INTERVAL_HOURS 小时重扫一次。

    只在非高峰期跑（由调用方在 17:00 摸底轮结束后触发），避免跟抢漏抢带宽。
    """
    age = _vault_quote_age_hours()
    if age < VAULT_QUOTE_INTERVAL_HOURS:
        return False
    if not _is_off_peak():
        logger.info("[库存行情] 距上次 %.1fh 已到期，但当前是高峰期，推迟到下一轮",
                    age)
        return False
    try:
        run_vault_quote_scan(reason="schedule(%.0fh)" % age)
    except Exception as exc:                      # noqa: BLE001
        logger.error("[库存行情] 定时扫描失败: %s", exc)
        return False
    return True


def compute_smart_max_hours(requested: float) -> float:
    """手动刷新时的时间窗口。

    凌晨 00:21 手动刷一次，如果死守 5.5 小时，只会扫到凌晨 1-5 点截标的冷门卡，
    而真正要抢的是当晚 20:00-22:30 的黄金档——那还在 20 小时之外。
    所以非黄金时段自动把窗口撑到覆盖今晚 23:00，但**上限 12 小时**：
    无限放大会把明天的卡也倒进「待抢」，反而看不清眼前的机会。
    """
    now = _now_cn()
    if 15 <= now.hour <= 21:
        return requested
    target = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    hours_to_target = (target - now).total_seconds() / 3600.0
    return round(max(requested, min(12.0, hours_to_target)), 1)


def _fetch_one(player_cfg: Dict[str, Any], max_hours: float):
    """单球员抓取（供线程池调用）。异常不外抛，打包成第四个元素返回。"""
    try:
        items, raw_count = fetch_player_auctions(player_cfg, max_hours=max_hours)
        return player_cfg, items, raw_count, None
    except Exception as exc:                      # noqa: BLE001
        return player_cfg, [], 0, exc


def _cap_by_player(rows: List[Dict[str, Any]], total_cap: int,
                   per_player_cap: int = MAX_HITS_PER_PLAYER) -> List[Dict[str, Any]]:
    """按「每人最多 N 张 + 每桶总量上限」截断；输入需已按优先级降序排好。

    关键语义（容易被忽略）：超出上限的候选**既不推送、也不写入 hits.jsonl**，
    因此网页端同样看不到它们。也就是说用户看到「今晚 2 张漏」时，
    无法分辨那是全部，还是被截断后的残影 —— 所以调用方必须把丢弃数量
    上报到 stats 与心跳里，让"被截断"这件事本身可见。
    """
    per_player: Dict[str, int] = {}
    kept: List[Dict[str, Any]] = []
    for row in rows:
        name = row.get("player_cn", "")
        if per_player.get(name, 0) >= per_player_cap:
            continue
        per_player[name] = per_player.get(name, 0) + 1
        kept.append(row)
        if len(kept) >= total_cap:
            break
    return kept


def run_radar_scan(mode: str, req_id: str = "") -> List[Dict[str, Any]]:
    """mode: 1700(摸底) | 1920(终局) | manual(手动刷新)"""
    init_db()
    watchlist = load_watchlist()

    if mode == "1700":
        max_hours = 5.5
        mode_label = "17:00 晚场截标摸底初筛"
    elif mode == "manual":
        max_hours = compute_smart_max_hours(3.0)
        mode_label = f"手动刷新（窗口 {max_hours}h）"
    else:
        max_hours = 3.0
        mode_label = "19:20 截标前终极必抢名单"

    logger.info("========== 启动 [%s] (截标窗口 <= %.1fh, 监控 %d 人, 并发 %d) ==========",
                mode_label, max_hours, len(watchlist), MAX_WORKERS)
    write_scan_state(running=True, mode=mode, mode_label=mode_label, req_id=req_id)

    # ---- 扫描前：回填上一批已截标的成交价 → 重算校准 → 重算学习规则 ----
    # 顺序不能反：校准依赖成交流水，学习规则依赖反馈，两者都必须在评估之前就位。
    try:
        filled = harvest_closed_fills()
        if filled:
            logger.info("[回填] 本轮补到 %d 条真实成交价", filled)
        recalibrate_tiers()
        rules = rebuild_learned_rules(watchlist)
        learned = apply_learned_rules(watchlist, rules)
        blocked_set = _blocked_ids(rules)
        if blocked_set:
            logger.info("[学习] 生效：拦截 %d 条已标有误的拍品 / 新增排除词 %d 个 / "
                        "校准档位 %d 个",
                        len(blocked_set), learned["exclude"], learned["calib"])
    except Exception as exc:                      # noqa: BLE001
        # 学习链路坏了不能让整轮扫描挂掉 —— 雷达的主职责还是抓漏
        logger.error("[学习] 回填/校准流程异常，本轮降级为纯扫描: %s", exc)
        blocked_set = set()

    # 本轮扫描的代次号。hits.jsonl 是 append-only 流水，网页只认「最新一代」——
    # 否则被拦截/掉出窗口的卡会一直沿用上一代的旧记录挂在页面上（真实踩过：
    # 规则收紧了，页面上 321 倍的那几条却怎么刷新都不消失）。
    scan_id = int(_now_cn().timestamp())

    steals: List[Dict[str, Any]] = []
    near_misses: List[Dict[str, Any]] = []
    blinds: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    seen_ids = set()
    errors = 0
    raw_total = 0
    window_total = 0
    sealed_blocked = 0
    players_zero = 0
    # 进识别层之前的原始候选（离线回测的燃料，见 record_raw_candidates 的注释）
    raw_pool: List[Dict[str, Any]] = []

    # ---- 并发抓取：68 人串行要 4 分钟以上，手动刷新等不起 ----
    # 注意：抓取并发，但「评估 + 写流水」严格串行，避免多线程同时 append 同一个 JSONL。
    fetched: List[tuple] = []
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_fetch_one, p, max_hours) for p in watchlist]
            for fut in as_completed(futures):
                fetched.append(fut.result())
    except Exception as exc:                      # noqa: BLE001
        logger.error("[抓取] 线程池异常: %s", exc)

    # 按词库顺序回放，保证日志与命中顺序稳定可复现
    # 用 .get：词库已经过 _normalize_watchlist 校验，但这一行绝不能成为
    # 「一条脏数据 → 整晚不扫」的单点。历史上它确实在 try 之外。
    order = {p.get("keyword", ""): i for i, p in enumerate(watchlist)}
    fetched.sort(key=lambda x: order.get(x[0].get("keyword", ""), 999))

    for player_cfg, items, raw_count, err in fetched:
        if err is not None:
            errors += 1
            logger.error("  [跳过] 检索 %s 失败: %s", player_cfg.get("keyword", "?"), err)
            continue
        raw_total += raw_count
        window_total += len(items)
        if raw_count <= 0:
            players_zero += 1
        logger.info("  %s: 窗口内 %d 条（接口原始 %d 行）",
                    player_cfg.get("keyword", "?"), len(items), raw_count)

        for item in items:
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])

            # 落一份精简原始记录再进识别层：即使后面被新规则拦掉，
            # 下次 --backtest 也能看到"这条本来会被拦"而不是凭空消失。
            slim = dict(item)
            slim["player_kw"] = player_cfg.get("keyword", "")
            raw_pool.append(slim)

            # 你点过「有误」的拍品永久不再上报
            if item["id"] in blocked_set:
                continue

            try:
                res = evaluate_auction_item_v2(item, player_cfg)
            except Exception as exc:              # noqa: BLE001
                logger.warning("  [评估异常] %s -> %s", item.get("title", "")[:40], exc)
                continue

            if not res:
                # 被拦掉的原因里，整包占了很大比例，单独计数便于体检
                if is_sealed_product(item.get("title", ""))[0]:
                    sealed_blocked += 1
                continue
            candidates.append(res)

    # ---- 封顶：先排序再截断，然后才写流水 ----
    # 68 人词库下"低于红线"天然会有几百条，不封顶的话网页等于没用。
    # 排序键是「按红线买入的期望利润」，与网页的强烈推荐口径一致。
    candidates.sort(key=lambda r: -float(r.get("profit_at_red") or 0))

    kept_steals = _cap_by_player(
        [r for r in candidates if r["opportunity_type"] == "steal"], MAX_STEAL_TOTAL)
    kept_near = _cap_by_player(
        [r for r in candidates if r["opportunity_type"] == "near_miss"], MAX_NEAR_TOTAL)
    kept_blind = _cap_by_player(
        [r for r in candidates if r["opportunity_type"] == "blind"], MAX_BLIND_TOTAL)

    dropped = len(candidates) - len(kept_steals) - len(kept_near) - len(kept_blind)
    if dropped > 0:
        logger.info("  [封顶] 候选 %d 条，按利润保留 真漏%d/准漏%d/观察%d，其余 %d 条不写入流水",
                    len(candidates), len(kept_steals), len(kept_near), len(kept_blind), dropped)

    for res in kept_steals + kept_near + kept_blind:
        res["scan_id"] = scan_id
        snap = record_and_compare_snapshot(
            item_id=res["item_id"], scan_mode=mode, title=res["title"],
            current_price=res["current_price"], bid_count=res["bid_count"],
            remaining_minutes=res["remaining_minutes"])
        res.update(snap)
        record_hit(res, mode)

        if res["opportunity_type"] == "steal":
            steals.append(res)
            logger.info("  [真漏] %s | ¥%s → 埋≤¥%s(合邮¥%s) | %s",
                        res["title"][:40], res["current_price"],
                        res["safe_snipe_bid"], res["combined_snipe_bid"], res["tier_desc"])
        elif res["opportunity_type"] == "near_miss":
            near_misses.append(res)
            logger.info("  [准漏] %s | ¥%s 还差 ¥%s 够红线 | %s",
                        res["title"][:40], res["current_price"],
                        res["gap_rmb"], res["tier_desc"])
        else:
            blinds.append(res)
            logger.info("  [起拍] %s | 起拍¥%s 0出价 → 埋≤¥%s | %s",
                        res["title"][:40], res["current_price"],
                        res["safe_snipe_bid"], res["tier_desc"])

    enrich_same_seller_shipping(steals)
    steals.sort(key=lambda x: (x["same_seller_count"] >= 2, x["roi_multiple"]), reverse=True)
    near_misses.sort(key=lambda x: float(x["gap_rmb"]))
    blinds.sort(key=lambda x: -float(x.get("profit_at_red") or 0))
    trim_hits()

    # ---- 登记进成交追踪表：这些卡截标 10 分钟后会回抓真实成交价 ----
    # 用户抢没抢都记 —— 没抢到的卡成交价才是市场最真实的出价意愿。
    try:
        register_watch(kept_steals + kept_near + kept_blind)
        logger.info("[追踪] 已登记 %d 张卡等待截标价回填",
                    len(kept_steals) + len(kept_near) + len(kept_blind))
    except Exception as exc:                      # noqa: BLE001
        logger.warning("[追踪] 登记失败: %s", exc)

    # ---- 落原始候选池：供 --backtest 用最新规则重跑 ----
    # 放在评估之后、写流水之前写盘上的顺序无关紧要，但**必须每轮都写**：
    # 断了几天，回测就只能看到零散几天，看不出"规则收紧后多拦了什么"。
    try:
        if raw_pool:
            record_raw_candidates(raw_pool, mode)
            trim_raw_candidates()
            logger.info("[原始池] 本轮留存 %d 条进识别层前的候选（累计文件见 raw_candidates.jsonl）",
                        len(raw_pool))
    except Exception as exc:                      # noqa: BLE001
        logger.warning("[原始池] 留存失败: %s", exc)

    stats = {
        "players": len(watchlist),
        "raw": raw_total,
        "window": window_total,
        "candidates": len(candidates),
        "hits": len(steals),
        "near_miss": len(near_misses),
        "blind": len(blinds),
        "sealed_blocked": sealed_blocked,
        "errors": errors,
        "players_zero": players_zero,
        "mode": mode,
        "max_hours": max_hours,
        # 词库问题必须随 stats 走到推送里，否则用户永远不知道自己在扫别人的名单
        "watchlist_problems": list(_WATCHLIST_PROBLEMS),
        # 被封顶截掉、因而连网页都看不到的候选数量
        "dropped_by_cap": dropped,
    }
    logger.info("========== [%s] 结束：真漏 %d / 准漏 %d / 起拍观察 %d / 整包拦截 %d / "
                "候选 %d / 接口 %d 行 / 窗口 %d 条 / 零召回关键词 %d 个 ==========",
                mode_label, len(steals), len(near_misses), len(blinds),
                sealed_blocked, len(candidates), raw_total, window_total, players_zero)

    low = low_recall_reason(stats)
    if low:
        logger.warning("[召回] ⚠️ %s —— 疑似限流或接口变更，本轮结论不可全信", low)

    write_scan_status(stats, mode_label)
    write_scan_state(running=False, mode=mode, mode_label=mode_label, req_id=req_id)

    # ---------------------------------------------------------------- 推送决策
    # 19:20 终局轮：有漏推战报、无漏推心跳、接口全哑推告警。
    # 手动刷新只更新网页，不推微信（否则你每刷一次手机就响一次）。
    if mode == "manual":
        logger.info("[推送] 手动刷新不推送，结果仅写入网页。")
    elif mode == "1920":
        if raw_total == 0:
            logger.error("[告警] 所有关键词均返回 0 条原始数据，判定接口失效。")
            send_api_alert(stats)
        elif steals:
            send_notifications(steals, mode_label=mode_label, max_hours=max_hours)
        else:
            logger.info("[心跳] 本轮无命中，发送每日存活心跳。")
            send_heartbeat(stats)
    else:
        if steals:
            send_notifications(steals, mode_label=mode_label, max_hours=max_hours)
        else:
            logger.info("[心跳] 17:00 摸底轮 0 命中，不推送（终局轮会发心跳）。")

    return steals


def run_self_check() -> None:
    """
    全维度算法与数据库自检演练（验证同名消歧、首编加成、卡签加成、同店合邮、17:00->19:20盘口追踪）
    """
    init_db()
    watchlist = load_watchlist()
    cfg_map = {c["keyword"]: c for c in watchlist}

    # 0. 回归哨兵：截图里出现过的三类误判，任何一条回退都必须当场炸出来
    #
    # (a) 111/150编 被读成 1/1 ── 老代码的 r"(?:1/1|...)" 没有数字边界
    assert parse_serial_details("2025 Topps Chrome 科达 橙折 111/150编")[0] == 150, \
        "111/150编 又被误判了"
    # (b) 三级/3级 被读成 /3编 ── v3 丢掉守卫后回归过
    assert parse_serial_details("科达 三级/3级 特卡")[0] is None, "三级/3级 又被当成 /3编"
    # (c) 整包整盒被当成单卡 ── 睿卡嘉年华那批福特拉卡包就是这么漏进来的
    sealed_samples = [
        "2022-23 Panini Chronicles 足球 福特拉 Fat Pack 肥包 整包未拆",
        "睿卡嘉年华 2025-26 Panini Prizm 篮球 卡包 一盒12包",
        "2024 Topps Chrome 网球 Hobby Box 整盒 全新未拆",
        "Panini Select 足球 Blaster Box 密封",
        "球星卡 福袋 随机3张",
        # 线上实测漏网的第二批：多卡合售 / 拆卖组队赌卡 / 整盒签字
        "各年份 Panini Intrigue 上镜 阴谋 编年史系列等 新秀RC 一图打包",
        "2024-25 elevencard Barcelona team set refractor Soccer 原箱拆卖保一编博大",
        "【LA拍卖】2025-26 Topps Signature Class Cooper Flagg 签字盒 新秀RC",
        "panini 组队 拼包 保一编",
    ]
    for title in sealed_samples:
        assert is_sealed_product(title)[0], f"整包漏拦: {title}"
    # 反向：正常单卡标题绝不能被整包规则误伤
    assert not is_sealed_product(
        "2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编 #AT-SKA")[0], \
        "正常单卡被整包规则误伤"
    # 「带原盒」只是包装描述，卡本身还是单张，不能当整盒拦掉
    assert not is_sealed_product(
        "2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25 带原盒")[0], \
        "「带原盒」把单卡误判成整盒"
    # 「运费可累计」是合邮说明，不是多件合售
    assert not is_sealed_product(
        "2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25 运费可累计")[0], \
        "「运费可累计」把单卡误判成多卡合售"
    # 方括号店铺标签【可累计】同样是合邮缩写，单卡挂牌常见，必须放行
    assert not is_sealed_product(
        "【甲子拍卖】#51【可累计】2021-22 Panini Select Luka Garza RC 新秀 /249编 银折 折射")[0], \
        "【可累计】店铺标签把单卡误判成多卡合售"
    # 多人卡语义
    assert multi_player_hit("2023-24 Panini Top Class 博新秀文班亚马 经典双人全息特卡 好人签字"), \
        "多人卡语义漏拦"
    assert not multi_player_hit("2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编"), \
        "单人卡被多人卡规则误伤"
    # (d) 黑名单走词边界：hit 不能吃掉 White Sparkle
    assert not any(word_in_text(w, "panini white sparkle prizm") for w in ["hit"]), \
        "word_in_text 边界失效"
    logger.info("✅ [自检通过] 编号解析(111/150·三级/3级) + 整包/整盒拦截 + 词边界 全部回归通过")

    # 1. 验证误报拦截测试（应当全部被拦截返回 None）
    # 英文候选词绝不拆单名 —— 「Luka」会同时命中 Luka Doncic 与 Luka Garza
    assert en_name_candidates("Luka Doncic") == ["Luka Doncic", "Doncic"], \
        "en_name 又被拆出单名了"
    assert en_name_candidates("Neymar") == ["Neymar"], "单名球员应保留原名"
    # 中文简称"假名字"守卫：命中在别人名字内部不算数
    assert not player_name_matched("德约", "凯沙德约翰逊 签字", ["德约", "德约科维奇"]), \
        "「凯沙德约翰逊」里的「德约」被当成德约科维奇"
    assert not player_name_matched("贝林", "若昂贝兰热 贝林格 森林狼", ["贝林", "贝林厄姆"]), \
        "「贝林格」被当成贝林厄姆"
    assert not player_name_matched("萨卡", "玛丽亚萨卡里 50编", ["萨卡"]), "「萨卡里」被当成萨卡"
    assert not player_name_matched("马克西", "马克西莫 阿科斯塔 RC", ["马克西"]), "「马克西莫」被当成马克西"
    # 反向：合法写法必须照常认（简称+完整名都要通）
    assert player_name_matched("文班", "文班亚马 新秀签字", ["文班", "文班亚马"]), "文班亚马被误杀了"
    assert player_name_matched("科达", "科达网球 橙折卡签", ["科达"]), "科达网球被误杀了"
    assert player_name_matched("科达", "科达 橙折卡签", ["科达"]), "科达+空格被误杀了"
    assert player_name_matched("文班", "文班的RPA", ["文班", "文班亚马"]), "文班的RPA 被误杀了"

    false_positives = [
        ({"id": "bad1", "title": "2025 帕尼尼 切尔西 利斯·詹姆斯 Reece James /99 绿折", "current_price": 15}, cfg_map["埃娃"]),
        ({"id": "bad2", "title": "2026 Topps 高尔夫 奈莉·科达 Nelly Korda /25 橙签", "current_price": 50}, cfg_map["科达"]),
        ({"id": "bad3", "title": "2025-26 Topps 活塞 奥萨尔·汤普森 Ausar Thompson /50 金折", "current_price": 30}, cfg_map["阿门"]),
        ({"id": "bad4", "title": "2025-26 NBA Hoops 字母哥 /99 普通纸卡球衣物料 Patch 无签", "current_price": 20}, cfg_map["字母哥"]),
        # 单名撞车：Luka Garza 不是东契奇（曾按 ¥3780 红线算出 75 倍幻觉）
        ({"id": "bad5", "title": "【甲子拍卖】#51 2021-22 Panini Select Luka Garza 卢卡 加尔扎 RC 新秀 /249编 银折 签字",
          "current_price": 11}, cfg_map["东契奇"]),
        # 中文音译前缀撞车：亚当弗拉格勒 ≠ 弗拉格
        ({"id": "bad6", "title": "2024-25 Panini Optic Adam Flagler 亚当弗拉格勒 球票 签字 雷霆",
          "current_price": 10}, cfg_map["弗拉格"]),
        # 中文音译前缀撞车：玛丽亚萨卡里 ≠ 萨卡
        ({"id": "bad7", "title": "2026 Topps Chrome Maria Sakkari 网球 玛丽亚 萨卡里 50编 金马赛克折 签字",
          "current_price": 1}, cfg_map["萨卡"]),
        # 蹭热度：「同届 文班」写在别人的球票签字标题里
        ({"id": "bad8", "title": "2023-24 Panini Contenders Optic Dariq Whitehead RC 球票 签字 99编 同届 文班 瑕疵如图",
          "current_price": 1}, cfg_map["文班亚马"]),
        # 多人卡：一条标题挤了 4 个监控球星
        ({"id": "bad9", "title": "2023-24 Panini Top Class 博新秀文班亚马 晚安库里 詹姆斯 东契奇 欧文等经典双人全息特卡 好人签字",
          "current_price": 18}, cfg_map["文班亚马"]),
        # 中文简称吃到别人名字内部：凯沙【德约】翰逊 ≠ 德约科维奇（曾算出 272 倍）
        ({"id": "bad10", "title": "【大腿代卖】Keshad Johnson 凯沙德约翰逊 2024-25 Panini Hoops 新秀 RC 签字 签名 hot签 热火",
          "current_price": 11}, cfg_map["德约科维奇"]),
        # 蹭队友：「姆巴佩奥利塞队友」
        ({"id": "bad11", "title": "2025-26 Panini Donruss William Saliba 威廉 萨利巴 签字 11/25编 紫折 姆巴佩奥利塞队友 凑套必备",
          "current_price": 120}, cfg_map["姆巴佩"]),
        # 拆卡抽奖：买的是"有机会抽到"
        ({"id": "bad12", "title": "2023-24 Panini Prizm retail 拆文班亚马42编、88编 红波纹折射 有机会抽粉脉冲 名人签字 Manga",
          "current_price": 638}, cfg_map["文班亚马"]),
        # 多卡 lot
        ({"id": "bad13", "title": "2026 Topps Chrome Tennis 新秀 Joao Fonseca 若昂 丰塞卡 马赛克折 YouthQuake特卡 等 lot",
          "current_price": 1}, cfg_map["丰塞卡"]),
        # 截图上那张：Alexander Bublik（亚历山大·布勃利克）被当成 SGA
        ({"id": "bad14", "title": "2026 Topps Chrome Alexander Bublik 网球 亚历山大 布勃利克 150编 蓝马赛克折 折射 签字",
          "current_price": 14}, cfg_map["亚历山大"]),
        # 短中文名 + 英文标题但无本球员英文名：阿罗科达雷（Arokodare 含"科达"）
        ({"id": "bad15", "title": "2025-26 Panini Donruss Tolu Arokodare RC 托卢 阿罗科达雷 新秀 银折 世界杯之路",
          "current_price": 12}, cfg_map["科达"]),
        ({"id": "bad16", "title": "2026 Topps Chrome 网球 科达 橙折卡签 01/25首编 #AT-SKA",
          "current_price": 52}, cfg_map["科达"]),
        # 标题写的是别人的英文名：DAKA 这张是 Mbappe 的，不是贝林厄姆
        ({"id": "bad17", "title": "2026 DAKA 达咖 Kylian Mbappe 【折射之家】皇家马德里 皇马 折射签字",
          "current_price": 1}, cfg_map["贝林厄姆"]),
    ]
    for bad_item, cfg in false_positives:
        assert evaluate_auction_item_v2(bad_item, cfg) is None, f"误报拦截失败: {bad_item['title']}"
    logger.info("✅ [自检通过] 同名球星跨界防误伤 (足球利斯/高尔夫科达/活塞奥萨尔) & 纯球衣纸卡拦截 100% 生效！")
    logger.info("✅ [自检通过] 错绑拦截生效：单名撞车(Luka Garza) / 音译前缀(弗拉格勒·萨卡里) / "
                "蹭热度(同届文班) / 多人卡(4 球星并列)")
    # 反向：纯中文标题（一个英文字母都没有）**默认也不认** —— 用户明确要求
    # 「扫描逻辑以球员英文名全称为准」。要放行只能靠词库里的 allow_cn_only 开关。
    _zh_item = {"id": "cn1", "title": "【拔脚代卖】科达 橙折卡签 01/25首编", "current_price": 52,
                "bid_count": 4, "remaining_minutes": 200, "end_time_str": "21:00"}
    assert evaluate_auction_item_v2(_zh_item, cfg_map["科达"]) is None, \
        "纯中文标题没被拦，英文名为准的判定没有真正生效"

    _cn_only_cfg = dict(cfg_map["科达"], allow_cn_only=True)
    assert evaluate_auction_item_v2(_zh_item, _cn_only_cfg) is not None, \
        "allow_cn_only 开关失效：显式放行的球员仍被拦"
    logger.info("✅ [自检通过] 英文名全称为准：默认强制英文名校验，纯中文标题一律不认；"
                "allow_cn_only 可对个别球员放行")

    # ---- 0.5 同姓不同人（2026-10-20 线上实测三条，全部造成百倍级以上幻觉）----
    # Carsen Edwards 被当成 Anthony Edwards：参照 ¥5945 / 现价 ¥49 = 122 倍
    # Trey Alexander / Alexandre Pantoja 被当成 SGA：参照 ¥2012 / 现价 ¥1 = 2012 倍
    # 根因：姓氏单独匹配。COMMON_SURNAMES 里的大姓只认全名/首名。
    edw, sga = cfg_map["爱德华兹"], cfg_map["亚历山大"]
    homonyms = [
        (edw, "Carsen Edwards",
         "【LA拍卖】2019-20 Panini Encased Carsen Edwards 云端 新秀RC 卡森 爱德华兹 "
         "凯尔特人 Scripted 卡签签字 /99编 金标BGS9.5分 完美墨10分 K30D"),
        (sga, "Trey Alexander",
         "2024-25 Panini Origins Trey Alexander RC 特雷 亚历山大 新秀 起源 卡签 签字 掘金 未来可期"),
        (sga, "Alexandre Pantoja",
         "2026 Topps Midnight Alexandre Pantoja UFC 亚历山大 潘多哈 前蝇量级冠军 "
         "多次卫冕腰带 非卡签签字 小窗物料 自拆厚卡"),
    ]
    for cfg, who, title in homonyms:
        assert not en_name_verified(cfg, title.lower()), \
            f"「{who}」的卡被当成了 {cfg['keyword']}（英文校验放行）"
        assert evaluate_auction_item_v2(
            {"id": "hm", "title": title, "current_price": 49, "bid_count": 3,
             "remaining_minutes": 200, "end_time_str": "21:00"}, cfg) is None, \
            f"「{who}」的卡混进了 {cfg['keyword']} 名单"

    # 反向：本人真卡必须照常通过，不能"修得过火"把自家卡也拦了
    assert en_name_verified(edw, "2024-25 panini prizm anthony edwards 安东尼 爱德华兹 银折"), \
        "Anthony Edwards 全名被大姓规则误杀"
    assert en_name_verified(sga, "2024-25 panini prizm shai gilgeous-alexander 亚历山大 银折"), \
        "SGA 全名被大姓规则误杀"
    assert en_name_verified(sga, "2024-25 prizm sga 亚历山大 银折"), \
        "SGA 缩写被 require_en 误杀"
    logger.info("✅ [自检通过] 同姓不同人拦截：Carsen Edwards / Trey Alexander / "
                "Alexandre Pantoja 全部拦掉；本人全名与 SGA 缩写不受影响")

    # ---- 0.6 兑换板（redemption）必须按非实物折价 ----
    # 线上：约基奇 Pristine /5编 绰号签 Personal Endorsements「交换板」
    # 被当实物估出参照 ¥16675。这里验证系数真的落到了 ebay_ref 上。
    red_title = ("2025-26 Topps Pristine Nikola Jokic 太古 约基奇 掘金 /5编 绰号签 "
                 "铭文签字 卡签 Personal Endorsements交换板")
    assert REDEMPTION_RE.search(red_title), "兑换板关键词没认出来"
    _red_item = {"id": "red1", "title": red_title, "current_price": 2900, "bid_count": 8,
                 "remaining_minutes": 200, "end_time_str": "21:00"}
    _plain_item = dict(_red_item, id="red2", title=red_title.replace("交换板", ""))
    jokic_red = evaluate_auction_item_v2(_red_item, cfg_map["约基奇"])
    jokic_plain = evaluate_auction_item_v2(_plain_item, cfg_map["约基奇"])
    assert jokic_plain is not None, "兑换板对照组没算出结果，测试用例失效"
    assert jokic_red is not None, "兑换板被整条拦掉了（应该折价保留，而不是丢弃）"
    assert jokic_red["redemption_flag"] is True, "兑换板标记没写进结果"
    assert jokic_red["ebay_ref_rmb"] < jokic_plain["ebay_ref_rmb"], "兑换板没有折价"
    ratio = jokic_red["ebay_ref_rmb"] / max(jokic_plain["ebay_ref_rmb"], 1)
    assert abs(ratio - REDEMPTION_FACTOR) < 0.02, f"兑换板折价系数异常 {ratio:.2f}"
    logger.info("✅ [自检通过] 兑换板折价生效：同档实物参照 ¥%s → 兑换板 ¥%s（×%.2f）",
                jokic_plain["ebay_ref_rmb"], jokic_red["ebay_ref_rmb"], ratio)

    # ---- 0.8 Base 普卡 & 卖家货号（2026-10-20 线上截图）----
    # 标题：…世界杯 Base …/16985编(8/45)
    #   「16985编」是卖家货号，不是限量编号；随后的 (8/45) 是批次备注。
    #   原代码把它读成 8/45 编 → 归入「/50 金折低编平行」档 → 参照 ¥1015 / 现价 ¥16。
    #   Prizm 世界杯 Base 平卡真实价约 ¥20，等于凭空造出 17 倍假漏。
    _ym_cfg = cfg_map["亚马尔"]
    _base_title = ("【大赚委卖】Lamine Yamal 亚马尔 2026 Panini Prizm World Cup PZ "
                   "世界杯 Base 巴塞罗那 巴 西班牙 天才少年 世界杯冠军/崭露头角见闻录/16985编(8/45)")
    assert parse_serial_details(_base_title)[0] is None, "卖家货号 16985编 又被当成限量编号"
    assert is_base_plain_card(_base_title), "Base 普卡没认出来"
    assert evaluate_auction_item_v2(
        {"id": "base1", "title": _base_title, "current_price": 16, "bid_count": 5,
         "remaining_minutes": 120, "end_time_str": "21:47"}, _ym_cfg) is None, \
        "Base 普卡被算进了捡漏池"

    # 反向：真·金折必须照常估价，不能被 Base 规则误杀
    _gold = evaluate_auction_item_v2(
        {"id": "gold1", "title": "2026 Prizm World Cup Lamine Yamal 亚马尔 金折 08/45编",
         "current_price": 16, "bid_count": 5, "remaining_minutes": 120,
         "end_time_str": "21:47"}, _ym_cfg)
    assert _gold is not None and _gold["tier_key"] == "p_35_50", \
        "真·金折被 Base 规则误杀：%s" % (None if not _gold else _gold["tier_key"])

    # 中高编但认不出颜色 → 只能按最低档，不能给「金折」的价
    _unknown = evaluate_auction_item_v2(
        {"id": "unk1", "title": "2026 Prizm World Cup Lamine Yamal 亚马尔 /99编",
         "current_price": 16, "bid_count": 5, "remaining_minutes": 120,
         "end_time_str": "21:47"}, _ym_cfg)
    assert _unknown is not None and _unknown["tier_key"] == "p_250_399", \
        "颜色未明的 /99 编没有被压低到最低档"
    assert "颜色未明" in _unknown["tier_desc"], "档位描述没说实话"

    # 但 /25 以下极低编不受影响：稀缺性独立于颜色，压到最低档属于矫枉过正
    _low_serial = evaluate_auction_item_v2(
        {"id": "low1", "title": "2026 Prizm World Cup Lamine Yamal 亚马尔 /25编",
         "current_price": 16, "bid_count": 5, "remaining_minutes": 120,
         "end_time_str": "21:47"}, _ym_cfg)
    assert _low_serial is not None and _low_serial["tier_key"] == "p_15_25", \
        "极低编被「颜色未明」规则误压到最低档"

    # 「残卡折价 / 折叠痕」里的「折」不是折射特征
    assert not has_parallel_feature("亚马尔 角损 残卡折价 /25编"), "「折价」被当成了折射特征"
    assert not has_parallel_feature("亚马尔 折叠痕 /25编"), "「折叠痕」被当成了折射特征"
    assert has_parallel_feature("亚马尔 银折 /99编"), "银折没被认出是平行"
    logger.info("✅ [自检通过] Base 普卡拦截 / 卖家货号剥离 / 颜色未明按最低档 / "
                "「折价·折痕」不冒充折射")

    # ---- 0.8b 卖家货号尾巴上的 `/1` 不能当 /1 编（2026-10-23 线上截图）----
    # 标题：…Topps Chrome Updates Base 天光折射 马刺队/瑕疵见 详图/17217老水母/1
    # 17217 是卖家货号，末尾那个 `/1` 被「只有分母」的规则吃掉 →
    # 一张 Base 天窗折射拿到了「/10 及以下极低编折射」的价（参照 ¥1344 / 红线 ¥480）。
    _wenban_title = ("【大脚代卖】Victor Wembanyama 维克托文班亚马 2025-26 Topps Chrome "
                     "Updates Base 天光折射 天窗折射 马刺队/瑕疵见 详图/17217老水母/1")
    assert parse_serial_details(_wenban_title)[0] is None, \
        "卖家货号尾巴的 /1 又被当成 /1 编：%s" % (parse_serial_details(_wenban_title),)
    # 分母写 1 的分数没有意义（1 编只会写 1/1 / one of one），一律不认
    assert parse_serial_details("科达 银折 /1")[0] is None, "孤立的 /1 不该当分母"
    assert parse_serial_details("科达 1/1 唯一编")[0] == 1, "1/1 必须是唯一编"
    logger.info("✅ [自检通过] 卖家货号尾巴 `/1` 不再冒充 /1 编，1/1 仍正确识别")

    # ---- 0.8c 「签字」是卖家最爱堆的词：特卡冒充签字要降档 ----
    # 线上真实案例：Topps Finest 的 Prizmed Footballers 特卡，标题写「新秀 RC 签字」，
    # 实物没有签名 —— 一张几百块的特卡被按「基础无编签字」估到 ¥2414。
    _fake_auto_title = ("【天秤座代卖预付85%】2023-24 Topps Finest 欧冠 Lamine Yamal 拉明 亚马尔 "
                        "巴塞罗那 巴 西班牙 新秀 RC 签字 银折 Prizmed Footballers 特卡 /50编")
    _ym_no_learn = dict(_ym_cfg)
    _ym_no_learn.pop("_non_auto", None)
    _ym_learned = dict(_ym_cfg, _non_auto=["prizmed"])
    _probe_auto = {"id": "fa1", "title": _fake_auto_title, "current_price": 124,
                   "bid_count": 18, "remaining_minutes": 120, "end_time_str": "20:00"}
    _r_trust = evaluate_auction_item_v2(dict(_probe_auto), _ym_no_learn)
    _r_learn = evaluate_auction_item_v2(dict(_probe_auto), _ym_learned)
    assert _r_trust is not None and _r_trust["tier_basis"] == "auto", \
        "自检前提不成立：未学习时应按签字轨"
    assert _r_learn is not None and _r_learn["tier_basis"] != "auto", \
        "学到的非签字系列没有把签字轨降下来"
    assert _r_learn["ebay_ref_rmb"] < _r_trust["ebay_ref_rmb"], \
        "降档后参照价居然没变低：%s -> %s" % (_r_trust["ebay_ref_rmb"], _r_learn["ebay_ref_rmb"])
    # 无编号的纯特卡：降档后折射轨也接不住（折射轨必须有编或 Case Hit），
    # 结果就是"不再报价"。这比给一个签字价安全得多 —— 宁可漏，不可错。
    _r_noserial = evaluate_auction_item_v2(
        dict(_probe_auto, title=_fake_auto_title.replace(" /50编", "")), _ym_learned)
    assert _r_noserial is None, "无编号特卡降档后居然还在报价：%s" % (_r_noserial,)
    # 「卡签 / autograph」是强证据，哪怕系列被标了非签字也要按签字估
    _strong_auto = evaluate_auction_item_v2(
        {"id": "fa2", "title": _fake_auto_title.replace("签字", "卡签 autograph"),
         "current_price": 124, "bid_count": 18, "remaining_minutes": 120,
         "end_time_str": "20:00"}, _ym_learned)
    assert _strong_auto is not None and _strong_auto["tier_basis"] == "auto", \
        "「卡签/autograph」被非签字名单误降级"
    logger.info("✅ [自检通过] 特卡冒充签字降档（¥%s → ¥%s），无编号特卡直接不报价，"
                "卡签/autograph 不被误降",
                _r_trust["ebay_ref_rmb"], _r_learn["ebay_ref_rmb"])

    # ---- 0.8d 人工判定「档位偏高」必须真的乘上去 ----
    # 用户原话：「我很多时候只能选档位偏高」—— 它原来只是个记录项，
    # 点了跟没点一样，那才是最伤信任的。现在每次 ×0.9，地板 0.60。
    _pen_cfg = dict(_ym_cfg, _tier_penalty={"p_35_50": 0.81})
    _pen_base = dict(_ym_cfg)
    _pen_base.pop("_tier_penalty", None)
    _pen_probe = {"id": "pen1", "title": "2026 Prizm World Cup Lamine Yamal 亚马尔 金折 08/45编",
                  "current_price": 16, "bid_count": 5, "remaining_minutes": 120,
                  "end_time_str": "21:47"}
    _r_pen0 = evaluate_auction_item_v2(dict(_pen_probe), _pen_base)
    _r_pen1 = evaluate_auction_item_v2(dict(_pen_probe), _pen_cfg)
    assert _r_pen0 and _r_pen1, "人工判高自检的试样被整条拦掉了"
    assert abs(_r_pen1["ebay_ref_rmb"] - round(_r_pen0["ebay_ref_rmb"] * 0.81)) <= 1, \
        "人工判高没乘到参照价上：%s -> %s" % (_r_pen0["ebay_ref_rmb"], _r_pen1["ebay_ref_rmb"])
    assert "人工判高" in (_r_pen1.get("bonus_str") or ""), "网页没标出这是人工下调的"
    logger.info("✅ [自检通过] 人工判「档位偏高」×0.81 真的生效：¥%s → ¥%s",
                _r_pen0["ebay_ref_rmb"], _r_pen1["ebay_ref_rmb"])


    # ---- 0.7 普通 insert 不能冒充 Case Hit ----
    # Prizm 的 Aces 是 ~1:8 包的普通特卡，套 ¥490~820 箱货参照价就是凭空造漏。
    assert "aces" not in CASE_HIT_KEYWORDS, "aces 又被当成 Case Hit 了"
    logger.info("✅ [自检通过] Aces 等普通 insert 不再套用 Case Hit 箱货参照价")

    # ---- 0.9 全局折价兜底（2026-10-20 首批 4 笔真实成交的教训）----
    # 实测成交 / 参照 = 0.21 / 0.23 / 0.33 / 0.43 —— 参照价系统性虚高 2~5 倍。
    # 但每个(球员|档位)只有 1 笔样本，tier 级校准（门槛 3 笔）永远等不到，
    # 于是"越学越准"变成了空话。全局因子跨档位合并，专门兜这个死结。
    _g = compute_global_factor([0.21, 0.23, 0.33, 0.43])
    assert _g["factor"] == CALIB_GLOBAL_FLOOR, "全局折价没有落到地板上：%s" % _g
    # 只许下调：哪怕实测成交普遍高于参照，也不能把参照价往上抬
    assert compute_global_factor([1.2, 1.3, 1.4, 1.5])["factor"] == 1.0, \
        "全局折价居然允许上调"
    assert compute_global_factor([0.99, 1.0, 1.0, 1.01])["factor"] <= 1.0, \
        "全局折价越过了 1.0 上限"
    # 样本不足一律不动，避免 2 笔成交就敢改全站参照价
    assert compute_global_factor([0.2, 0.3])["factor"] is None, "样本不足时不该生效"

    # 端到端：全局因子必须真的乘到参照价与红线上，且档位级因子优先
    _ym_plain = dict(_ym_cfg)
    _ym_plain.pop("_calib", None)
    _ym_glob = dict(_ym_cfg, _calib={CALIB_GLOBAL_KEY: 0.5})
    _ym_tier = dict(_ym_cfg, _calib={CALIB_GLOBAL_KEY: 0.5,
                                     "亚马尔|p_35_50": 0.9})
    _probe = {"id": "cal1", "title": "2026 Prizm World Cup Lamine Yamal 亚马尔 金折 08/45编",
              "current_price": 16, "bid_count": 5, "remaining_minutes": 120,
              "end_time_str": "21:47"}
    _r0 = evaluate_auction_item_v2(dict(_probe), _ym_plain)
    _r1 = evaluate_auction_item_v2(dict(_probe), _ym_glob)
    _r2 = evaluate_auction_item_v2(dict(_probe), _ym_tier)
    assert _r0 and _r1 and _r2, "校准自检的试样被整条拦掉了，检查阈值"
    assert abs(_r1["ebay_ref_rmb"] - round(_r0["ebay_ref_rmb"] * 0.5)) <= 1, \
        "全局折价没乘到参照价上：%s -> %s" % (_r0["ebay_ref_rmb"], _r1["ebay_ref_rmb"])
    assert abs(_r2["ebay_ref_rmb"] - round(_r0["ebay_ref_rmb"] * 0.9)) <= 1, \
        "档位级因子没有优先于全局因子：%s" % _r2["ebay_ref_rmb"]
    logger.info("✅ [自检通过] 全局折价只降不升 / 样本门槛生效 / 档位级因子优先于全局")

    # 2. 模拟 17:00 摸底扫描并写入 SQLite
    mock_1700 = [
        (
            {
                "id": "test_99001",
                "title": "【拔脚代卖】2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编 #AT-SKA",
                "current_price": 52.0,
                "bid_count": 4,
                "remaining_minutes": 210,
                "end_time_str": "20:30"
            },
            cfg_map["科达"]
        ),
        (
            {
                "id": "test_99002",
                "title": "【拔脚代卖】2026 Topps Chrome Tennis 埃娃·利斯 Eva Lys 绿折卡签 18/99编 #AT-ELS",
                "current_price": 45.0,
                "bid_count": 2,
                "remaining_minutes": 225,
                "end_time_str": "20:45"
            },
            cfg_map["埃娃"]
        ),
        (
            {
                "id": "test_99003",
                "title": "【战斧代卖】2025-26 Topps Chrome 字母哥 Giannis Daily Dribble 橙折特卡 12/25编 PSA 10",
                "current_price": 210.0,
                "bid_count": 6,
                "remaining_minutes": 240,
                "end_time_str": "21:00"
            },
            cfg_map["字母哥"]
        )
    ]

    for item, cfg in mock_1700:
        res = evaluate_auction_item_v2(item, cfg)
        if res:
            record_and_compare_snapshot(res["item_id"], "1700", res["title"], res["current_price"], res["bid_count"], res["remaining_minutes"])

    # 3. 模拟 19:20 终极扫描（对比 17:00 价格异动 + 同店合并运费）
    mock_1920 = [
        (
            {
                "id": "test_99001",
                "title": "【拔脚代卖】2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编 #AT-SKA",
                "current_price": 52.0,  # 价格未动，冷门死水
                "bid_count": 4,
                "remaining_minutes": 70,
                "end_time_str": "20:30"
            },
            cfg_map["科达"]
        ),
        (
            {
                "id": "test_99002",
                "title": "【拔脚代卖】2026 Topps Chrome Tennis 埃娃·利斯 Eva Lys 绿折卡签 18/99编 #AT-ELS",
                "current_price": 62.0,  # 温和上涨 17元，与科达同属【拔脚代卖】触发合并运费
                "bid_count": 5,
                "remaining_minutes": 85,
                "end_time_str": "20:45"
            },
            cfg_map["埃娃"]
        ),
        (
            {
                "id": "test_99003",
                "title": "【战斧代卖】2025-26 Topps Chrome 字母哥 Giannis Daily Dribble 橙折特卡 12/25编 PSA 10",
                "current_price": 235.0,  # PSA 10 高评壳自动上浮红线
                "bid_count": 8,
                "remaining_minutes": 100,
                "end_time_str": "21:00"
            },
            cfg_map["字母哥"]
        )
    ]

    steals_1920 = []
    for item, cfg in mock_1920:
        res = evaluate_auction_item_v2(item, cfg)
        if res:
            snap = record_and_compare_snapshot(res["item_id"], "1920", res["title"], res["current_price"], res["bid_count"], res["remaining_minutes"])
            res.update(snap)
            steals_1920.append(res)

    enrich_same_seller_shipping(steals_1920)
    logger.info(f"✅ [自检通过] 19:20 终极名单共命中 {len(steals_1920)} 张真漏，详细评估结果：")
    for s in steals_1920:
        logger.info(
            f"  -> [{s['sport']}] {s['player_cn']} | {s['tier_desc']} | 加成: {s['bonus_str']}\n"
            f"     当前价: ¥{s['current_price']:.0f} | 单张建议埋价: ≤¥{s['safe_snipe_bid']} | 同店合邮埋价: ≤¥{s['combined_snipe_bid']} | eBay基准: ¥{s['ebay_ref_rmb']}\n"
            f"     盘口追踪: {s['trend_tag']}\n"
            f"     合邮提示: {re.sub('<[^<]+?>', '', s['shipping_tip'])}"
        )

    # 4. 准漏分级（v3 新增）：价格略高于红线但仍在 120% 容差内
    #
    # 红线与运费都随词库版本变化，所以不写死价格，而是从「真漏」那条推算出来——
    # 否则换个 watchlist.json，这段自检就会莫名其妙地红。
    probe = evaluate_auction_item_v2(
        {
            "id": "test_99004",
            "title": "【拔脚代卖】2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编 #AT-SKA",
            "current_price": 52.0,
            "bid_count": 3,
            "remaining_minutes": 200,
            "end_time_str": "21:00"
        },
        cfg_map["科达"]
    )
    assert probe is not None, "准漏分级前置的盗漏样例失效"
    red_line = float(probe["max_buy_rmb"])
    ship = float(probe["shipping_rmb"])
    near_price = round(max(red_line * 1.05 - ship, 1.0), 1)

    near_res = evaluate_auction_item_v2(
        {
            "id": "test_99004",
            "title": "【拔脚代卖】2026 Topps Chrome 网球 科达 Sebastian Korda 橙折卡签 01/25首编 #AT-SKA",
            "current_price": near_price,
            "bid_count": 5,
            "remaining_minutes": 200,
            "end_time_str": "21:00"
        },
        cfg_map["科达"]
    )
    assert near_res is not None and near_res["opportunity_type"] == "near_miss", \
        f"准漏分级失效（红线 ¥{red_line:.0f}，试样价 ¥{near_price:.0f}）"
    logger.info("✅ [自检通过] 准漏分级生效：红线 ¥%.0f，试样价 ¥%.0f 超出红线仍以「准漏」保留（还差 ¥%.0f）",
                red_line, near_price, near_res["gap_rmb"])

    # 5. 「起拍观察」必须真的便宜（线上事故：起拍 ¥40000 的 Eminence 康宁汉姆也进了观察区）
    #
    # 收益地板（BLIND_MIN_PROFIT_RMB）只跟红线与估值有关，与现价无关；
    # 所以这里把地板临时压到 1 元，单独验证“起拍价闸门”这一层逻辑本身。
    _saved_floor = BLIND_MIN_PROFIT_RMB
    try:
        globals()["BLIND_MIN_PROFIT_RMB"] = 1.0
        blind_title = ("【拔脚代卖】2026 Topps Chrome 网球 科达 Sebastian Korda "
                       "橙折卡签 01/25首编 #AT-SKA")
        cheap = evaluate_auction_item_v2(
            {"id": "test_99005", "title": blind_title, "current_price": 1.0,
             "bid_count": 0, "remaining_minutes": 200, "end_time_str": "21:00"},
            cfg_map["科达"])
        assert cheap is not None and cheap["opportunity_type"] == "blind", \
            "¥1 起拍 0 出价的真漏被丢了"

        # 起拍价超过红线 50%（BLIND_MAX_START_RATIO）→ 只是没人要，不是漏
        pricey = evaluate_auction_item_v2(
            {"id": "test_99006", "title": blind_title,
             "current_price": round(red_line * (BLIND_MAX_START_RATIO + 0.3)),
             "bid_count": 0, "remaining_minutes": 200, "end_time_str": "21:00"},
            cfg_map["科达"])
        assert pricey is None, \
            f"起拍 ¥{round(red_line * (BLIND_MAX_START_RATIO + 0.3))} 高于红线 ¥{red_line:.0f} 仍进观察区"
    finally:
        globals()["BLIND_MIN_PROFIT_RMB"] = _saved_floor
    logger.info("✅ [自检通过] 起拍观察价格闸门生效：红线 ¥%.0f，起拍价 > ¥%.0f 一律不入观察区",
                red_line, red_line * BLIND_MAX_START_RATIO)

    send_notifications(steals_1920, mode_label="【测试·请勿当真】19:20 算法自检演练", max_hours=3.0)


def run_daemon_loop(run_on_start: bool = False) -> None:
    logger.info("[DAEMON] 群晖 Docker 守护进程已启动！每日 17:00 (摸底) 与 19:20 (终极名单) 各扫描一次。")

    # 记录"今天已经跑过的槽位"，跨天自动失效
    done = {"date": "", "1700": False, "1920": False}

    if run_on_start:
        # 容器新起或 NAS 重启后，先跑一轮摸底建立盘口基线，避免 19:20 全是"晚间新入窗口"
        logger.info("[DAEMON] --run-on-start 已启用，立即执行一轮 1700 摸底作为基线。")
        try:
            run_radar_scan("1700")
            # 只有「重启时刻已经过了 17:00」才把槽位记成已完成 —— 那种情况下紧接着的循环
            # 会发现 hm >= "17:00" 而立刻再扫一遍，等于每次重启白烧两轮 68 人的流量。
            #
            # 反过来，凌晨重启绝不能把当天的 17:00 记成已完成：那会让今晚的摸底轮
            # 被静默跳过，而"今晚没有待抢名单"在手机上看起来就像"今天没漏"。
            # （2026-10-25 凌晨 02:08 重启时真实踩到：当晚 17:00 会被跳过。）
            if _now_cn().strftime("%H:%M") >= "17:00":
                done = {"date": _today_cn(), "1700": True, "1920": False}
        except Exception as exc:
            logger.error(f"[DAEMON] 启动摸底失败（不影响后续定时）: {exc}")

    while True:
        try:
            # 网页端手动刷新优先级最高：用户就站在那儿等，先跑它再跑定时
            if handle_rescan_request():
                continue

            # 其次是 Card Vault 的「刷新库存行情」请求（用户同样在等）
            if handle_vault_quote_request():
                continue

            now = _now_cn()
            date_key = now.strftime("%Y-%m-%d")
            hm = now.strftime("%H:%M")

            if done["date"] != date_key:
                done = {"date": date_key, "1700": False, "1920": False}

            # 用 >= 判断而非 ==，这样容器在 17:00 之后才起来也能补跑当天的摸底
            if not done["1700"] and hm >= "17:00" and hm < "19:20":
                done["1700"] = True
                logger.info(f"[DAEMON] 触发 17:00 摸底（实际触发时刻 {hm}）")
                try:
                    run_radar_scan("1700")
                except Exception as exc:
                    logger.error(f"[DAEMON] 1700 扫描失败: {exc}")
                # 摸底轮收工、19:20 还早，正是刷新库存行情的唯一好窗口
                try:
                    maybe_run_scheduled_vault_quote_scan()
                except Exception as exc:
                    logger.error(f"[DAEMON] 库存行情定时扫描失败: {exc}")

            if not done["1920"] and hm >= "19:20":
                done["1920"] = True
                logger.info(f"[DAEMON] 触发 19:20 终极名单（实际触发时刻 {hm}）")
                try:
                    run_radar_scan("1920")
                except Exception as exc:
                    logger.error(f"[DAEMON] 1920 扫描失败: {exc}")

        except Exception as exc:
            # 守护循环绝不能因单轮异常而退出
            logger.error(f"[DAEMON] 调度循环异常: {exc}")

        time.sleep(20)


def run_probe(keyword: str = "科达") -> None:
    """真实抓取链路探针：不写快照、不推送，只验证接口是否还活着。

    这是本项目最重要的自检 —— 算法自检全是 mock 数据，
    接口一旦失效，算法再对也只是每天安静地不推送。
    """
    logger.info(f"[PROBE] 开始真实抓取链路验证，关键词='{keyword}'")
    rows = _request_search_page(keyword, 1)
    logger.info(f"[PROBE] 第 1 页原始返回 {len(rows)} 条")
    if not rows:
        logger.error("[PROBE] ❌ 接口返回空 —— 可能已被改版/限流/需要登录态。请立即排查！")
        return

    now = _now_cn()
    sample = rows[:5]
    logger.info("[PROBE] 前 5 条原始字段抽样：")
    for r in sample:
        logger.info("  ID=%s | Price=%s | PriceCount=%s | EffectiveDate=%s | Seller=%s | Postage=%s",
                    r.get("ID"), r.get("Price"), r.get("PriceCount"),
                    r.get("EffectiveDate"), r.get("SellRealName"), r.get("NewPostageMoney"))
        logger.info("    Title=%s", str(r.get("Title"))[:80])

    # 校验时间基准：距现在多久截标，若整体偏差约 8 小时说明时区错位
    dt = _parse_effective_date(str(rows[0].get("EffectiveDate") or ""))
    if dt:
        delta_h = (dt - now).total_seconds() / 3600.0
        logger.info("[PROBE] 首条距截标 %.2f 小时（本机时间 %s）", delta_h,
                    now.strftime("%Y-%m-%d %H:%M:%S"))
        if delta_h > 12 or delta_h < -12:
            logger.error("[PROBE] ❌ 时间基准疑似错位，请检查容器 TZ 设置！")

    parsed = parse_raw_item_list(rows, max_hours=24)
    logger.info("[PROBE] 24h 窗口内可解析条目 %d 条", len(parsed))
    logger.info("[PROBE] ✅ 抓取链路正常" if parsed else "[PROBE] ⚠️ 可抓但 24h 内无条目")


def _bt_read_jsonl(path: str, limit: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return out
    return out[-limit:] if limit else out


def _bt_item_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """把流水记录还原成 evaluate_auction_item_v2 需要的最小 item 结构。"""
    return {
        "id": str(rec.get("id") or rec.get("item_id") or ""),
        "title": str(rec.get("title") or ""),
        "current_price": float(rec.get("current_price") or 0),
        "start_price": float(rec.get("start_price") or rec.get("lowest_price") or 0),
        "lowest_price": float(rec.get("lowest_price") or 0),
        "bid_count": int(rec.get("bid_count") or 0),
        "out_count": int(rec.get("out_count") or 0),
        "seller": str(rec.get("seller_name") or rec.get("seller") or ""),
        "seller_review": int(rec.get("seller_review") or 0),
        "seller_source": str(rec.get("seller_source") or ""),
        "shipping_rmb": float(rec.get("shipping_rmb") or DEFAULT_SHIPPING_RMB),
        "remaining_minutes": int(rec.get("remaining_minutes") or 0),
        "end_time_str": str(rec.get("end_time_str") or ""),
        "end_time_full": str(rec.get("end_time_full") or ""),
    }


def run_backtest() -> int:
    """离线回测：不发一个网络请求、不发一条推送，用**当前代码**重跑历史候选。

    回答两个问题：
      1. 你在网页上标过的那些误判，现在的规则拦住了多少？（拦截率 + 漏网标题）
      2. 相比上一次回测，这次多拦了谁、又放宽了谁？（规则改动的真实代价）

    为什么要专门做这个：识别层的规则是"宁可漏不可错"，每次收紧都会**顺带**
    拦掉一些本来该放行的卡。没有回测，这种代价永远看不见 ——
    下次再收紧时你就没有依据了。
    """
    watchlist = load_watchlist()
    if not watchlist:
        print("词库为空，无法回测。")
        return 1
    cfg_map = {c.get("keyword", ""): c for c in watchlist}
    try:
        rules = rebuild_learned_rules(watchlist)
        apply_learned_rules(watchlist, rules)
    except Exception as exc:                      # noqa: BLE001
        print("学习规则加载失败（继续用裸词库回测）: %s" % exc)

    # ---- 1. 历史候选池 ----
    src_path = RAW_CANDIDATES_PATH if os.path.exists(RAW_CANDIDATES_PATH) else HITS_JSONL_PATH
    src_name = os.path.basename(src_path)
    records = _bt_read_jsonl(src_path)
    pool: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        iid = str(rec.get("id") or rec.get("item_id") or "").strip()
        if iid and iid not in pool:
            pool[iid] = rec

    print("=" * 78)
    print("离线回测 · 数据源 %s（%d 行，去重后 %d 条候选）" % (src_name, len(records), len(pool)))
    print("=" * 78)
    if not pool:
        print("没有历史候选可回放。先让雷达正常跑一轮，raw_candidates.jsonl 会自动积累。")
        return 1

    # ---- 2. 用当前规则重跑 ----
    passed: Dict[str, bool] = {}
    titles: Dict[str, str] = {}
    kw_of: Dict[str, str] = {}
    n_skip_cfg = 0
    for iid, rec in pool.items():
        kw = str(rec.get("player_kw") or rec.get("player_cn") or "").strip()
        cfg = cfg_map.get(kw)
        title = str(rec.get("title") or "")
        titles[iid] = title
        kw_of[iid] = kw
        if not cfg:
            n_skip_cfg += 1
            continue
        try:
            res = evaluate_auction_item_v2(_bt_item_from_record(rec), cfg)
        except Exception:                         # noqa: BLE001
            res = None
        passed[iid] = bool(res)

    n_pass = sum(1 for v in passed.values() if v)
    print("重跑完成：放行 %d 条 / 拦截 %d 条（另有 %d 条因球员已不在词库而跳过）"
          % (n_pass, len(passed) - n_pass, n_skip_cfg))

    # ---- 3. 历史反馈的拦截率 ----
    fb = load_feedback()
    blocked_ok = 0
    missed: List[str] = []
    unreplayable = 0
    for rec in fb:
        iid = str(rec.get("item_id") or "").strip()
        if not iid:
            continue
        if iid not in passed:
            unreplayable += 1
            continue
        if passed[iid]:
            missed.append("%s | %s | 反馈：%s" % (
                rec.get("player_cn", ""), (rec.get("title") or titles.get(iid, ""))[:64],
                rec.get("reason", "")))
        else:
            blocked_ok += 1
    replayable = blocked_ok + len(missed)
    print("\n[1] 历史误判回放（feedback.jsonl 共 %d 条）" % len(fb))
    if replayable:
        print("    当前规则成功拦截 %d/%d = %.1f%%"
              % (blocked_ok, replayable, blocked_ok * 100.0 / replayable))
    else:
        print("    没有可回放的误判记录（候选池里找不到对应 item_id）")
    if unreplayable:
        print("    %d 条无法回放：候选池里没有这些 item_id（早于 raw_candidates 启用）"
              % unreplayable)
    if missed:
        print("    仍未拦住（这些是真实漏网的误判，逐条看标题找规律）：")
        for line in missed[:30]:
            print("      ✗ " + line)
        if len(missed) > 30:
            print("      …… 另有 %d 条" % (len(missed) - 30))
    else:
        print("    ✓ 全部拦住")

    # ---- 4. 与上次回测对比 ----
    prev = _load_json_safe(BACKTEST_REPORT_PATH, {})
    prev_pass = set(prev.get("passed_ids") or [])
    prev_ts = prev.get("generated_at") or "（无历史记录）"
    now_pass_ids = {iid for iid, ok in passed.items() if ok}
    new_blocked = sorted(prev_pass - now_pass_ids)
    new_passed = sorted(now_pass_ids - prev_pass)

    print("\n[2] 与上次回测对比（上次：%s）" % prev_ts)
    if not prev:
        print("    首次回测，无对比基线。已保存本次结果供下次对比。")
    else:
        print("    新增拦截 %d 条（规则收紧的代价：这些卡以后不会再报）" % len(new_blocked))
        for iid in new_blocked[:20]:
            print("      − [%s] %s" % (kw_of.get(iid, ""), titles.get(iid, iid)[:66]))
        if len(new_blocked) > 20:
            print("      …… 另有 %d 条" % (len(new_blocked) - 20))
        print("    新增放行 %d 条（规则放宽的效果）" % len(new_passed))
        for iid in new_passed[:20]:
            print("      + [%s] %s" % (kw_of.get(iid, ""), titles.get(iid, iid)[:66]))
        if len(new_passed) > 20:
            print("      …… 另有 %d 条" % (len(new_passed) - 20))

    _atomic_write_json(BACKTEST_REPORT_PATH, {
        "generated_at": _now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src_name,
        "candidates": len(pool),
        "passed": n_pass,
        "feedback_total": len(fb),
        "feedback_blocked": blocked_ok,
        "feedback_missed": len(missed),
        "passed_ids": sorted(now_pass_ids),
    })
    print("\n结果已写入 %s（下次 --backtest 会拿它做对比基线）" % BACKTEST_REPORT_PATH)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="卡淘跨市场捡漏扫描雷达 v3.1（真实接口版）")
    parser.add_argument(
        "--mode",
        choices=["1700", "1920", "manual", "daemon", "self-check", "probe",
                 "vault-quotes"],
        default="self-check",
        help="1700(下午摸底) | 1920(晚间终极名单) | manual(单次手动刷新,不推送) | "
             "daemon(常驻定时+轮询网页刷新请求) | self-check(算法自检,不抓网) | "
             "probe(真实抓取链路验证) | vault-quotes(只给档案库在库卡抓行情)"
    )
    parser.add_argument("--keyword", default="科达", help="probe 模式使用的中文关键词")
    parser.add_argument("--run-on-start", action="store_true",
                        help="daemon 模式启动时立即跑一轮摸底建立基线")
    parser.add_argument("--backtest", action="store_true",
                        help="离线回测：用当前规则重跑本地历史候选，不发请求、不推送")
    args = parser.parse_args()

    if args.backtest:
        # 回测必须走独立出口：不建库、不抓网、不推送
        sys.exit(run_backtest())
    if args.mode == "self-check":
        run_self_check()
    elif args.mode == "probe":
        run_probe(args.keyword)
    elif args.mode == "daemon":
        run_daemon_loop(run_on_start=args.run_on_start)
    elif args.mode == "vault-quotes":
        run_vault_quote_scan(reason="cli")
    else:
        run_radar_scan(args.mode)