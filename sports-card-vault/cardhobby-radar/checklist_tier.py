# -*- coding: utf-8 -*-
"""雷达侧「Checklist 真值优先」的 tier_key 映射器（只读，零依赖）。

解决的问题
----------
雷达扫卡淘标题时，`tier_key` 一直靠正则从标题里猜：编数靠 `/(\\d+)`，
签字靠「签 / auto / 卡签」这些词，平行版本靠一张关键词表。三种猜法都会错——
卖家把 `/10` 写在国名后面、把「金折」写成「金色折射」、把签字卡写成「墨迹」，
都会让同一张卡落进不同档位，于是 market_base 的同族梯度折算开始拿错样本。

做法
----
先用 `checklists` 表（人工校对过的球员 / 卡号 / 平行阶梯）在标题里做**最长匹配**，
命中就按表里的结构化事实定档；未收录的标题才退回原来的正则兜底。
两种情况返回同一份结构，调用方不需要写分支。

对外接口
--------
    idx = ChecklistTierIndex("/app/data/cards.db")
    res = idx.map_title("2025-26 Topps Chrome Kasparas Jakucionis RC ... 23/50编 金折签字")
    res["tier_key"]   # "s_35_50"；无法定档时为空串
    res["tier_desc"]  # "/50金折/低编签"
    res["tier_basis"] # "auto" / "parallel" / "base_auto" / "case_hit"
    res["source"]     # "checklist"（真值命中）或 "regex"（兜底）

    map_tier_key(title, db_path) -> str   # 只要一个键时的便捷函数

设计约束
--------
* **只读**：只用 SELECT，绝不写 cards.db（雷达是另一个容器，写库会打架）。
* **不抛异常**：库打不开、表不存在、列缺失都降级为正则兜底，雷达不能因为
  Card Vault 侧没升级就停摆。
* **不猜档位**：算出的键必须落在 market_base.TIER_ORDER 的已知集合里，
  否则返回空串由调用方保留原判定——错的档位比没档位更伤估值。
"""
import json
import logging
import os
import re
import sqlite3
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request

# 与 market_base.TIER_ORDER 的键完全一致；雷达写入 closed_fills 的就是这套
ALLOWED_TIER_KEYS = {
    "p_1_10", "s_1_10", "p_15_25", "s_15_25",
    "p_35_50", "s_35_50", "p_75_99", "s_75_99",
    "p_125_199", "s_125_199", "s_250_399",
    "base_auto", "case_hit",
}

# 编数分母 → 区间标签（区间必须和 TIER_ORDER 的命名对齐）
SERIAL_BUCKETS = (
    (1, 10, "1_10"),
    (11, 25, "15_25"),
    (26, 50, "35_50"),
    (51, 99, "75_99"),
    (100, 199, "125_199"),
    (200, 399, "250_399"),
)

# 标题里的编数写法：/99、/ 99、99编、6/10编、1/1、one of one
_SERIAL_RES = (
    re.compile(r"/\s*(\d{1,4})"),
    re.compile(r"(\d{1,4})\s*编"),
)
_ONE_OF_ONE_RE = re.compile(r"(one\s*of\s*one|1\s*of\s*1)", re.I)

# 正则兜底用的签字线索
_AUTO_WORDS = ("签字", "卡签", "亲签", "直签", "墨迹", "auto", "autograph", "签名")

# 正则兜底用的平行线索（顺序即优先级：越具体越靠前）
_PARALLEL_WORDS = (
    ("superfractor", "Superfractor"), ("黑折", "黑折"), ("black", "黑折"),
    ("金折", "金折"), ("gold", "金折"), ("红折", "红折"), ("red", "红折"),
    ("银折", "银折"), ("silver", "银折"), ("紫折", "紫折"), ("purple", "紫折"),
    ("绿折", "绿折"), ("green", "绿折"), ("蓝折", "蓝折"), ("blue", "蓝折"),
    ("橙折", "橙折"), ("orange", "橙折"), ("折射", "折射"), ("refractor", "折射"),
)

