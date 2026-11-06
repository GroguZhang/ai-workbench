# -*- coding: utf-8 -*-
"""AI 教练：一键深度诊断 + 动态计划生成 + 方案归档。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from core import db
from core.analysis import (compute_trend, estimate_tdee, training_volume,
                           bia_error_check, recovery_status, build_context)
from core.llm import call_llm

st.title("🤖 AI 教练")

records = db.get_body_range()
training = db.get_training_range()

if not records:
    st.warning("暂无数据，请先到「数据录入」页添加记录。")
    st.stop()

# 本地分析概览
rows = sorted(records, key=lambda r: r["date"])
today = rows[-1]
prev = rows[-2] if len(rows) >= 2 else None

st.subheader("📋 本地分析概览")

c1, c2, c3 = st.columns(3)
with c1:
    tw = compute_trend(rows, "weight")
    st.metric("体重 7日均线斜率", f"{tw.get('slope','N/A')} kg/天" if tw.get("slope") else "数据不足")
with c2:
    tbf = compute_trend(rows, "bodyfat_pct")
    st.metric("体脂率 7日均线斜率", f"{tbf.get('slope','N/A')} %/天" if tbf.get("slope") else "数据不足")
with c3:
    vol = training_volume(training)
    st.metric("近期训练容量", f"{vol} kg·组·次")

tdee = estimate_tdee(rows)
if tdee:
    st.info(f"**TDEE 估算**：{tdee['tdee']} kcal | 日均摄入 {tdee['avg_intake']} kcal | "
            f"体重变化 {tdee['delta_weight']} kg / {tdee['days']}天")
else:
    st.info(f"数据点 {len(rows)}/14，攒满 14 天后可计算 TDEE。")

bia = bia_error_check(today, prev)
if bia.get("risk") == "water_chain_error":
    st.error("⚠ BIA 误差：水分波动主导体成分读数，肌肉/骨骼肌下降为连带重算。体脂量是唯一可信短期信号。")

ma7 = {"sleep_h": compute_trend(rows, "sleep_h")}
status = recovery_status(today, ma7)
st.write(f"**恢复状态**：{status}")

st.divider()

# LLM 深度诊断
st.subheader("🧠 一键深度诊断")
st.caption("复用健身工作区 agent 的分析 Prompt，接入 OpenAI 兼容接口（DeepSeek/OpenAI/本地 Ollama）")

if st.button("⚡ 生成今日分析", type="primary"):
    with st.spinner("正在构建数据上下文并调用 LLM..."):
        context = build_context(records, training)
        # 先展示上下文（可折叠）
        with st.expander("📊 数据上下文（传给 LLM 的内容）"):
            st.code(context, language="markdown")
        result = call_llm(context)
        st.markdown(result)

        # 归档
        db.save_plan(str(today.get("date", "")), "daily_analysis", result)
        st.success("✅ 分析已归档，可在下方历史中查看")

st.divider()
st.subheader("📜 方案归档")
plans = db.get_plans(20)
if plans:
    for p in plans:
        with st.expander(f"{p['date']} · {p['plan_type']}"):
            st.markdown(p["content"])
else:
    st.caption("暂无归档方案")
