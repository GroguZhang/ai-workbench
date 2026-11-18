# -*- coding: utf-8 -*-
"""卡淘成交基准价引擎（独立模块，供雷达与 Card Vault 共用）。

设计依据（用户评审确认）：
  1. 绝不用 eBay 挂售价当基准 —— 实测比真实成交虚高 3.3 倍
  2. 绝不跨球员比较 —— 同一球员不同系列价差中位 6.8 倍，最大 84.7 倍
  3. 只用「同球员 + 同系列」的成交，不同版本间按系列梯度折算
  4. 剔除流拍（价<=1 且无出价），否则中位数被拉到 ¥1
  5. 分层时间窗口（7天 / 30天 / 全量），卡价一月内可波动 ±50%
  6. 出价次数决定竞争热度，从而决定预判成交价

对外接口：
    estimate(player, series, tier) -> (price, level, reason)
    预判成交价(scan_price, bid_count) -> float
    judge(player, series, tier, scan_price, bid_count) -> dict
"""
import json
import os
import statistics
import time

# ---------- 系列标准名（规则兜底，缓存优先） ----------
RULE_SERIES = [
    "national treasures", "immaculate", "flawless", "noir", "obsidian",
    "prizm", "optic", "select", "mosaic", "donruss", "hoops", "contenders",
    "chrome update", "chrome", "flagship", "finest", "sapphire", "stadium club",
    "allen & ginter", "heritage", "archives", "bowman", "merlin", "score",
    "absolute", "elite", "crown royale", "revolution", "spectra", "luxe",
    "transcendent", "pristine", "top class", "museum collection", "futera",
    "chronicles", "certified", "prestige", "limited", "origins", "signature class",
    "court kings", "impeccable", "one and one", "opulence", "graphite", "motif",
    "phoenix", "marquee", "encased", "illusions", "eminence", "gala", "status", "flux",
]

# 档位排序（数字越小越贵），用于梯度折算时选取相邻档位
TIER_ORDER = {
    "p_1_10": 1, "s_1_10": 2, "p_15_25": 3, "s_15_25": 4,
    "p_35_50": 5, "s_35_50": 6, "p_75_99": 7, "s_75_99": 8,
    "p_125_199": 9, "s_125_199": 10, "s_250_399": 11,
    "base_auto": 12, "case_hit": 13,
    # ---- v3.6：两个"非编数"档位（用户评审确认的选 A 方案）----
    # 它们不并入任何编数桶，而是各自独立成族：
    #   parallel_unnumbered = 有平行/折射特征但无编（介于 p_250_399 与 base 之间）
    #   base                = 纯 base 普卡，全库最便宜
    # 编号仍是"越小越贵"，所以放在编数桶之后、比 base_auto/case_hit 更便宜的一端。
    "parallel_unnumbered": 14, "base": 15,
}

WINDOW_7D = 7
WINDOW_30D = 30