_NORM_RE = re.compile(r"[\s\-_·•・.,，、/|()（）\[\]【】]+")


def _norm(text):
    """折叠变音符 + 去分隔符 + 小写（Dončić→Doncic 才能对上）。"""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _NORM_RE.sub("", s).lower()


def _col(row, name, default=""):
    """安全取列：老库可能还没跑 CHECKLIST_MIGRATIONS，缺列不能炸。"""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[name] if name in keys else default


def _pretty_parallel(label):
    """把阶梯原文洗成版本名：`Gold /10 (金折)` → 金折；`Gold /50 金折签字` → 金折。

    版本名会直接进成交过滤条件，带英文/编数/「签字」后缀只会让匹配更窄，
    所以这里统一归一到中文版本词（归一失败就保留原文，不丢信息）。
    """
    text = str(label or "").strip()
    if not text:
        return ""
    m = re.search(r"[（(]([^）)]+)[）)]", text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"/\s*\d{1,4}\s*(\S.*)$", text)
    if m and m.group(1).strip():
        candidate = m.group(1).strip()
        return _parallel_from_title(candidate) or candidate
    return _parallel_from_title(text) or text


def _bucket_label(den):
    """分母 → 区间标签；超出表范围返回 None。"""
    for lo, hi, label in SERIAL_BUCKETS:
        if lo <= den <= hi:
            return label
    return None


def _serial_from_title(title):
    """从标题里抽编数分母；抽不到或抽到 1/1 时按 1 处理（1/1 是最高档）。"""
    if not title:
        return None
    if _ONE_OF_ONE_RE.search(title):
        return 1
    for pattern in _SERIAL_RES:
        for m in pattern.finditer(title):
            try:
                den = int(m.group(1))
            except (TypeError, ValueError):
                continue
            # /2026 这种是产品年份不是限量，跳过
            if 1900 <= den <= 2099:
                continue
            if 1 <= den <= 3999:
                return den
    return None


def _is_auto_title(title):
    low = str(title or "").lower()
    return any(word in low for word in _AUTO_WORDS)


def _parallel_from_title(title):
    low = str(title or "").lower()
    for word, label in _PARALLEL_WORDS:
        if word in low:
            return label
    return ""


