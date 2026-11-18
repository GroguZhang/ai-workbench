# -*- coding: utf-8 -*-
"""Topps 官方发售与抽签监控雷达 (ToppsDropRadar)

监控渠道：
  1. 微信端：微信小程序「Topps收藏卡」与微信公众号「TOPPS中国」的官方抽签、放货、预约与发售预告
  2. 淘宝端：淘宝/天猫「TOPPS官方旗舰店」线上抽签与新品上架/补货

核心能力：
  - 搜狗微信全网发售推文智能监听 (涵盖 TOPPS中国、球星卡发售、拆卡情报等全圈子渠道)
  - 严格指纹去重与状态持久化 (topps_radar_state.json, topps_drops.json)
  - DeepSeek LLM + 正则启发式双轨结构化信息抽取 (精准提取卡盒系列名、发售渠道、原价、套利溢价、截止时间)
  - 多通道紧急预警 (PushPlus S级红色渐变发售卡片、Bark 警报级锁屏高优提醒、企业微信群机器人)
  - 自动集成于群晖 Docker 守护进程 (cardhobby-radar daemon) 周期巡检 (默认每 15 分钟)
"""
import os
import sys
import io
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import logging
from typing import List, Dict, Any, Tuple, Optional

# 修复 Windows 控制台 UTF-8 打印编码
if sys.stdout and sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except Exception:
        pass

logger = logging.getLogger("ToppsDropRadar")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [ToppsDropRadar] %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 关注的 Topps 官方关键词与查询组合
QUERIES = [
    "TOPPS 抽签",
    "Topps收藏卡 抽签",
    "TOPPS中国 抽签",
    "TOPPS 官方 发售",
    "TOPPS官方旗舰店 抽签",
    "TOPPS 官方 放货",
]

# 必须包含的正向动作触发词
ACTION_KEYWORDS = [
    "抽签", "放货", "发售", "预约", "上新", "开签", "补货", "原价", "抢购", "中签", "资格", "入场"
]

# 必须涉及的球星卡/卡盒品类词
PRODUCT_KEYWORDS = [
    "盒", "箱", "hobby", "jumbo", "mega", "value", "chrome", "ucc", "欧冠", "英超",
    "nba", "f1", "mlb", "网球", "王权", "旗舰", "蓝宝石", "sapphire", "卡牌", "收藏卡",
    "球星卡", "卡盒", "原盒", "bowman", "finest", "merlin", "flagship"
]

# 强排除的纯噪音 (完全无关领域)
EXCLUDE_KEYWORDS = [
    "演唱会", "电影票", "景区", "门票代抢", "泡泡玛特门店"
]

# =============================================================================
# 系列监控白名单与通知阈值
#   · 白名单为空  = 监控全部系列（保持上线时的原有行为）
#   · 阈值填 0    = 该条阈值不生效（同样保持原有行为）
#   · 全部可通过 .env 覆盖，改策略不用改代码
# =============================================================================

# 系列归一化表：左边是标准 key，右边是各种叫法（中英文/昵称都要能认出来）
SERIES_SYNONYMS = {
    "chrome": ["chrome"],
    "sapphire": ["sapphire", "蓝宝石", "蓝宝"],
    "bowman": ["bowman", "波曼"],
    "merlin": ["merlin", "梅林"],
    "finest": ["finest"],
    "flagship": ["flagship", "旗舰"],
    "ucc": ["ucc", "欧冠"],
    "monarch": ["monarch", "王权"],
    "sterling": ["sterling", "斯特林"],
    "pristine": ["pristine"],
    "cosmic": ["cosmic", "宇宙"],
    "museum": ["museum", "博物馆"],
    "dynasty": ["dynasty", "王朝"],
    "inception": ["inception"],
    "tribute": ["tribute"],
    "definitive": ["definitive"],
    "tier_one": ["tier one", "tierone", "第一梯队"],
    "allen_ginter": ["allen & ginter", "allen ginter", "a&g", "艾伦金特"],
    "topps_now": ["topps now"],
    "chrome_black": ["chrome black"],
    "gpk": ["gpk"],
    "match_attax": ["match attax", "matchattax"],
}

# 溢价 / 利润信号词（命中即说明这盒有套利空间）
PREMIUM_WORDS = ["溢价", "利润", "四位数", "五位数", "套利", "倒挂", "稳赚", "翻倍"]

# 稀缺 / 首发信号词
SCARCITY_WORDS = ["首发", "限量", "独家", "仅限", "首卖", "1of1", "one of one", "1/1"]


def _parse_series_list(raw: str) -> set:
    """把 "chrome,sapphire,蓝宝石" 这种配置解析成规范化后的系列集合。

    认不出来的词（用户自己新加、代码里还没收录的系列）原样保留成小写字面量，
    后面的子串匹配仍然能命中，不需要改代码。
    """
    out = set()
    for tok in re.split(r"[,，;；|\s]+", (raw or "").strip()):
        tok = tok.strip().lower()
        if not tok:
            continue
        canon = None
        for key, aliases in SERIES_SYNONYMS.items():
            if tok == key or tok in aliases:
                canon = key
                break
        out.add(canon or tok)
    return out


