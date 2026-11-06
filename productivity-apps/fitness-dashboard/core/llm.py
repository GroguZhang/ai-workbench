# -*- coding: utf-8 -*-
"""大模型客户端：OpenAI 兼容接口（支持 DeepSeek / OpenAI / 本地 Ollama）。"""
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

from .paths import ENV_FILE, PROJECT_ROOT

# 优先读本机 ~/.drzfitness/.env（不同步，API Key 不落 NAS），回退到项目目录
load_dotenv(ENV_FILE)
load_dotenv(PROJECT_ROOT / ".env")

ANALYSIS_PROMPT = """你是 DrZ 的健身 AI 教练（奥本海默分析层）。

严格遵守以下分析纪律：
1. 不用单日读数做判断，一律看 7 日移动平均的斜率
2. BIA 体脂秤数据：水分波动会连带污染肌肉量/骨骼肌/FFM——它们是同一算式的三个输出，不是独立证据。唯一可信的短期信号是体脂量(kg)
3. 一天掉 1kg 以上骨骼肌在生理上不可能，出现这种数字就是水分主导的连带重算
4. 前 14 天数据不计入 TDEE 计算（含大量水分与糖原消耗）
5. 内脏脂肪等级是"估算中的估算"，单级跳变无意义

输出结构：
## 一、今日恢复状态
（疲劳度评估、代谢评估，基于睡眠/HRV/静息心率）

## 二、体成分趋势
（7 日均线方向、真实信号 vs 噪点、水分干扰判断）

## 三、训练建议
（基于训练容量与渐进超负荷，给出下一阶段负荷/热量/休息日建议）

## 四、风险提示
（任何需要警惕的信号）

用中文，冷静精准，不废话。"""


def call_llm(context: str, api_key: str = None, base_url: str = None, model: str = None) -> str:
    """调用 OpenAI 兼容接口。从参数或 .env 读取配置。"""
    key = api_key or os.getenv("LLM_API_KEY", "")
    url = base_url or os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    mdl = model or os.getenv("LLM_MODEL", "deepseek-chat")

    if not key or key.startswith("sk-your"):
        return ("⚠ 未配置 LLM_API_KEY。请在「设置」页填入 API Key，"
                "或编辑项目根目录的 .env 文件。当前可用的分析（7日均线、TDEE、BIA误差检测）"
                "已在本地完成，下方展示。LLM 深度诊断需要 Key 才能生成。")

    endpoint = url.rstrip("/") + "/chat/completions"
    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": ANALYSIS_PROMPT},
            {"role": "user", "content": context},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠ LLM 调用失败：{type(e).__name__}: {e}"
