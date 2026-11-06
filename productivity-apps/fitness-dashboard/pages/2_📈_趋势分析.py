# -*- coding: utf-8 -*-
"""趋势分析：睡眠/心率/HRV 走势、训练容量周期对比、步数。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from core import db
from core.analysis import compute_trend, moving_avg, training_volume

st.title("📈 趋势分析")

records = db.get_body_range()
training = db.get_training_range()

if len(records) < 2:
    st.info("数据点不足（当前 %d 个）。趋势分析需要至少 2 个数据点，攒到 14 个后 7 日均线开始有意义。" % len(records))
    st.stop()

rows = sorted(records, key=lambda r: r["date"])
dates = [r["date"] for r in rows]

DARK = dict(paper_bgcolor="#0E1117", plot_bgcolor="#0E1117", font=dict(color="#FAFAFA"),
            margin=dict(l=10, r=10, t=40, b=10), showlegend=True,
            legend=dict(orientation="h", y=1.12))

# ---- 睡眠与恢复 ----
st.subheader("🌙 睡眠与恢复走势")
sleep_raw = [r.get("sleep_h") for r in rows]
sleep_ma = moving_avg(sleep_raw, 7)
deep_raw = [r.get("deep_sleep_h") for r in rows]

fig = make_subplots(specs=[[{"secondary_y": True}]])
fig.add_trace(go.Scatter(x=dates, y=sleep_raw, name="睡眠时长", mode="lines+markers",
                         line=dict(color="#378ADD", width=1.5)), secondary_y=False)
fig.add_trace(go.Scatter(x=dates, y=sleep_ma, name="7日均线", line=dict(color="#FF8A1F", width=3)), secondary_y=False)
if any(deep_raw):
    fig.add_trace(go.Bar(x=dates, y=deep_raw, name="深睡", marker_color="rgba(29,158,117,0.4)"), secondary_y=True)
fig.add_hline(y=7, line_dash="dot", line_color="#D85A30", annotation_text="7h 目标", secondary_y=False)
fig.update_layout(height=350, **DARK)
fig.update_yaxes(title_text="睡眠 (h)", secondary_y=False, gridcolor="#1A1F2E")
fig.update_yaxes(title_text="深睡 (h)", secondary_y=True, gridcolor="#1A1F2E")
st.plotly_chart(fig, width="stretch")

# ---- 心率与 HRV ----
rhr_raw = [r.get("resting_hr") for r in rows]
hrv_raw = [r.get("hrv") for r in rows]
if any(rhr_raw) or any(hrv_raw):
    st.subheader("❤️ 心率与 HRV")
    fig2 = make_subplots(specs=[[{"secondary_y": True}]])
    if any(rhr_raw):
        fig2.add_trace(go.Scatter(x=dates, y=rhr_raw, name="静息心率", mode="lines+markers",
                                  line=dict(color="#D85A30", width=2)), secondary_y=False)
        fig2.add_hline(y=60, line_dash="dot", line_color="#888", annotation_text="60 bpm", secondary_y=False)
    if any(hrv_raw):
        fig2.add_trace(go.Scatter(x=dates, y=hrv_raw, name="HRV", mode="lines+markers",
                                  line=dict(color="#1D9E75", width=2)), secondary_y=True)
    fig2.update_layout(height=300, **DARK)
    fig2.update_yaxes(title_text="静息心率 (bpm)", secondary_y=False, gridcolor="#1A1F2E")
    fig2.update_yaxes(title_text="HRV (ms)", secondary_y=True, gridcolor="#1A1F2E")
    st.plotly_chart(fig2, width="stretch")
else:
    st.caption("静息心率 / HRV 暂无数据——接入手表数据后此处自动出现走势图。")

# ---- 步数 ----
steps_raw = [r.get("steps") for r in rows]
if any(steps_raw):
    st.subheader("🚶 步数")
    fig3 = go.Figure()
    fig3.add_trace(go.Bar(x=dates, y=steps_raw, name="步数",
                          marker_color=["#FF8A1F" if s and s >= 8000 else "#378ADD" for s in steps_raw]))
    fig3.add_hline(y=8000, line_dash="dot", line_color="#1D9E75", annotation_text="8000 目标")
    fig3.update_layout(height=280, **DARK)
    fig3.update_yaxes(gridcolor="#1A1F2E")
    st.plotly_chart(fig3, width="stretch")

# ---- 训练容量周期对比 ----
st.subheader("🏋️ 训练容量周期对比")
if training:
    import collections
    weekly = collections.OrderedDict()
    for t in sorted(training, key=lambda x: x["date"]):
        week = t["date"][:7] + "-W" + str(__import__("datetime").date.fromisoformat(t["date"]).isocalendar()[1])
        weekly.setdefault(week, []).append(t)
    weeks = list(weekly.keys())
    vols = [training_volume(v) for v in weekly.values()]
    counts = [len(v) for v in weekly.values()]

    fig4 = make_subplots(specs=[[{"secondary_y": True}]])
    fig4.add_trace(go.Bar(x=weeks, y=vols, name="训练容量 (kg·组·次)", marker_color="rgba(255,138,31,0.7)"), secondary_y=False)
    fig4.add_trace(go.Scatter(x=weeks, y=counts, name="记录条数", mode="lines+markers",
                              line=dict(color="#378ADD", width=2)), secondary_y=True)
    fig4.update_layout(height=320, **DARK)
    fig4.update_yaxes(title_text="容量", secondary_y=False, gridcolor="#1A1F2E")
    fig4.update_yaxes(title_text="条数", secondary_y=True, gridcolor="#1A1F2E")
    st.plotly_chart(fig4, width="stretch")
    st.caption("容量 = Σ 组数 × 次数 × 重量。渐进超负荷的核心指标：容量应随周期缓慢上升，而非直线飙升。")
else:
    st.caption("暂无训练记录。到「数据录入」页添加，或等训记模版开始使用后同步。")

# ---- 每日消耗 ----
kcal_raw = [r.get("energy_kcal") for r in rows]
if any(kcal_raw):
    st.subheader("🔥 每日消耗")
    fig5 = go.Figure()
    fig5.add_trace(go.Scatter(x=dates, y=kcal_raw, name="消耗 kcal", mode="lines+markers",
                              line=dict(color="#EF9F27", width=2)))
    ma_kcal = moving_avg(kcal_raw, 7)
    fig5.add_trace(go.Scatter(x=dates, y=ma_kcal, name="7日均线", line=dict(color="#FF8A1F", width=3)))
    fig5.update_layout(height=280, **DARK)
    fig5.update_yaxes(gridcolor="#1A1F2E")
    st.plotly_chart(fig5, width="stretch")
