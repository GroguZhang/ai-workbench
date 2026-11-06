# -*- coding: utf-8 -*-
"""设置：API Key 配置、数据库状态、一键导入工作区数据。"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from core import db, paths, synclayer
from dotenv import load_dotenv

st.title("⚙️ 设置")

# 配置写本机目录，不落群晖同步盘
ENV_FILE = paths.ENV_FILE

tab1, tab2, tab3 = st.tabs(["API 配置", "数据库", "关于"])

# ---- API 配置 ----
with tab1:
    st.subheader("大模型 API 配置")
    st.caption("OpenAI 兼容接口，支持 DeepSeek / OpenAI / 本地 Ollama")

    load_dotenv(ENV_FILE)
    cur_key = os.getenv("LLM_API_KEY", "")
    cur_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    cur_model = os.getenv("LLM_MODEL", "deepseek-chat")

    with st.form("llm_config"):
        api_key = st.text_input("API Key", value=cur_key, type="password",
                                placeholder="sk-...")
        base_url = st.text_input("Base URL", value=cur_url,
                                 help="DeepSeek: https://api.deepseek.com/v1 | Ollama: http://localhost:11434/v1")
        model = st.text_input("模型名", value=cur_model,
                              help="deepseek-chat / gpt-4o-mini / llama3.2 等")
        st.form_submit_button("保存到 .env")

    if st.button("💾 保存配置", type="primary"):
        lines = [
            f"LLM_API_KEY={api_key}",
            f"LLM_BASE_URL={base_url}",
            f"LLM_MODEL={model}",
            f"STREAMLIT_PORT={os.getenv('STREAMLIT_PORT','8501')}",
        ]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        st.success("✅ 已保存到 .env，刷新页面后生效")
        st.rerun()

    st.divider()
    st.write("**快速预设**")


def _save_preset(key, url, model):
    lines = [f"LLM_API_KEY={key}", f"LLM_BASE_URL={url}", f"LLM_MODEL={model}"]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    st.success("已保存预设")
    st.rerun()


c1, c2, c3 = st.columns(3)
with c1:
    if st.button("DeepSeek"):
        _save_preset("sk-", "https://api.deepseek.com/v1", "deepseek-chat")
with c2:
    if st.button("OpenAI"):
        _save_preset("sk-", "https://api.openai.com/v1", "gpt-4o-mini")
with c3:
    if st.button("本地 Ollama"):
        _save_preset("ollama", "http://localhost:11434/v1", "llama3.2")


# ---- 数据库 ----
with tab2:
    st.subheader("数据库与多机同步")
    records = db.get_body_range()
    training = db.get_training_range()
    plans = db.get_plans()
    c1, c2, c3 = st.columns(3)
    c1.metric("身体记录", len(records))
    c2.metric("训练记录", len(training))
    c3.metric("AI 方案", len(plans))
    st.markdown(paths.describe())
    st.caption("⚠ 多机部署原则：代码与 CSV 主档随群晖同步；SQLite、venv、.env 留在各机器本地。"
               "SQLite 若进同步目录，写入时会被同步客户端抓取到损坏的中间状态。")
    if st.button("🔄 立即双向同步 CSV"):
        r = synclayer.sync()
        st.success(f"✅ 拉取 {r['imported']} 条 / 推送 {r['exported']} 条 / 共 {r['total']} 条")

# ---- 关于 ----
with tab3:
    st.subheader("关于本系统")
    st.markdown("""
**DrZ 健身仪表盘** · 基于苹果健康 / 训记 Open API / 本地分析层

**分析纪律（内置）**：
1. 不用单日读数做判断，一律看 7 日移动平均斜率
2. BIA 体脂秤：水分波动会连带污染肌肉量/骨骼肌/FFM
3. 体脂量(kg) 是唯一可信的短期信号
4. 前 14 天数据不计入 TDEE 计算
5. 内脏脂肪等级单级跳变无意义

**数据流闭环**：
```
阿福体脂秤 → 苹果健康 → 训记 S3 → 本地 SQLite
                                    ↓
华为 Watch → 苹果健康 → 训记 S1 → 训练容量分析
                                    ↓
                             7日均线 + TDEE + 恢复评估
                                    ↓
                        LLM 深度诊断 → 方案归档
                                    ↓
                        训记 S4 模版写入 → 闭环
```

⚛️ 奥本海默分析层
""")