class MarketBase:
    """基于卡淘真实成交的基准价计算器。"""

    def __init__(self, fills_path, cache_path=None):
        self.fills_path = fills_path
        self.cache_path = cache_path
        self._series_cache = {}
        self._records = []
        self._by_player_series = {}
        self._by_series_tier = {}
        self._multiplier = {}
        self._loaded_at = 0
        self.reload()

    # ---------------- 加载与清洗 ----------------
    def reload(self):
        self._series_cache = self._load_series_cache()
        self._records = self._load_records()
        self._index()
        self._multiplier = self._calc_multiplier()
        self._loaded_at = time.time()

    def _load_series_cache(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return {}

    def _load_records(self):
        out = []
        if not os.path.exists(self.fills_path):
            return out
        now = time.time()
        with open(self.fills_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue

                final = self._num(d.get("final_price"))
                bids = int(d.get("bid_count_final") or 0)

                # 剔除流拍：价<=1 且无出价
                if final <= 1 and bids == 0:
                    continue

                ts = d.get("deadline_ts") or d.get("closed_ts") or d.get("filled_ts")
                try:
                    age = (now - float(ts)) / 86400.0
                except (TypeError, ValueError):
                    age = 9999.0

                title = (d.get("title") or "").strip()
                out.append({
                    "player": (d.get("player_cn") or "").strip(),
                    "series": self.get_series(title),
                    "tier": (d.get("tier_key") or "").strip(),
                    "final": final,
                    "bids": bids,
                    "age": age,
                    "scan": self._num(d.get("price_at_scan")),
                    "bids_at_scan": int(d.get("bid_at_scan") or 0),
                })
        return out

    @staticmethod
    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def _index(self):
        self._by_player_series = {}
        self._by_series_tier = {}
        for r in self._records:
            if not r["player"] or r["series"] == "(未识别)":
                continue
            ps = (r["player"], r["series"])
            self._by_player_series.setdefault(ps, []).append(r)
            if r["tier"]:
                self._by_series_tier.setdefault((r["series"], r["tier"]), []).append(r["final"])

    def _calc_multiplier(self):
        """按扫描时出价次数，算「扫描价 -> 成交价」的预判倍数。"""
        buckets = {}
        for r in self._records:
            if r["scan"] > 0 and r["final"] > 0:
                b = r["bids_at_scan"]
                k = "0" if b == 0 else ("1-5" if b <= 5 else ("6-15" if b <= 15 else "16+"))
                buckets.setdefault(k, []).append(r["final"] / r["scan"])
        return {k: statistics.median(v) for k, v in buckets.items() if v}

    # ---------------- 系列识别 ----------------
    def get_series(self, title):
        t = (title or "").strip()
        if t and t in self._series_cache:
            s = str(self._series_cache[t]).strip().lower()
            if s and s != "unknown":
                return s
        low = t.lower()
        for s in RULE_SERIES:
            if s in low:
                return s
        return "(未识别)"

    # ---------------- 时间窗口中位数 ----------------
    @staticmethod
    def _window_median(records, days):
        vals = [r["final"] for r in records if r["age"] <= days and r["final"] > 0]
        return (statistics.median(vals), len(vals)) if vals else (None, 0)

    @staticmethod
    def _family(tier):
        """档位族群：p_ 平行/折射、s_ 签字，其余各自独立成族。"""
        t = str(tier or "")
        if t.startswith("p_"):
            return "parallel"
        if t.startswith("s_"):
            return "signature"
        return t

    def _base_by_window(self, records, min_n=3):
        """时间窗口中位数。min_n 控制最少样本数（L2 借用基准时可放宽到 1）。"""
        m7, n7 = self._window_median(records, WINDOW_7D)
        if n7 >= max(3, min_n):
            return m7, "high", "7天(n=%d)" % n7
        m30, n30 = self._window_median(records, WINDOW_30D)
        if n30 >= max(3, min_n):
            return m30, "medium", "30天(n=%d)" % n30
        vals = [r["final"] for r in records if r["final"] > 0]
        if len(vals) >= min_n:
            return statistics.median(vals), ("low" if len(vals) >= 3 else "thin"), "全量(n=%d)" % len(vals)
        return None, None, None

    # ---------------- 核心：同球员同系列估价 ----------------
    def estimate(self, player, series, tier, sanity_ref=0):
        """返回 (price, level, reason)。严格遵守不跨球员。

        sanity_ref：可选，传入旧硬编码基准价用于合理性校验。若算出的
        真实基准与它相差超过 12 倍，说明 tier_key 分类可能错配
        （实测：标题写 /10 但实际是便宜银折，被归到 p_1_10），
        此时不采信，避免把错价当真价。
        """
        price, level, reason = self._estimate_raw(player, series, tier)
        if price is not None and sanity_ref:
            ok, why = self._sanity_ok(price, sanity_ref)
            if not ok:
                return None, "SUSPECT_TIER", why
        return price, level, reason

    def _estimate_raw(self, player, series, tier):
        player = (player or "").strip()
        series = (series or "").strip()
        tier = (tier or "").strip()

        if not series or series == "(未识别)":
            return None, "NO_SERIES", "系列未识别，不估价"

        records = self._by_player_series.get((player, series))
        if not records:
            return None, "NO_PLAYER_IN_SERIES", "该球员在此系列无成交，拒绝跨球员估价"

        # L1：同球员同系列同档位
        same_tier = [r for r in records if r["tier"] == tier]
        if len(same_tier) >= 2:
            p, conf, why = self._base_by_window(same_tier)
            if p:
                return p, "L1_EXACT/%s" % conf, "同球员同系列同档位 %s" % why

        # v3.6：非编数档位（base / parallel_unnumbered）**不借编数档的价**。
        # 它们的价由「是否折射、是否签字」决定，和编数桶不同族，硬借会把
        # 一张 Base 普卡按 /250 的价估出来。样本不足就老实不估。
        if tier in ("base", "parallel_unnumbered"):
            return None, "NO_TIER_COMP", "非编数档位（%s）无同档成交，不借编数价" % tier

        # L2：同球员同系列不同版本，按系列梯度折算
        #     铁律：只能在「同族」内折算 —— p_（平行/折射，无签字）与
        #     s_（签字）是两类完全不同的卡，跨族折算会把签字价按平行价估，
        #     实测能差 5 倍以上。base_auto / case_hit 各自单独成族。
        if tier in TIER_ORDER:
            family = self._family(tier)
            target_vals = self._by_series_tier.get((series, tier)) or []
            other_tiers = [r for r in records
                           if r["tier"] and r["tier"] != tier
                           and r["tier"] in TIER_ORDER
                           and self._family(r["tier"]) == family]
            if target_vals and other_tiers:
                cands = []
                tgt_order = TIER_ORDER[tier]
                for r in other_tiers:
                    ref_vals = self._by_series_tier.get((series, r["tier"])) or []
                    if len(ref_vals) < 2:
                        continue
                    ref_med = statistics.median(ref_vals)
                    if ref_med <= 0:
                        continue
                    grad = statistics.median(target_vals) / ref_med
                    # 梯度必须落在合理区间，否则说明样本被污染
                    if not (0.2 <= grad <= 20.0):
                        continue
                    gap = abs(TIER_ORDER[r["tier"]] - tgt_order)
                    cands.append((gap, r["tier"], grad))
                if cands:
                    cands.sort(key=lambda x: x[0])
                    _, best_tier, grad = cands[0]
                    best_recs = [r for r in records if r["tier"] == best_tier]
                    p, conf, why = self._base_by_window(best_recs, min_n=1)
                    if p:
                        est = p * grad
                        return (est, "L2_VERSION/%s" % conf,
                                "同球员同系列[%s]¥%d × 同族梯度(%.2f) [%s]"
                                % (best_tier, int(p), grad, why))

        # L3：同球员同系列同档位只有 1 条 —— 用那一条（不混档位）
        if same_tier:
            p, conf, why = self._base_by_window(same_tier)
            if p:
                return p, "L3_SINGLE/%s" % conf, "同球员同系列同档位唯一样本 %s" % why

        # L4：同档位无成交，且无法梯度折算 —— 不估价
        #     绝不用「全系列中位」兜底：那会把 base 和签字混在一起，
        #     产出的数字比不给还危险。
        return None, "NO_TIER_COMP", "同球员同系列缺少该档位成交，不估价（拒绝混档）"

    def _sanity_ok(self, price, sanity_ref):
        """合理性校验：偏离旧基准超过 12 倍视为档位错配，不采信。"""
        try:
            sanity_ref = float(sanity_ref or 0)
        except (TypeError, ValueError):
            return True, ""
        if sanity_ref <= 0 or price is None or price <= 0:
            return True, ""
        ratio = sanity_ref / price
        if ratio > 12.0:
            return False, "真实基准比旧基准低 %.0f 倍，疑似档位错配（标题编数≠实际卡种）" % ratio
        if ratio < 0.08:
            return False, "真实基准比旧基准高 %.0f 倍，疑似档位错配" % (1 / ratio)
        return True, ""

    # ---------------- 预判成交价 ----------------
    def predict_final(self, scan_price, bids_at_scan):
        """用出价次数分档倍数，把扫描价换算成预判成交价。"""
        try:
            scan_price = float(scan_price or 0)
        except (TypeError, ValueError):
            return 0.0
        b = int(bids_at_scan or 0)
        k = "0" if b == 0 else ("1-5" if b <= 5 else ("6-15" if b <= 15 else "16+"))
        mult = self._multiplier.get(k, 2.0)
        return scan_price * mult

    def multiplier_table(self):
        return dict(self._multiplier)

    # ---------------- 机会判断 ----------------
    def judge(self, player, series, tier, scan_price, bids_at_scan, threshold=0.7):
        """返回机会判定。level: green / watch / pass / unknown"""
        base, basis, reason = self.estimate(player, series, tier)
        if base is None:
            return {
                "verdict": "unknown", "base": None, "predicted": None,
                "basis": basis, "reason": reason, "margin": None,
            }

        predicted = self.predict_final(scan_price, bids_at_scan)
        if predicted <= 0:
            return {
                "verdict": "unknown", "base": base, "predicted": None,
                "basis": basis, "reason": reason, "margin": None,
            }

        margin = predicted / base if base > 0 else 99
        if margin < threshold:
            verdict = "green"
        elif margin < 1.0:
            verdict = "watch"
        else:
            verdict = "pass"

        return {
            "verdict": verdict,
            "base": round(base, 1),
            "predicted": round(predicted, 1),
            "margin": round(margin, 3),
            "basis": basis,
            "reason": reason,
        }

    # ---------------- 自检 ----------------
    def stats(self):
        return {
            "records": len(self._records),
            "player_series_groups": len(self._by_player_series),
            "series_tier_groups": len(self._by_series_tier),
            "series_cache": len(self._series_cache),
            "multiplier": {k: round(v, 2) for k, v in self._multiplier.items()},
            "loaded_at": self._loaded_at,
        }


if __name__ == "__main__":
    import io
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    base = os.path.dirname(os.path.abspath(__file__))
    mb = MarketBase(
        os.path.join(base, "data", "closed_fills.jsonl"),
        os.path.join(base, "data", "series_cache.json"),
    )
    print("=== 自检 ===")
    for k, v in mb.stats().items():
        print("  %-22s %s" % (k, v))

    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("\n=== 估价测试 ===")
        cases = [
            ("东契奇", "immaculate", "s_15_25"),
            ("东契奇", "prizm", "s_1_10"),
            ("文班亚马", "prizm", "s_1_10"),
            ("哈兰德", "futera", "s_15_25"),
            ("郑钦文", "immaculate", "s_1_10"),
        ]
        for p, s, t in cases:
            est, lvl, why = mb.estimate(p, s, t)
            print("  %-8s %-14s %-10s -> %s | %s" % (
                p, s, t, ("¥%d" % est) if est else "无法估价", lvl))
            print("      %s" % why)