class ChecklistTierIndex:
    """checklists 表的只读内存索引，用于标题 → (球员, 卡号, 版本) 的真值匹配。"""

    def __init__(self, db_path, auto_reload=True):
        # db_path 既可以是 cards.db（直接查表），也可以是 Card Vault 导出的
        # checklists_truth.json（跨容器推荐用法：雷达拿不到别人的库文件）。
        self.db_path = db_path
        self.source = db_path
        self.auto_reload = auto_reload
        self._mtime = 0
        # alias(normalized) -> row dict；按 alias 长度降序，保证「卢卡·东契奇」
        # 先于「东契奇」命中，也保证「Jayson Tatum」先于「Tatum」
        self._players = []
        self._rows = []
        self.reload()

    # ---------------------------- 加载 ----------------------------
    def reload(self):
        """重新读数据源。文件不存在/表缺失一律降级为空索引（正则兜底继续可用）。"""
        self._players, self._rows = [], []
        self._mtime = self._current_mtime()
        try:
            rows = self._load_rows()
        except Exception:          # 这里是「绝不打断雷达」的兜底，任何异常都降级
            rows = []

        alias_map = {}
        for row in rows:
            item = {
                "player_en": _col(row, "player_en"),
                "player_cn": _col(row, "player_cn"),
                "card_number": str(_col(row, "card_number") or "").strip(),
                "series": _col(row, "series"),
                "year": _col(row, "year"),
                "subset": _col(row, "subset"),
                "is_auto": int(_col(row, "is_auto", 0) or 0),
                "serial_max": _col(row, "serial_max", None),
                "parallels": self._parse_parallels(_col(row, "parallels")),
                "canonical_query_cn": _col(row, "canonical_query_cn"),
            }
            self._rows.append(item)

            for name in (item["player_cn"], item["player_en"]):
                name = (name or "").strip()
                if not name:
                    continue
                hits = [name]
                # 中文译名按分隔点拆出姓氏（卢卡·东契奇 → 东契奇）
                for sep in ("·", "•", "・"):
                    if sep in name:
                        tail = name.split(sep)[-1].strip()
                        if len(tail) >= 2:
                            hits.append(tail)
                # 英文全名拆出姓氏（Kasparas Jakucionis → Jakucionis）
                if re.search(r"[A-Za-z]", name) and " " in name:
                    tail = name.split()[-1].strip()
                    if len(tail) >= 4:
                        hits.append(tail)
                for alias in hits:
                    key = _norm(alias)
                    if len(key) >= 2 and key not in alias_map:
                        alias_map[key] = item

        self._players = sorted(alias_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        return self

    @staticmethod
    def _parse_parallels(raw):
        if not raw:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in data]

    # ---------------------------- 数据源 ----------------------------
    def _current_mtime(self):
        try:
            return os.path.getmtime(self.source)
        except (OSError, TypeError, ValueError):
            return 0

    def refresh_if_changed(self):
        """文件变了就重载（雷达是长驻进程，不能等重启才看到新入库的卡表）。"""
        now = self._current_mtime()
        if now and now != self._mtime:
            self.reload()
        return self

    def _load_rows(self):
        src = str(self.source or "")
        if src.lower().endswith(".json"):
            return self._rows_from_json(src)
        return self._rows_from_sqlite(src)

    @staticmethod
    def _rows_from_json(path):
        """读 Card Vault 导出的 checklists_truth.json（{"items":[...]} 或裸数组）。"""
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("items") or []
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def _rows_from_sqlite(self, path):
        try:
            conn = sqlite3.connect(path, timeout=3)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        try:
            try:
                return conn.execute(
                    "SELECT player_en, player_cn, card_number, series, year, subset,"
                    " is_auto, is_relic, serial_max, parallels, canonical_query_cn"
                    " FROM checklists"
                ).fetchall()
            except sqlite3.Error:
                # 老库缺 canonical_query_cn 等新列 → 退回 SELECT * ，按列名安全取值
                try:
                    return conn.execute("SELECT * FROM checklists").fetchall()
                except sqlite3.Error:
                    return []
        finally:
            conn.close()

    # ---------------------------- 匹配 ----------------------------
    def _match_title(self, title):
        """最长匹配标题里出现的球员别名，返回 (row, alias) 或 (None, None)。"""
        low = _norm(title)
        if not low:
            return None, None
        for alias, row in self._players:
            if alias and alias in low:
                return row, alias
        return None, None

    def _parallel_from_row(self, row, den):
        """按编数在平行阶梯里对号入座；撞多条就返回空（不猜）。"""
        if not den or not row.get("parallels"):
            return ""
        token = "1/1" if den == 1 else "/%d" % den
        hits = [p for p in row["parallels"] if token in p]
        if len(hits) != 1:
            return ""
        return _pretty_parallel(hits[0])

    def map_title(self, title, serial=None, player_hint="", is_auto_hint=None):
        """标题 → tier 判定。返回 dict，字段固定，调用方不需要判空分支。

        serial / player_hint / is_auto_hint 是给「雷达已经解析过一遍」的场景用的
        可选覆盖值——有就直接用，省一次正则。
        """
        title = title or ""
        den = serial
        if den is None:
            den = _serial_from_title(title)
        try:
            den = int(den) if den else None
        except (TypeError, ValueError):
            den = None

        row, alias = self._match_title(title)
        auto_flag = _is_auto_title(title) if is_auto_hint is None else bool(is_auto_hint)
        if row and row["is_auto"]:
            auto_flag = True

        # ---------- ① 真值命中：按表里的结构化事实定档 ----------
        if row:
            parallel = self._parallel_from_row(row, den) or _parallel_from_title(title)
            if not den and row.get("serial_max"):
                try:
                    den = int(row["serial_max"])
                except (TypeError, ValueError):
                    den = None

            # base_auto = 「无编数的签字卡」；有编数的签字一律走 s_ 族。
            # 早期版本直接看 subset 里有没有 auto 就判 base_auto，会把 /50 签字
            # 也降级成 base_auto，等于把 50 编签当普签估——差一个数量级。
            subset_low = str(row.get("subset") or "").lower()
            if auto_flag:
                basis = "auto" if den else ("base_auto" if "auto" in subset_low else "auto")
            else:
                basis = "parallel"

            key = self._compose_key(basis, den)
            return {
                "matched": True,
                "source": "checklist",
                "tier_key": key,
                "tier_desc": self._describe(key, den, parallel, auto_flag),
                "tier_basis": basis,
                "player": row["player_cn"] or row["player_en"],
                "player_alias": alias,
                "card_number": row["card_number"],
                "series": row["series"],
                "parallel": parallel,
                "serial_den": den,
                "canonical_query_cn": row["canonical_query_cn"],
            }

        # ---------- ② 未收录：保留原正则兜底 ----------
        parallel = _parallel_from_title(title)
        basis = "auto" if auto_flag else "parallel"
        key = self._compose_key(basis, den)
        return {
            "matched": False,
            "source": "regex",
            "tier_key": key,
            "tier_desc": self._describe(key, den, parallel, auto_flag),
            "tier_basis": basis,
            "player": player_hint or "",
            "player_alias": "",
            "card_number": "",
            "series": "",
            "parallel": parallel,
            "serial_den": den,
            "canonical_query_cn": "",
        }

    @staticmethod
    def _compose_key(basis, den):
        """拼 tier_key 并**校验是否在已知集合内**；不在就返回空串。"""
        if basis == "base_auto":
            return "base_auto" if "base_auto" in ALLOWED_TIER_KEYS else ""
        if basis == "case_hit":
            return "case_hit" if "case_hit" in ALLOWED_TIER_KEYS else ""
        if not den:
            return ""
        bucket = _bucket_label(den)
        if not bucket:
            return ""
        prefix = "s_" if basis == "auto" else "p_"
        key = prefix + bucket
        return key if key in ALLOWED_TIER_KEYS else ""

    @staticmethod
    def _describe(key, den, parallel, is_auto):
        if not key:
            return ""
        if key in ("base_auto", "case_hit"):
            return "Base 签字" if key == "base_auto" else "Case Hit"
        parts = []
        if den:
            parts.append("/%d" % den)
        if parallel:
            parts.append(parallel)
        if is_auto and den and den <= 25:
            parts.append("低编签")
        elif is_auto:
            parts.append("编签")
        return "".join(parts) if len(parts) <= 1 else "/".join(parts[:1]) + "".join(parts[1:])


