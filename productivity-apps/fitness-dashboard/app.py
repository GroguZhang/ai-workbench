# -*- coding: utf-8 -*-
"""DrZ 健身仪表盘 · 主入口。
启动: streamlit run app.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
from core import db, synclayer, paths

st.set_page_config(
    page_title="DrZ 健身仪表盘",
    page_icon="⚛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 初始化数据库 + 与权威 CSV 同步（每个会话一次）
db.init_db()
_sync = synclayer.ensure_synced()

# 侧边栏显示数据来源
with st.sidebar:
    st.markdown("### ⚛️ DrZ 健身仪表盘")
    csv_path = paths.find_workspace_csv()
    if csv_path:
        st.caption(f"数据源 · 群晖同步 CSV\n`{csv_path.name}` · {_sync.get('total', 0)} 条记录")
    else:
        st.warning("未找到权威 CSV")

# 自定义暗黑科技风 CSS
st.markdown("""
<style>
    /* 顶部背景 */
    .stApp { background: #0E1117; }
    /* 卡片样式 */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1A1F2E 0%, #151926 100%);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    div[data-testid="stMetric"] label {
        font-size: 12px;
        color: #8892A6;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        font-size: 28px;
        font-weight: 600;
        color: #FAFAFA;
    }
    /* Delta 颜色：涨红跌绿（中国市场惯例） */
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] {
        color: #FF8A1F;
    }
    /* 侧边栏 */
    section[data-testid="stSidebar"] {
        background: #0A0D14;
        border-right: 1px solid rgba(255,255,255,0.04);
    }
    /* 标题 */
    h1, h2, h3 { color: #FAFAFA !important; letter-spacing: -0.5px; }
    h1 { border-bottom: 2px solid #FF8A1F; padding-bottom: 8px; }
    /* Tab 样式 */
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background: #1A1F2E;
        border-radius: 8px 8px 0 0;
        padding: 8px 20px;
    }
    .stTabs [aria-selected="true"] {
        background: #FF8A1F !important;
        color: #0E1117 !important;
    }
    /* 警告框 */
    .stAlert { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

st.title("⚛️ DrZ 健身仪表盘")
st.caption("数据源 · 苹果健康 → 训记 → 本地分析层 · 闭环系统")

# 今日概览
records = db.get_body_range()
training = db.get_training_range()

if not records:
    st.warning("数据库还没有数据。请到「📝 数据录入」页导入或手动添加第一条记录，"
               "或将群晖同步的 `data/body_records.csv` 直接导入。")
else:
    today = records[0]
    prev = records[1] if len(records) > 1 else None

    from core.analysis import compute_trend, bia_error_check, recovery_status

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        delta_w = (today.get("weight") or 0) - (prev.get("weight") or 0) if prev else 0
        st.metric("体重 (kg)", today.get("weight"), f"{delta_w:+.1f}" if prev else None, delta_color="inverse")
    with col2:
        delta_bf = (today.get("bodyfat_pct") or 0) - (prev.get("bodyfat_pct") or 0) if prev else 0
        st.metric("体脂率 (%)", today.get("bodyfat_pct"), f"{delta_bf:+.1f}" if prev else None, delta_color="inverse")
    with col3:
        st.metric("去脂体重 (kg)", today.get("ffm_kg"),
                  f"{(today.get('ffm_kg') or 0)-(prev.get('ffm_kg') or 0):+.1f}" if prev else None)
    with col4:
        st.metric("睡眠 (h)", today.get("sleep_h"),
                  f"{(today.get('sleep_h') or 0)-(prev.get('sleep_h') or 0):+.1f}" if prev else None)

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        st.metric("骨骼肌 (kg)", today.get("skeletal_muscle_kg"))
    with col6:
        st.metric("内脏脂肪等级", today.get("visceral_fat_lvl"))
    with col7:
        st.metric("步数", today.get("steps"))
    with col8:
        st.metric("静息心率", today.get("resting_hr") or "—")

    # BIA 误差检测
    bia = bia_error_check(today, prev)
    if bia.get("risk") == "water_chain_error":
        st.error(
            "⚠ BIA 误差警告：今日水分波动主导了体成分读数。\n\n"
            f"水分率变化 {bia['delta_water_pct']:+.1f}%，FFM 变化 {bia['delta_ffm']:+.1f}kg，"
            f"体脂量变化 {bia['delta_fat']:+.1f}kg。\n\n"
            "肌肉量/骨骼肌的下降是连带重算，不是真实流失。"
            "**体脂量是唯一可信的短期信号。**"
        )

    # 恢复状态
    ma7 = {"sleep_h": compute_trend(records, "sleep_h")}
    status = recovery_status(today, ma7)
    if status == "恢复不足":
        st.error(f"恢复状态：{status} — 睡眠不足，今日不建议高强度训练。")
    elif status == "中等恢复":
        st.warning(f"恢复状态：{status}")
    else:
        st.success(f"恢复状态：{status}")

    # 7 日均线趋势
    st.subheader("📊 7 日移动平均趋势")
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    rows = sorted(records, key=lambda r: r["date"])
    dates = [r["date"] for r in rows]
    tw = compute_trend(rows, "weight")
    tbf = compute_trend(rows, "bodyfat_pct")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=dates, y=tw["raw"], name="体重", line=dict(color="#378ADD", width=1, dash="dot"), opacity=0.4), secondary_y=False)
    fig.add_trace(go.Scatter(x=dates, y=tw["ma"], name="体重 7日均线", line=dict(color="#FF8A1F", width=2.5)), secondary_y=False)
    fig.add_trace(go.Scatter(x=dates, y=tbf["raw"], name="体脂率", line=dict(color="#D85A30", width=1, dash="dot"), opacity=0.4), secondary_y=True)
    fig.add_trace(go.Scatter(x=dates, y=tbf["ma"], name="体脂率 7日均线", line=dict(color="#1D9E75", width=2.5)), secondary_y=True)
    fig.update_layout(
        template="plotly_dark", height=400, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#0E1117", plot_bgcolor="#0E1117",
        legend=dict(orientation="h", y=1.1)
    )
    fig.update_yaxes(title_text="体重 (kg)", secondary_y=False, gridcolor="#1A1F2E")
    fig.update_yaxes(title_text="体脂率 (%)", secondary_y=True, gridcolor="#1A1F2E")
    st.plotly_chart(fig, width="stretch")

    st.info(f"数据点：{len(records)} / 14（攒满 14 个点后可计算 7 日移动平均与 TDEE）")