def _parse_number(raw, cast=float, default=0):
    """宽松解析数字配置：空串、非法值都退回默认值，绝不让配置炸掉整个巡检。"""
    try:
        if raw is None or str(raw).strip() == "":
            return default
        return cast(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


# 价格抽取：原价4649 / ¥4,649 / 售价：￥1200
_RMB_RE = re.compile(
    r"(?:原价|发售价|售价|价格|定价|¥|￥)\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,]{1,7}(?:\.[0-9]+)?)"
)


def load_env_dict() -> Dict[str, str]:
    """从多个潜在位置加载 .env 变量字典。"""
    env_vars = dict(os.environ)
    search_paths = [
        "/app/.env",
        ".env",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    ]
    for p in search_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip()
                            if k not in env_vars or not env_vars[k]:
                                env_vars[k] = v
                break
            except Exception:
                pass
    return env_vars


class ToppsDropRadar:
    """Topps 官方放货与抽签监控雷达。"""

    def __init__(self, data_dir: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        if env is None:
            env = load_env_dict()
        self.env = env
        
        # 确定数据存储目录
        if data_dir:
            self.data_dir = data_dir
        elif os.path.exists("/app/data"):
            self.data_dir = "/app/data"
        else:
            self.data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(self.data_dir, exist_ok=True)

        self.state_file = os.path.join(self.data_dir, "topps_radar_state.json")
        self.drops_file = os.path.join(self.data_dir, "topps_drops.json")

        # 推送凭证
        self.pushplus_token = env.get("PUSHPLUS_TOKEN", "").strip()
        self.bark_url = env.get("BARK_URL", "").strip()
        self.wechat_work_webhook = env.get("WECHAT_WORK_WEBHOOK", "").strip()

        # AI 抽取凭据 (支持 DeepSeek)
        self.deepseek_key = env.get("DEEPSEEK_API_KEY", "").strip()
        self.deepseek_base = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        self.deepseek_model = env.get("DEEPSEEK_MODEL", "deepseek-chat").strip()

        # ---- 系列白名单与通知阈值（全部留空/0 = 维持原有全量推送行为）----
        # 例：TOPPS_SERIES_WHITELIST=chrome,sapphire,蓝宝石,bowman
        self.series_whitelist = _parse_series_list(env.get("TOPPS_SERIES_WHITELIST", ""))
        # 低于该优先级分不推送（0 = 不卡分）
        self.min_score = _parse_number(env.get("TOPPS_MIN_SCORE", "0"), int, 0)
        # 官方原价超过该值不推送（0 = 不限价）
        self.max_official_price = _parse_number(env.get("TOPPS_MAX_OFFICIAL_PRICE", "0"), float, 0.0)
        # 只在距截止 N 小时内才提醒（0 = 不卡时间窗口）
        self.deadline_within_hours = _parse_number(
            env.get("TOPPS_DEADLINE_WITHIN_HOURS", "0"), float, 0.0)

        # 最近一轮扫描的门禁明细，供 CLI / 守护进程日志查看
        self.last_scan_report: Dict[str, Any] = {"scanned": 0, "alerted": 0, "suppressed": []}

        self._state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取状态文件失败，重建新状态: {e}")
        return {"seen_articles": {}, "last_check_ts": 0}

    def _save_state(self) -> None:
        try:
            # 维护历史条目数上限（保留最近 300 条），防止无限膨胀
            if len(self._state.get("seen_articles", {})) > 400:
                items = sorted(self._state["seen_articles"].items(), key=lambda x: x[1])
                self._state["seen_articles"] = dict(items[-300:])
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态文件失败: {e}")

    def _save_active_drops(self, drops: List[Dict[str, Any]]) -> None:
        try:
            with open(self.drops_file, "w", encoding="utf-8") as f:
                json.dump(drops, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存活动列表失败: {e}")

    # =========================================================================
    # 抓取与探测模块
    # =========================================================================
    def search_sogou_articles(self, query: str) -> List[Dict[str, Any]]:
        """从搜狗微信检索指定关键词的文章列表。"""
        url = f"https://weixin.sogou.com/weixin?type=2&query={urllib.parse.quote(query)}"
        headers = {
            'User-Agent': UA,
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://weixin.sogou.com/'
        }
        req = urllib.request.Request(url, headers=headers)
        results = []
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
                blocks = re.findall(r'<li id="sogou_vr_\d+_box_\d+".*?</li>', html, re.S)
                for b in blocks:
                    t_m = re.search(r'<h3>\s*<a[^>]*href="(/link\?url=[^"]+)"[^>]*>(.*?)</a>', b, re.S)
                    if not t_m:
                        continue
                    href, raw_title = t_m.group(1), t_m.group(2)
                    title = re.sub(r'<[^>]+>', '', raw_title).strip()
                    
                    s_m = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', b, re.S)
                    snippet = re.sub(r'<[^>]+>', '', s_m.group(1)).strip() if s_m else ""
                    
                    acc_m = re.search(r'<a class="account"[^>]*>(.*?)</a>', b, re.S) or \
                            re.search(r'id="sogou_vr_\d+_account_\d+"[^>]*>(.*?)</a>', b, re.S)
                    account = re.sub(r'<[^>]+>', '', acc_m.group(1)).strip() if acc_m else ""
                    
                    # 搜狗内嵌时间戳: document.write(timeConvert('1794236032'))
                    ts_m = re.search(r"timeConvert\('(\d+)'\)", b)
                    pub_ts = int(ts_m.group(1)) if ts_m else 0
                    pub_dt = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else "近期"
                    
                    # 搜狗跳转链接标准化
                    full_url = "https://weixin.sogou.com" + href.replace("&amp;", "&")

                    results.append({
                        "title": title,
                        "snippet": snippet,
                        "account": account,
                        "url": full_url,
                        "pub_ts": pub_ts,
                        "pub_dt": pub_dt,
                        "query": query,
                    })
        except Exception as e:
            logger.warning(f"搜狗微信查询 '{query}' 失败: {e}")
        return results

    # =========================================================================
    # 过滤与真伪鉴别模块
    # =========================================================================
    def is_valid_topps_drop(self, article: Dict[str, Any], lookback_hours: float = 72.0) -> bool:
        """评估文章是否为近期真实的 Topps 官方抽签/放货情报。"""
        title = article.get("title", "")
        snippet = article.get("snippet", "")
        combined = (title + " " + snippet).lower()

        # 1. 品牌强校验：必须包含 topps
        if "topps" not in combined:
            return False

        # 2. 必须包含抽签/放货等核心动作
        has_action = any(w in combined for w in ACTION_KEYWORDS)
        if not has_action:
            return False

        # 3. 必须涉及盒/箱/球星卡相关品类
        has_prod = any(w in combined for w in PRODUCT_KEYWORDS)
        if not has_prod:
            return False

        # 3.5 系列白名单硬过滤：白名单为空表示监控全部，非空则必须命中目标系列
        if not self.is_series_watched(combined):
            return False

        # 4. 纯噪音排除
        if any(w in title for w in EXCLUDE_KEYWORDS) and "topps" not in title.lower():
            return False

        # 5. 时间窗口限制（仅关注指定小时以内的近期情报，默认 72 小时）
        pub_ts = article.get("pub_ts", 0)
        if pub_ts > 0:
            now_ts = time.time()
            if pub_ts < now_ts - lookback_hours * 3600.0:
                return False

        return True

    # =========================================================================
    # 系列白名单 / 优先级评分（决定"要不要推送这一条"）
    # =========================================================================
    def normalize_series(self, text: str) -> set:
        """从文本中识别出标准化的系列名集合，例如「蓝宝石」→ sapphire。"""
        t = (text or "").lower()
        hit = set()
        for key, aliases in SERIES_SYNONYMS.items():
            if any(a in t for a in aliases):
                hit.add(key)
        return hit

    def is_series_watched(self, text: str) -> bool:
        """判断该情报是否落在关注的系列范围内。白名单为空 = 全部放行。"""
        if not self.series_whitelist:
            return True
        t = (text or "").lower()
        # ① 归一化命中
        if self.normalize_series(t) & self.series_whitelist:
            return True
        # ② 用户自定义、未收录进 SERIES_SYNONYMS 的系列名，按子串兜底
        return any(tok in t for tok in self.series_whitelist)

    def parse_deadline_ts(self, text: str,
                          now: Optional[datetime.datetime] = None) -> Optional[float]:
        """从文本里解析截止时刻（epoch 秒），解析不出返回 None。

        只认三种写法，宁缺毋滥：今天 14:00 / 明天 09:30 / 11月20日 14:00。
        解析不出时上层不做时间门禁，避免把真实机会误杀。
        """
        if not text:
            return None
        if now is None:
            now = datetime.datetime.now()
        t = text
        is_pm = any(w in t for w in ("下午", "晚上", "傍晚"))

        def _mk(day_offset: int, hh: int, mm: int) -> float:
            base = (now + datetime.timedelta(days=day_offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            return base.timestamp()

        for word, off in (("今天", 0), ("明天", 1), ("后天", 2)):
            m = re.search(
                word + r"\s*(?:上午|下午|晚上|中午|傍晚)?\s*([0-2]?\d)\s*[:：点]\s*([0-5]?\d)?", t)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2) or 0)
                if is_pm and hh < 12:
                    hh += 12
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    return _mk(off, hh, mm)

        m = re.search(
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"
            r"(?:\s*(?:上午|下午|晚上|中午|傍晚)?\s*([0-2]?\d)\s*[:：点]\s*([0-5]?\d)?)?", t)
        if m:
            mo, dy = int(m.group(1)), int(m.group(2))
            hh = int(m.group(3)) if m.group(3) else 23
            mm = int(m.group(4)) if m.group(4) else 59
            if m.group(3) and is_pm and hh < 12:
                hh += 12
            try:
                target = now.replace(month=mo, day=dy, hour=hh, minute=mm,
                                     second=0, microsecond=0)
            except ValueError:
                return None
            # 跨年：写的是早就过去的月份，按明年算
            if (target - now).days < -30:
                try:
                    target = target.replace(year=target.year + 1)
                except ValueError:
                    return None
            return target.timestamp()

        return None

    def _first_rmb(self, text: str) -> Optional[float]:
        """抽出文本里第一个像官方原价的数字（元）。抽不到返回 None。"""
        m = _RMB_RE.search(text or "")
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    def score_drop(self, drop: Dict[str, Any]) -> Dict[str, Any]:
        """给一条发售情报打分，供推送门禁判断。

        返回 {"score": int, "hits": [...], "blocks": [...]}：
          · blocks 非空 = 硬性拦下（系列不符 / 超预算 / 已过期 / 不在提醒窗口）
          · score 低于 TOPPS_MIN_SCORE = 软性暂缓（下一轮还会重新评估）
        """
        text = " ".join([
            str(drop.get("title") or ""),
            str(drop.get("snippet") or ""),
            str(drop.get("product_name") or ""),
        ])
        low = text.lower()
        score = 0
        hits: List[str] = []
        blocks: List[str] = []

        # 1) 系列命中
        matched = self.normalize_series(low)
        if self.series_whitelist:
            if matched & self.series_whitelist:
                score += 40
                hits.append("命中关注系列: " + "/".join(sorted(matched & self.series_whitelist)))
            else:
                blocks.append("系列不在白名单")
        elif matched:
            score += 10
            hits.append("识别系列: " + "/".join(sorted(matched)))

        # 2) 官方原价
        price = self._first_rmb(text)
        if price is not None:
            if self.max_official_price > 0 and price > self.max_official_price:
                blocks.append("原价 ¥%g 超过上限 ¥%g" % (price, self.max_official_price))
            else:
                score += 20
                hits.append("原价 ¥%g" % price)

        # 3) 官方抽签：要登记、时效最强
        if "抽签" in low:
            score += 15
            hits.append("官方抽签")

        # 4) 截止时间
        dl = self.parse_deadline_ts(text)
        if dl is not None:
            left_h = (dl - time.time()) / 3600.0
            if left_h < -1:
                blocks.append("已过截止时间")
            elif self.deadline_within_hours > 0:
                if 0 <= left_h <= self.deadline_within_hours:
                    score += 30
                    hits.append("距截止 %.1f 小时" % left_h)
                else:
                    blocks.append("距截止 %.1f 小时，不在 %.0f 小时提醒窗口"
                                  % (left_h, self.deadline_within_hours))
            else:
                score += 10
                hits.append("距截止 %.1f 小时" % left_h)

        # 5) 溢价信号
        if any(w in low for w in PREMIUM_WORDS):
            score += 15
            hits.append("含溢价信号")

        # 6) 稀缺 / 首发信号
        if any(w in low for w in SCARCITY_WORDS):
            score += 10
            hits.append("含稀缺/首发信号")

        return {"score": score, "hits": hits, "blocks": blocks}

    # =========================================================================
    # 结构化信息抽取 (AI + 正则双轨驱动)
    # =========================================================================
    def extract_drop_details(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """提取发售系列、官方价格、抽签截止时间与渠道。"""
        title = article["title"]
        snippet = article["snippet"]

        # 优先尝试通过 DeepSeek API 进行高精度结构化抽取
        if self.deepseek_key:
            ai_res = self._extract_with_deepseek(title, snippet)
            if ai_res and ai_res.get("is_topps_drop"):
                return {
                    **article,
                    "product_name": ai_res.get("product_name") or self._clean_product_name(title),
                    "drop_type": ai_res.get("drop_type") or "官方抽签/发售",
                    "channel": ai_res.get("channel") or ["微信小程序『Topps收藏卡』", "淘宝『TOPPS官方旗舰店』"],
                    "official_price": ai_res.get("official_price") or "官方发售价",
                    "profit_margin": ai_res.get("profit_margin") or "高溢价空间",
                    "deadline": ai_res.get("deadline") or "见推文说明",
                    "urgent_summary": ai_res.get("urgent_summary") or title,
                }

        # 兜底规则抽取 (Regex Heuristics)
        return self._extract_with_regex(article)

    def _extract_with_deepseek(self, title: str, snippet: str) -> Optional[Dict[str, Any]]:
        """调用 DeepSeek 提取精准字段。"""
        prompt = (
            "请分析以下球星卡发售/抽签情报，提取结构化信息：\n"
            f"标题：{title}\n"
            f"摘要：{snippet}\n\n"
            "请输出严格纯 JSON 格式，包含以下字段：\n"
            "- is_topps_drop: true/false (是否与Topps球星卡/收藏卡官方放货或抽签活动相关)\n"
            "- product_name: 具体的卡盒或系列名称 (如: 2026 Topps 网球王权系列 / 2024-25 Topps NBA旗舰Jumbo)\n"
            "- drop_type: 抽签 / 抢购 / 现货放货 / 预售\n"
            "- channel: 发售渠道列表 (如: [\"微信小程序『Topps收藏卡』\", \"淘宝『TOPPS官方旗舰店』\"])\n"
            "- official_price: 官方原价/发售价 (如: \"¥4,649\"，若无具体数字则填\"官方原价\")\n"
            "- profit_margin: 预期溢价或套利空间 (如: \"预计溢价四位数\"，若未知填\"高溢价空间\")\n"
            "- deadline: 截止时间 (如: \"11月12日 14:00\"，若未知填\"请速去查看\")\n"
            "- urgent_summary: 一句话紧急提醒 (25字以内，概括系列、渠道与时间)\n\n"
            "只输出 JSON，不要任何代码块标记或多余解释。"
        )

        headers = {
            "Authorization": f"Bearer {self.deepseek_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.deepseek_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 500
        }

        try:
            req = urllib.request.Request(
                f"{self.deepseek_base}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    lines = content.splitlines()
                    content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                return json.loads(content)
        except Exception as e:
            logger.warning(f"DeepSeek 智能抽取失败，降级到规则抽取: {e}")
            return None

    def _clean_product_name(self, raw_title: str) -> str:
        """剥离标题中的各种日期和前缀标签，返回干净的卡盒系列名。"""
        clean = re.sub(r'^(?:【.*?】|「.*?」|\[.*?\]|\d+[\.月]\d+[日号]?\s*(?:发售|线报|快报|项目)?\s*[\|丨：:]*)\s*', '', raw_title)
        clean = re.sub(r'(?:!|！|，|,|\||丨).*$', '', clean).strip()
        return clean or raw_title

    def _extract_with_regex(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """纯规则抽取兜底。"""
        title = article["title"]
        snippet = article["snippet"]
        combined = title + " " + snippet

        # 1. 抽取渠道
        channels = []
        if any(w in combined for w in ["小程序", "Topps收藏卡", "微信"]):
            channels.append("微信小程序『Topps收藏卡』")
        if any(w in combined for w in ["淘宝", "天猫", "官旗", "旗舰店"]):
            channels.append("淘宝『TOPPS官方旗舰店』")
        if not channels:
            channels = ["微信小程序『Topps收藏卡』", "淘宝『TOPPS官方旗舰店』"]

        # 2. 抽取原价
        p_match = re.search(r'(?:原价|发售价|价格|￥|¥|&yen;)\s*[:：]?\s*(?:&yen;|¥|￥)?\s*([0-9,]+(?:\.\d+)?)\s*(?:元|/盒)?', combined)
        official_price = f"¥{p_match.group(1)}" if p_match else "官方原价"

        # 3. 抽取溢价/利润
        prof_match = re.search(r'(?:预计)?(?:利润|溢价|空间|润)\s*[:：]?\s*([0-9a-zA-Z\u4e00-\u9fa5]{2,10})', combined)
        if prof_match:
            val = prof_match.group(1)
            profit_margin = f"预计溢价{val}" if "溢价" not in val and "利润" not in val else val
        else:
            profit_margin = "高溢价空间(套利推荐)"

        # 4. 抽取截止时间
        d_match = re.search(r'((?:1[0-2]|[1-9])月(?:[1-3]?[0-9])日|\b今天|\b明[天早日])\s*([0-2]?[0-9][:点][0-5]?[0-9]?)?\s*(?:截止|开奖|开签)?', combined)
        if d_match and len(d_match.group(0).strip()) > 3:
            deadline = d_match.group(0).strip()
            if not deadline.endswith("截止") and not deadline.endswith("开签"):
                deadline += " 截止"
        else:
            deadline = "见推文公告"

        # 5. 发售类型
        if "抽签" in combined:
            drop_type = "官方抽签"
        elif "现货" in combined or "放货" in combined:
            drop_type = "官方限量放货"
        elif "预售" in combined:
            drop_type = "官方预售"
        else:
            drop_type = "官方上新/发售"

        product_name = self._clean_product_name(title)

        return {
            **article,
            "product_name": product_name,
            "drop_type": drop_type,
            "channel": channels,
            "official_price": official_price,
            "profit_margin": profit_margin,
            "deadline": deadline,
            "urgent_summary": f"Topps官方开启{drop_type}，原价套利机会，速前往登记！",
        }

    # =========================================================================
    # 消息构建与分发
    # =========================================================================
    def build_pushplus_html(self, drop: Dict[str, Any]) -> str:
        """构建 PushPlus S 级红色发售通知卡片。"""
        channels_str = " · ".join(drop.get("channel", []))
        product_name = drop.get("product_name", drop.get("title", ""))
        urgent_summary = drop.get("urgent_summary", "")
        drop_type = drop.get("drop_type", "官方抽签")
        official_price = drop.get("official_price", "官方发售价")
        profit_margin = drop.get("profit_margin", "套利空间可观")
        deadline = drop.get("deadline", "见详情")
        source_account = drop.get("account") or "卡圈发售快讯"
        pub_dt = drop.get("pub_dt", "刚刚")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        article_url = drop.get("url", "https://weixin.sogou.com/")

        html = f"""
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #fee2e2; box-shadow: 0 4px 12px rgba(220, 38, 38, 0.08);">
  <!-- 头部红色渐变 Banner -->
  <div style="background: linear-gradient(135deg, #b91c1c 0%, #dc2626 50%, #ef4444 100%); color: #ffffff; padding: 18px 20px;">
    <div style="display: flex; align-items: center; justify-content: space-between;">
      <span style="font-size: 18px; font-weight: bold; letter-spacing: 0.5px;">🚨【TOPPS 官方放货/抽签紧急提醒】</span>
      <span style="background: rgba(255,255,255,0.25); font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: bold;">S级官方直发</span>
    </div>
    <div style="font-size: 13px; margin-top: 6px; opacity: 0.95;">
      监控渠道：微信小程序「Topps收藏卡」 & 淘宝「TOPPS官方旗舰店」
    </div>
  </div>

  <!-- 正文核心内容 -->
  <div style="padding: 20px;">
    <!-- 系列标题与核心亮点 -->
    <div style="background: #fef2f2; border-left: 4px solid #dc2626; border-radius: 0 8px 8px 0; padding: 12px 16px; margin-bottom: 16px;">
      <div style="font-size: 17px; font-weight: bold; color: #991b1b; margin-bottom: 4px;">
        {product_name}
      </div>
      <div style="font-size: 13px; color: #b91c1c; font-weight: 500;">
        🔥 {urgent_summary}
      </div>
    </div>

    <!-- 详细属性参数表 -->
    <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 18px;">
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 10px 0; color: #64748b; width: 28%;">🎯 发售形式</td>
        <td style="padding: 10px 0; color: #0f172a; font-weight: bold;">
          <span style="background: #fee2e2; color: #dc2626; padding: 2px 8px; border-radius: 4px; font-size: 12px;">{drop_type}</span>
        </td>
      </tr>
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 10px 0; color: #64748b;">🛒 发售渠道</td>
        <td style="padding: 10px 0; color: #0f172a; font-weight: 600;">{channels_str}</td>
      </tr>
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 10px 0; color: #64748b;">💰 官方原价</td>
        <td style="padding: 10px 0; color: #15803d; font-weight: bold; font-size: 16px;">{official_price}</td>
      </tr>
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 10px 0; color: #64748b;">📈 预期利润</td>
        <td style="padding: 10px 0; color: #b45309; font-weight: bold;">{profit_margin}</td>
      </tr>
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 10px 0; color: #64748b;">⏰ 截止/开售时间</td>
        <td style="padding: 10px 0; color: #dc2626; font-weight: bold;">{deadline}</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #64748b;">📰 信息来源</td>
        <td style="padding: 10px 0; color: #475569; font-size: 12px;">{source_account} ({pub_dt})</td>
      </tr>
    </table>

    <!-- 行动指南 -->
    <div style="background: #f8fafc; border-radius: 8px; padding: 14px; text-align: center; border: 1px dashed #cbd5e1;">
      <div style="font-size: 13px; font-weight: bold; color: #334155; margin-bottom: 8px;">
        🏃 快速行动指南：
      </div>
      <div style="font-size: 12px; color: #64748b; line-height: 1.6; margin-bottom: 12px; text-align: left;">
        1. <b>微信端</b>：打开微信搜<b>「Topps收藏卡」</b>小程序 &rarr; 首页或抽签专区参与登记；<br>
        2. <b>淘宝端</b>：打开手机淘宝搜<b>「TOPPS官方旗舰店」</b> &rarr; 进入线上抽签或关注上架；<br>
        3. <b>提示</b>：官方 MSRP 卡盒极具套利空间（通常显著低于二级市场），请尽快完成登记！
      </div>
      <a href="{article_url}" style="display: inline-block; background: #dc2626; color: #ffffff; text-decoration: none; padding: 8px 20px; border-radius: 6px; font-size: 13px; font-weight: bold;">查看发售详细推文 ↗</a>
    </div>
  </div>

  <!-- 底部说明 -->
  <div style="background: #f8fafc; padding: 10px 20px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #f1f5f9;">
    Card Vault 智能雷达 · Topps China 官方发售监控 · 发现时间：{now_str}
  </div>
</div>
"""
        return html

    def dispatch_alert(self, drop: Dict[str, Any]) -> None:
        """分发报警给 PushPlus、Bark 与企业微信。"""
        product_name = drop.get("product_name", drop.get("title", ""))
        channels_str = " / ".join(drop.get("channel", []))
        drop_type = drop.get("drop_type", "官方抽签")
        official_price = drop.get("official_price", "官方原价")
        profit_margin = drop.get("profit_margin", "套利可观")
        deadline = drop.get("deadline", "见详情")

        title = f"🚨【TOPPS {drop_type}】{product_name[:30]}"

        # 1. PushPlus 微信公众号图文卡片推送
        if self.pushplus_token:
            try:
                html_content = self.build_pushplus_html(drop)
                payload = json.dumps({
                    "token": self.pushplus_token,
                    "title": title,
                    "content": html_content,
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
                    if '"code":200' in raw.replace(" ", ""):
                        logger.info(f"✅ PushPlus 发送 Topps 发售提醒成功: {product_name[:30]}")
                    else:
                        logger.error(f"❌ PushPlus 发送被拒: {raw[:200]}")
            except Exception as e:
                logger.error(f"PushPlus 推送异常: {e}")
        else:
            logger.info("未配置 PUSHPLUS_TOKEN，跳过 PushPlus 发送。")

        # 2. Bark iPhone 锁屏强警报
        if self.bark_url:
            try:
                bark_msg = (
                    f"【{product_name}】{drop_type}开启！"
                    f"渠道：{channels_str} | 原价：{official_price} | 利润：{profit_margin} | 截止：{deadline}。"
                    f"请速去微信「Topps收藏卡」或淘宝「TOPPS官方旗舰店」！"
                )
                encoded_title = urllib.parse.quote(title)
                encoded_msg = urllib.parse.quote(bark_msg)
                # sound=alarm 产生警报强震动，level=timeSensitive
                bark_api = (
                    f"{self.bark_url.rstrip('/')}/{encoded_title}/{encoded_msg}"
                    f"?sound=alarm&group=ToppsDrop&level=timeSensitive"
                )
                with urllib.request.urlopen(bark_api, timeout=10) as resp:
                    logger.info(f"✅ Bark 推送成功: {product_name[:30]}")
            except Exception as e:
                logger.error(f"Bark 推送异常: {e}")

        # 3. 企业微信群机器人 Webhook (可选)
        if self.wechat_work_webhook:
            try:
                md_content = (
                    f"### 🚨 {title}\n"
                    f"> **系列**：{product_name}\n"
                    f"> **类型**：`{drop_type}` | **渠道**：{channels_str}\n"
                    f"> **价格**：{official_price} | **预期利润**：{profit_margin}\n"
                    f"> **截止**：<font color=\"warning\">{deadline}</font>\n"
                    f"> **线索**：{drop.get('urgent_summary', '')}\n\n"
                    f"[点击查看发售推文详情]({drop.get('url', '')})"
                )
                wx_payload = json.dumps({
                    "msgtype": "markdown",
                    "markdown": {"content": md_content}
                }).encode("utf-8")
                req = urllib.request.Request(
                    self.wechat_work_webhook,
                    data=wx_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info(f"✅ 企业微信机器人发送成功: {product_name[:30]}")
            except Exception as e:
                logger.error(f"企业微信机器人发送异常: {e}")

    # =========================================================================
    # 主流程与调度驱动
    # =========================================================================
    def scan(self, lookback_hours: float = 72.0) -> List[Dict[str, Any]]:
        """全网扫描近期 Topps 放货与抽签，返回提取并结构化后的列表。"""
        all_articles = []
        seen_titles = set()

        logger.info(f"开始扫描 Topps 官方发售情报 (回溯窗口 {lookback_hours} 小时)...")
        for q in QUERIES:
            items = self.search_sogou_articles(q)
            for it in items:
                title = it["title"]
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                if self.is_valid_topps_drop(it, lookback_hours=lookback_hours):
                    all_articles.append(it)
            time.sleep(0.5)

        logger.info(f"初筛命中 {len(all_articles)} 篇相关发售情报，正在进行结构化解析...")
        parsed_drops = []
        for art in all_articles:
            detail = self.extract_drop_details(art)
            parsed_drops.append(detail)

        # 按时间排序 (从新到旧)
        parsed_drops.sort(key=lambda x: x.get("pub_ts", 0), reverse=True)
        return parsed_drops

    def check_and_notify(self, lookback_hours: float = 72.0) -> int:
        """主执行入口：扫描 → 打分 → 达标才推送。

        系列白名单在 is_valid_topps_drop 里做硬过滤（不进入解析流程）；
        这里执行阈值门禁（原价上限 / 截止提醒窗口 / 最低优先级分）。
        被门禁暂缓的条目不写入 seen_articles，因此下一轮会重新评估——
        截止时间越近分数越高，到点自然会推出去，不会一路漏掉。
        """
        drops = self.scan(lookback_hours=lookback_hours)
        seen_articles = self._state.setdefault("seen_articles", {})
        suppressed_state = self._state.setdefault("suppressed", {})
        new_alerts = 0
        report: Dict[str, Any] = {"scanned": len(drops), "alerted": 0, "suppressed": []}

        for drop in drops:
            # 以 标题 + 发布时间 作为唯一定位指纹
            fingerprint = f"{drop['title']}_{drop.get('pub_ts', 0)}"
            if fingerprint in seen_articles:
                continue

            verdict = self.score_drop(drop)
            if verdict["blocks"] or verdict["score"] < self.min_score:
                reason = "；".join(verdict["blocks"]) or (
                    "优先级 %d < 阈值 %d" % (verdict["score"], self.min_score))
                title_short = (drop.get("title") or "")[:60]
                logger.info("⏸ 暂缓推送｜%s｜%s", reason, title_short)
                suppressed_state[fingerprint] = {
                    "ts": time.time(),
                    "score": verdict["score"],
                    "hits": verdict["hits"],
                    "blocks": verdict["blocks"],
                    "reason": reason,
                }
                report["suppressed"].append({
                    "title": drop.get("title", ""),
                    "score": verdict["score"],
                    "reason": reason,
                })
                continue

            # 通过门禁：触发多通道报警
            drop["priority_score"] = verdict["score"]
            drop["priority_hits"] = verdict["hits"]
            logger.info("🔔 发现全新 Topps 发售/抽签事件（优先级 %d｜%s）：%s",
                        verdict["score"], "、".join(verdict["hits"]) or "-", drop["title"])
            self.dispatch_alert(drop)
            seen_articles[fingerprint] = time.time()
            suppressed_state.pop(fingerprint, None)
            new_alerts += 1
            report["alerted"] += 1

        # 暂缓记录只保留最近 200 条，避免状态文件无限膨胀
        if len(suppressed_state) > 200:
            keep = sorted(suppressed_state.items(),
                          key=lambda kv: kv[1].get("ts", 0), reverse=True)[:200]
            self._state["suppressed"] = dict(keep)

        self.last_scan_report = report
        self._state["last_check_ts"] = time.time()
        self._save_state()
        self._save_active_drops(drops[:20])

        logger.info(
            f"Topps 发售扫描完成：共探测 {report['scanned']} 项，"
            f"新触发预警 {new_alerts} 条，暂缓 {len(report['suppressed'])} 条。")
        return new_alerts


# =============================================================================
# CLI 命令行调试入口
# =============================================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Topps Drop Radar CLI")
    parser.add_argument("--test", action="store_true", help="运行本地模拟自检")
    parser.add_argument("--probe", action="store_true", help="执行真实网络探测（不发送真实推送）")
    parser.add_argument("--live", action="store_true", help="执行真实扫描并发送推送")
    parser.add_argument("--lookback", type=float, default=72.0, help="回溯检查小时数 (默认 72)")
    args = parser.parse_args()

    radar = ToppsDropRadar()

    if args.test:
        print("\n=== 运行 ToppsDropRadar 本地自检 ===")
        mock_article = {
            "title": "「发售」高溢价Topps网球王权抽签开启!KDA礼盒明早开签!",
            "snippet": "Topps 官方旗舰店(线上抽签)登记截止:今天 11月10日 14:00(两个渠道同步关)入口:淘宝APP搜索「Topps官方旗舰店」 原价4649 预计溢价四位数起 小程序「Topps收藏卡」同步开启",
            "account": "球星卡发售情报",
            "url": "https://weixin.sogou.com/test",
            "pub_ts": int(time.time()),
            "pub_dt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        assert radar.is_valid_topps_drop(mock_article), "Mock 校验失败"
        detail = radar.extract_drop_details(mock_article)
        print("提取详情:\n", json.dumps(detail, ensure_ascii=False, indent=2))
        html = radar.build_pushplus_html(detail)
        print(f"生成的 PushPlus HTML 长度: {len(html)} 字符")
        assert len(html) > 500 and "Topps" in html, "HTML 生成异常"
        print("✅ 本地算法自检 100% 通过！\n")

    elif args.probe:
        print("\n=== 运行 ToppsDropRadar 真实网络探针 (不发推送) ===")
        drops = radar.scan(lookback_hours=args.lookback)
        print(f"\n共抓取到 {len(drops)} 项近期 Topps 发售/抽签：")
        for i, d in enumerate(drops, 1):
            print(f"{i}. [{d['drop_type']}] {d['product_name']}")
            print(f"   渠道: {d['channel']} | 原价: {d['official_price']} | 利润: {d['profit_margin']} | 截止: {d['deadline']}")
            print(f"   时间: {d['pub_dt']} | 来源: {d.get('account')}")
            print(f"   提要: {d.get('urgent_summary')}\n")

    elif args.live:
        print("\n=== 执行真实扫描并分发推送 ===")
        new_count = radar.check_and_notify(lookback_hours=args.lookback)
        print(f"完成，发送了 {new_count} 条推送。")

    else:
        parser.print_help()