def map_tier_key(title, db_path=None, index=None, **kwargs):
    """便捷函数：只要 tier_key（空串表示定不了档，调用方沿用原判定）。"""
    if index is None:
        if not db_path:
            return ""
        index = ChecklistTierIndex(db_path)
    return index.map_title(title, **kwargs)["tier_key"]


# ============================ 在库卡片回写桥 ============================


class VaultBridge:
    """雷达命中 → 在库卡片 的桥：按球员名把标题对到 card_id，再把档位写回 Card Vault。

    设计取舍：
    * 只按**球员名**做匹配，不按标题相似度。"同一球员两张不同卡"是常态，
      标题相似度匹配一定会写错卡；球员对不上就宁可不回写。
    * 同一个 (card_id, tier_key, parallel) 在 min_interval 秒内只推一次：
      雷达是循环扫的，不节流会把 Card Vault 的接口刷穿。
    * 全程吞异常：回写失败只是「档位没更新」，绝不能让雷达主流程崩。
    """

    def __init__(self, vault_path, api_base="", timeout=6, min_interval=300):
        self.vault_path = vault_path
        self.api_base = (api_base or "").strip().rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self._mtime = 0
        self._cards = []
        self._aliases = []
        self._pushed = {}
        self.reload()

    def reload(self):
        self._cards, self._aliases = [], []
        try:
            self._mtime = os.path.getmtime(self.vault_path)
        except (OSError, TypeError, ValueError):
            self._mtime = 0
        try:
            with open(self.vault_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return self
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return self

        alias_map = {}
        for card in items:
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("card_id") or "").strip()
            if not card_id:
                continue
            self._cards.append(card)
            player = str(card.get("player") or "").strip()
            if not player:
                continue
            hits = [player]
            for sep in ("·", "•", "・"):
                if sep in player:
                    tail = player.split(sep)[-1].strip()
                    if len(tail) >= 2:
                        hits.append(tail)
            if re.search(r"[A-Za-z]", player) and " " in player:
                tail = player.split()[-1].strip()
                if len(tail) >= 4:
                    hits.append(tail)
            for alias in hits:
                key = _norm(alias)
                if len(key) >= 2 and key not in alias_map:
                    alias_map[key] = card
        self._aliases = sorted(alias_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        return self

    def refresh_if_changed(self):
        try:
            now = os.path.getmtime(self.vault_path)
        except (OSError, TypeError, ValueError):
            return self
        if now != self._mtime:
            self.reload()
        return self

    def match_card(self, title):
        """标题 → 在库卡片 dict；对不上返回 None。"""
        low = _norm(title)
        if not low:
            return None
        for alias, card in self._aliases:
            if alias and alias in low:
                return card
        return None

    def push(self, card_id, tier_key, tier_desc="", tier_basis="",
             parallel="", card_number="", is_auto=None):
        """把档位写回 Card Vault。返回 dict（含 status），失败不抛。"""
        if not self.api_base:
            return {"status": "disabled"}
        if not card_id or not tier_key:
            return {"status": "skipped"}
        dedup_key = "%s|%s|%s" % (card_id, tier_key, parallel or "")
        now = time.time()
        last = self._pushed.get(dedup_key)
        if last and (now - last) < self.min_interval:
            return {"status": "throttled"}

        payload = {"tier_key": tier_key}
        if tier_desc:
            payload["tier_desc"] = tier_desc
        if tier_basis:
            payload["tier_basis"] = tier_basis
        if parallel:
            payload["parallel"] = parallel
        if card_number:
            payload["card_number"] = card_number
        if is_auto is not None:
            payload["is_auto"] = "1" if is_auto else "0"

        url = "%s/api/cards/%s/tier" % (self.api_base, urllib.parse.quote(str(card_id), safe=""))
        try:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            self._pushed[dedup_key] = now
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            return {"status": "ok", "changed": parsed.get("changed", [])}
        except urllib.error.HTTPError as exc:
            return {"status": "http_%d" % exc.code, "url": url}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"status": "error", "error": str(exc), "url": url}


