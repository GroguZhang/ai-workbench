# -*- coding: utf-8 -*-
"""分析层：7 日移动平均、TDEE 反推、训练容量、BIA 误差解读。
复用健身工作区的 agent 分析逻辑。"""
import statistics
from datetime import date, timedelta


def _dates(n: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n)]


def moving_avg(values: list[float], window: int = 7) -> list[float]:
    """居中 7 日移动平均（边缘用可用数据补）。"""
    out = []
    half = window // 2
    for i in range(len(values)):
        s = max(0, i - half)
        e = min(len(values), i + half + 1)
        chunk = [v for v in values[s:e] if v is not None]
        out.append(round(statistics.mean(chunk), 2) if chunk else None)
    return out


def compute_trend(records: list[dict], metric: str, window: int = 7) -> dict:
    """对某指标做 7 日移动平均 + 首尾斜率。"""
    rows = sorted(records, key=lambda r: r["date"])
    raw = [r.get(metric) for r in rows]
    ma = moving_avg(raw, window)
    valid_ma = [v for v in ma if v is not None]
    slope = None
    if len(valid_ma) >= 2:
        span = len(rows) - 1
        slope = round((valid_ma[-1] - valid_ma[0]) / max(span, 1), 3)
    return {"raw": raw, "ma": ma, "slope": slope, "latest": raw[-1] if raw else None}


def estimate_tdee(records: list[dict], days: int = 14) -> dict | None:
    """TDEE 反推：日均摄入 − Δ体重×7700/天数。
    需要 weight + energy_kcal + 至少 14 个数据点。
    """
    rows = sorted(records, key=lambda r: r["date"])[-days:]
    weights = [r.get("weight") for r in rows if r.get("weight")]
    energies = [r.get("energy_kcal") for r in rows if r.get("energy_kcal")]
    if len(weights) < 14 or len(energies) < 14:
        return None
    delta = weights[-1] - weights[0]
    avg_in = statistics.mean(energies)
    daily_deficit = delta * 7700 / len(rows)
    tdee = round(avg_in - daily_deficit)
    return {
        "tdee": tdee, "avg_intake": round(avg_in), "delta_weight": round(delta, 2),
        "daily_deficit": round(daily_deficit), "days": len(rows)
    }


def training_volume(rows: list[dict]) -> float:
    """总训练容量 = Σ 组数 × 次数 × 重量。"""
    vol = 0.0
    for r in rows:
        s = r.get("sets") or 0
        reps = r.get("reps") or 0
        w = r.get("weight") or 0
        vol += s * reps * w
    return round(vol, 1)


def bia_error_check(today: dict, yesterday: dict | None) -> dict:
    """水分主导的连锁误差检测。
    BIA 只直接测阻抗，水分降→阻抗升→肌肉/骨骼肌/FFM 全线连带下修。
    它们是同一条算式的输出，不是独立证据。
    """
    if not yesterday:
        return {"risk": "no_baseline"}
    dw = (today.get("water_pct") or 0) - (yesterday.get("water_pct") or 0)
    d_ffm = (today.get("ffm_kg") or 0) - (yesterday.get("ffm_kg") or 0)
    d_fat = (today.get("fat_mass_kg") or 0) - (yesterday.get("fat_mass_kg") or 0)
    d_muscle = (today.get("muscle_kg") or 0) - (yesterday.get("muscle_kg") or 0)

    # 水分变动幅度主导 FFM 变动 → 连锁误差
    water_dominant = abs(dw) > 0.8 and abs(d_ffm) > 0.3 and abs(d_fat) < abs(d_ffm)
    # 骨骼肌单日降幅 > 0.5kg 在生理上不可能
    impossible_muscle = abs(d_muscle) > 0.5

    return {
        "risk": "water_chain_error" if (water_dominant or impossible_muscle) else "normal",
        "delta_water_pct": round(dw, 2),
        "delta_ffm": round(d_ffm, 2),
        "delta_fat": round(d_fat, 2),
        "delta_muscle": round(d_muscle, 2),
        "water_dominant": water_dominant,
        "impossible_muscle_loss": impossible_muscle,
        "trustworthy_signal": "fat_mass_kg",  # 体脂量是唯一不受水分直接驱动的短期信号
    }


def recovery_status(today: dict, ma7: dict) -> str:
    """疲劳/恢复评估：综合睡眠、HRV、静息心率。"""
    sleep = today.get("sleep_h") or 0
    hrv = today.get("hrv")
    rhr = today.get("resting_hr")
    sleep_ma = ma7.get("sleep_h", {}).get("latest")

    score = 0
    if sleep >= 7:
        score += 2
    elif sleep >= 6:
        score += 1
    if hrv:
        if hrv >= 60:
            score += 2
        elif hrv >= 40:
            score += 1
    if rhr:
        if rhr <= 60:
            score += 2
        elif rhr <= 70:
            score += 1
    if score >= 5:
        return "恢复良好"
    if score >= 3:
        return "中等恢复"
    return "恢复不足"


def build_context(records: list[dict], training: list[dict]) -> str:
    """为 LLM 构建数据上下文摘要。"""
    rows = sorted(records, key=lambda r: r["date"])
    today = rows[-1] if rows else {}
    prev = rows[-2] if len(rows) >= 2 else {}
    ma_w = compute_trend(rows, "weight")
    ma_bf = compute_trend(rows, "bodyfat_pct")
    tdee = estimate_tdee(rows)

    ctx = f"""# 健身数据上下文（截至 {today.get('date','')}）

## 今日核心指标
- 体重 {today.get('weight','N/A')} kg | 体脂率 {today.get('bodyfat_pct','N/A')}%
- 去脂体重 {today.get('ffm_kg','N/A')} kg | 骨骼肌 {today.get('skeletal_muscle_kg','N/A')} kg
- 内脏脂肪等级 {today.get('visceral_fat_lvl','N/A')} | 水分率 {today.get('water_pct','N/A')}%
- 睡眠 {today.get('sleep_h','N/A')} h | 步数 {today.get('steps','N/A')} | 静息心率 {today.get('resting_hr','N/A')}

## 7 日移动平均趋势
- 体重斜率 {ma_w.get('slope','N/A')} kg/天
- 体脂率斜率 {ma_bf.get('slope','N/A')} 百分点/天

## TDEE 估算
- {json.dumps(tdee) if tdee else '数据不足，需 ≥14 个数据点'}

## 训练记录（近 7 天）
"""
    vol = training_volume(training)
    ctx += f"- 总训练容量 {vol} kg·组·次\n"
    for t in training[:20]:
        ctx += f"  {t.get('date')}: {t.get('movement')} {t.get('sets')}×{t.get('reps')} @ {t.get('weight')}{t.get('unit','kg')}\n"

    bia = bia_error_check(today, prev)
    if bia.get("risk") == "water_chain_error":
        ctx += "\n## ⚠ BIA 误差警告\n水分波动主导了体成分读数，肌肉量/骨骼肌的下降是连带重算而非真实流失。体脂量是唯一可信短期信号。\n"

    return ctx
