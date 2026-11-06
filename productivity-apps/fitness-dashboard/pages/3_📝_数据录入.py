# -*- coding: utf-8 -*-
"""数据录入与管理：手动表单 + CSV/JSON 导入导出 + 历史记录。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import pandas as pd
from datetime import date
from core import db, importer, synclayer, paths

st.title("📝 数据录入与管理")

tab1, tab2, tab3 = st.tabs(["手动录入", "导入 / 导出", "历史记录"])

# ---- 手动录入 ----
with tab1:
    st.subheader("每日身体数据")
    with st.form("body_form"):
        d = st.date_input("日期", value=date.today())
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            weight = st.number_input("体重 (kg)", value=83.0, step=0.1, format="%.1f")
            bodyfat = st.number_input("体脂率 (%)", value=25.0, step=0.1, format="%.1f")
            bmi = st.number_input("BMI", value=26.0, step=0.1, format="%.1f")
        with col2:
            ffm = st.number_input("去脂体重 FFM (kg)", value=62.0, step=0.1, format="%.1f")
            muscle = st.number_input("肌肉量 (kg)", value=59.0, step=0.1, format="%.1f")
            sm = st.number_input("骨骼肌量 (kg)", value=31.0, step=0.1, format="%.1f")
        with col3:
            vf = st.number_input("内脏脂肪等级", value=11, step=1)
            water = st.number_input("水分率 (%)", value=52.0, step=0.1, format="%.1f")
            fat_mass = st.number_input("体脂量 (kg)", value=21.0, step=0.1, format="%.1f")
        with col4:
            sleep = st.number_input("睡眠 (h)", value=6.5, step=0.1, format="%.1f")
            deep = st.number_input("深睡 (h)", value=2.0, step=0.1, format="%.1f")
            steps = st.number_input("步数", value=8000, step=100)

        col5, col6, col7 = st.columns(3)
        with col5:
            rhr = st.number_input("静息心率 (bpm)", value=0, step=1)
        with col6:
            hrv = st.number_input("HRV (ms)", value=0, step=1)
        with col7:
            kcal = st.number_input("每日消耗 (kcal)", value=0, step=10)
        note = st.text_input("备注", "")
        submitted = st.form_submit_button("保存")

        if submitted:
            rec = {"date": str(d), "weight": weight, "bmi": bmi, "bodyfat_pct": bodyfat,
                   "fat_mass_kg": fat_mass, "ffm_kg": ffm, "muscle_kg": muscle,
                   "skeletal_muscle_kg": sm, "visceral_fat_lvl": vf, "water_pct": water,
                   "sleep_h": sleep, "deep_sleep_h": deep, "steps": steps,
                   "resting_hr": rhr or None, "hrv": hrv or None, "energy_kcal": kcal or None,
                   "note": note}
            # 自动反算缺失字段
            if weight and bodyfat and not fat_mass:
                rec["fat_mass_kg"] = round(weight * bodyfat / 100, 1)
            if weight and fat_mass and not ffm:
                rec["ffm_kg"] = round(weight - fat_mass, 1)
            db.upsert_body(rec)
            n = synclayer.push()  # 写回权威 CSV，其他机器即可同步到
            st.success(f"✅ {d} 身体数据已保存，已写回权威 CSV（{n} 条）")

    st.divider()
    st.subheader("训练记录")
    with st.form("train_form"):
        td = st.date_input("训练日期", value=date.today(), key="tdate")
        tpl = st.text_input("模版名称（如：全身 A）", value="")
        mv = st.text_input("动作名称", value="", placeholder="如：杠铃深蹲（须匹配训记标准库）")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            sets = st.number_input("组数", value=3, step=1)
        with c2:
            reps = st.number_input("次数", value=8, step=1)
        with c3:
            wt = st.number_input("重量 (kg)", value=30.0, step=2.5, format="%.1f")
        with c4:
            unit = st.selectbox("单位", ["kg", "lb", "bw"], index=0)
        with c5:
            rpe = st.number_input("RPE (6-10)", value=7, step=1)
        tnote = st.text_input("备注", key="tnote")
        tsub = st.form_submit_button("添加训练记录")
        if tsub and mv:
            db.add_training({"date": str(td), "template_name": tpl, "movement": mv,
                             "sets": sets, "reps": reps, "weight": wt, "unit": unit,
                             "rpe": rpe, "note": tnote})
            st.success(f"✅ {td} 训练记录已添加：{mv} {sets}×{reps} @ {wt}{unit}")

# ---- 导入导出 ----
with tab2:
    st.subheader("导入数据")
    st.caption("兼容群晖同步的 `data/body_records.csv` 格式（21 列）")
    up = st.file_uploader("上传 CSV 或 JSON", type=["csv", "json"])
    if up is not None:
        try:
            if up.name.endswith(".csv"):
                n, skipped = importer.import_csv(up.getvalue())
            else:
                n, skipped = importer.import_json(up.getvalue())
            st.success(f"✅ 导入完成：{n} 条记录")
            if skipped:
                st.warning(f"跳过 {len(skipped)} 条：")
                for s in skipped[:10]:
                    st.text(s)
        except Exception as e:
            st.error(f"导入失败：{e}")

    st.divider()
    st.subheader("权威数据源（群晖同步）")
    ws_csv = paths.find_workspace_csv()
    if ws_csv:
        st.caption(f"CSV 主档：`{ws_csv}`")
        st.caption("页面启动时自动双向同步，通常无需手动干预。CSV 是权威源，SQLite 仅本机缓存。")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔄 立即从 CSV 拉取"):
                n, skipped = synclayer.pull(ws_csv)
                st.success(f"✅ 已拉取 {n} 条")
        with c2:
            if st.button("⬆ 立即推送到 CSV"):
                n = synclayer.push(ws_csv)
                st.success(f"✅ 已推送 {n} 条")
    else:
        st.warning("未找到权威 CSV。可设置环境变量 DRZ_WORKSPACE_CSV 指定路径。")

    st.divider()
    st.subheader("导出数据")
    records = db.get_body_range()
    if records:
        csv_str = importer.export_csv(records)
        st.download_button("⬇ 导出 CSV 副本", csv_str, file_name="body_records_export.csv",
                            mime="text/csv")
    else:
        st.caption("暂无数据可导出")

# ---- 历史记录 ----
with tab3:
    st.subheader("历史记录")
    records = db.get_body_range()
    if not records:
        st.info("暂无记录")
    else:
        df = pd.DataFrame(records)
        st.dataframe(df, width="stretch", hide_index=True)
        st.divider()
        st.write("**删除某天记录**")
        del_date = st.selectbox("选择日期", [r["date"] for r in records])
        if st.button("删除该日记录"):
            db.delete_body(del_date)
            synclayer.push()
            st.success(f"已删除 {del_date}，已同步到 CSV")
            st.rerun()