# ============================ 雷达模块安装器 ============================


def _ns_get(namespace, name, default=None):
    if isinstance(namespace, dict):
        return namespace.get(name, default)
    return getattr(namespace, name, default)


def _ns_set(namespace, name, value):
    if isinstance(namespace, dict):
        namespace[name] = value
    else:
        setattr(namespace, name, value)


def install_v35_bridge(namespace, truth_path=None, vault_path=None, api_base=None,
                       index=None, bridge=None, logger=None):
    """把「真值优先判档 + 命中在库卡片回写」装进雷达模块。

    namespace 传雷达模块的 globals() 或模块对象均可。做法是包一层
    evaluate_auction_item_v2：先让原逻辑算出结果，再用真值表覆盖 tier 三件套，
    命中在库卡片时异步回写 /api/cards/{card_id}/tier。原有正则兜底完全保留。

    幂等：重复调用只返回状态，不会套两层 wrapper。
    绝不抛异常：装不上就返回 status=error，雷达照旧跑。
    """
    log = logger or logging.getLogger("checklist_tier")
    try:
        orig = _ns_get(namespace, "evaluate_auction_item_v2")
        if orig is None:
            return {"status": "error", "error": "模块里没有 evaluate_auction_item_v2"}
        if getattr(orig, "_v35_wrapped", False):
            return {"status": "already"}

        idx = index
        if idx is None:
            if truth_path:
                idx = ChecklistTierIndex(truth_path)
            else:
                db_path = os.getenv("CHECKLIST_DB") or ""
                idx = ChecklistTierIndex(db_path) if db_path else None

        brg = bridge
        if brg is None and vault_path:
            brg = VaultBridge(vault_path, api_base=api_base or "")

        stats = {"checked": 0, "override": 0, "push_ok": 0, "push_fail": 0}

        def wrapper(item, player_cfg):
            hit = orig(item, player_cfg)
            if not isinstance(hit, dict):
                return hit
            stats["checked"] += 1
            title = str(hit.get("title") or (item or {}).get("title") or "")

            try:
                if idx is not None:
                    idx.refresh_if_changed()
                    verdict = idx.map_title(title)
                    if verdict.get("source") == "checklist" and verdict.get("tier_key"):
                        hit["tier_key"] = verdict["tier_key"]
                        if verdict.get("tier_desc"):
                            hit["tier_desc"] = verdict["tier_desc"]
                        if verdict.get("tier_basis"):
                            hit["tier_basis"] = verdict["tier_basis"]
                        hit["tier_source"] = "checklist"
                        stats["override"] += 1
                    else:
                        hit.setdefault("tier_source", "regex")
            except Exception as exc:      # 判档失败退回原结果
                log.warning("checklist 判档异常，沿用正则结果：%s", exc)

            try:
                if brg is not None:
                    brg.refresh_if_changed()
                    card = brg.match_card(title)
                    if card and hit.get("tier_key"):
                        result = brg.push(
                            card.get("card_id"), hit["tier_key"],
                            tier_desc=hit.get("tier_desc", ""),
                            tier_basis=hit.get("tier_basis", ""),
                            parallel=hit.get("parallel") or card.get("parallel", ""),
                            card_number=hit.get("card_number") or card.get("card_number", ""),
                            is_auto=1 if str(hit.get("tier_basis")) == "auto" else None,
                        )
                        if result.get("status") == "ok":
                            stats["push_ok"] += 1
                        elif result.get("status") not in ("throttled", "disabled", "skipped"):
                            stats["push_fail"] += 1
                            log.info("档位回写失败(%s)：%s", result.get("status"), card.get("card_id"))
            except Exception as exc:
                stats["push_fail"] += 1
                log.warning("档位回写异常：%s", exc)

            return hit

        wrapper._v35_wrapped = True
        wrapper.stats = stats
        _ns_set(namespace, "evaluate_auction_item_v2", wrapper)
        log.info("checklist 真值判档已接入（truth=%s, vault=%s, api=%s）",
                 truth_path or "-", vault_path or "-", api_base or "-")
        return {"status": "installed", "truth_path": truth_path,
                "vault_path": vault_path, "api_base": api_base, "stats": stats}
    except Exception as exc:
        log.warning("checklist 判档接入失败，雷达按原逻辑运行：%s", exc)
        return {"status": "error", "error": str(exc)}



if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python checklist_tier.py <cards.db> [标题...]")
        raise SystemExit(1)

    idx = ChecklistTierIndex(sys.argv[1])
    samples = sys.argv[2:] or [
        "2025-26 Topps Chrome Kasparas Jakucionis RC 【小町拍卖】 23/50编 金折签字 卡签",
        "2023-24 Panini Prizm Victor Wembanyama 136 RC 银折",
        "2019-20 Panini National Giannis Antetokounmpo Peerless签字 25/25编 尾编",
        "某个不在库里的球员 1/1 超级折",
    ]
    for s in samples:
        r = idx.map_title(s)
        print("[%s] %-10s %-28s %s" % (r["source"], r["tier_key"] or "-",
                                       r["tier_desc"] or "-", s[:46]))
