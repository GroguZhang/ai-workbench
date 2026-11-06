#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
球星卡拆卡报告截图 AI 解析器 MVP
--------------------------------
输入一张国内拼盒/拆卡平台（HobbyStock / 麦兜兜卡社 / FDC 等）的拆卡报告截图，
调用多模态 Vision 大模型（OpenAI gpt-4o 或任意 OpenAI 兼容接口）自动解析卡牌信息，
输出结构化 JSON 以及用于 eBay / 卡淘搜索的标准英文 Search Query。

用法:
    python parse_card.py --image ./screenshots/card_sample.jpg
    python parse_card.py --image ./screenshots/card_sample.jpg --model gpt-4o --output ./output/result.json
    python parse_card.py --image ./screenshots/card_sample.jpg --mock   # 无 API Key 时测试输出管线
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv 缺失时不影响运行，只是不加载 .env
    pass

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = HERE / "prompts" / "system_prompt.txt"
DEFAULT_OUTPUT_PATH = HERE / "output" / "result.json"
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 嵌入式兜底 Prompt（当 prompts/system_prompt.txt 不存在时使用）
FALLBACK_PROMPT = (
    "你是一名球星卡拆卡报告解析专家。解析拆卡截图，区分顶部商品标题与图片下方单卡描述栏，"
    "提取年份/运动/联盟/品牌/盒型，以及球队中英文、球员中英文、卡种、卡号、平行、编数，"
    "自动补全英文翻译（太阳→Phoenix Suns，特签→Signatures），生成 ebay_query 与 katao_query。"
    "只返回纯 JSON，不含 Markdown 代码块。"
)

# Mock 模式返回的示例数据（用于无 API Key 时验证输出管线）
MOCK_RESULT = {
    "raw_text_extracted": "2025-26 篮球 NBA Topps 盗梦 Inception Hobby #1\n太阳 Phoenix Suns 瑞安·邓恩 Ryan Dunn Rise to Fame Signatures",
    "break_info": {
        "platform": "HobbyStock",
        "product_title": "2025-26 篮球 NBA Topps 盗梦 Inception Hobby #1",
        "year": "2025-26",
        "sport": "Basketball",
        "league": "NBA",
        "brand_series": "Topps Inception",
        "box_type": "Hobby",
    },
    "card_info": {
        "team_cn": "太阳",
        "team_en": "Phoenix Suns",
        "player_cn": "瑞安·邓恩",
        "player_en": "Ryan Dunn",
        "set_subset": "Rise to Fame Signatures",
        "card_number": "RF-RD",
        "parallel_feature": "Auto",
        "numbered": None,
    },
    "search_queries": {
        "ebay_query": "2025-26 Topps Inception Ryan Dunn Rise to Fame Signatures RF-RD",
        "katao_query": "25-26 Topps Inception 太阳 瑞安邓恩 特签",
    },
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_system_prompt(prompt_path: Path) -> str:
    """加载系统 Prompt，优先外部文件，缺失时回退内置版本。"""
    try:
        if prompt_path and prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 读取 Prompt 文件失败，使用内置兜底: {exc}", file=sys.stderr)
    return FALLBACK_PROMPT


def encode_image_to_base64(image_path: str) -> str:
    """读取本地图片并转为 Base64 字符串。"""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")
    if path.suffix.lower() not in SUPPORTED_EXT:
        raise ValueError(
            f"不支持的图片格式: {path.suffix}，支持: {', '.join(sorted(SUPPORTED_EXT))}"
        )
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def mime_type_for(image_path: str) -> str:
    """根据扩展名推断 MIME 类型。"""
    ext = Path(image_path).suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    return mapping.get(ext, "image/jpeg")


def extract_json(text: str) -> dict:
    """
    从模型返回内容中提取纯 JSON。
    兼容三种情况：
      1. 纯 JSON 文本
      2. ```json ... ``` 代码块
      3. ``` ... ``` 代码块
    """
    if not text or not text.strip():
        raise ValueError("模型返回内容为空")

    text = text.strip()

    # 情况 2/3：提取代码块内容
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # 情况：只有开头 ``` 或夹杂文本，尝试截取首个 { 到末尾 }
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}\n原始内容片段:\n{text[:500]}")


# ---------------------------------------------------------------------------
# 核心解析逻辑
# ---------------------------------------------------------------------------
def parse_card(image_path: str, model: str, prompt_path: Path, base_url, api_key) -> dict:
    """调用 Vision API 解析拆卡截图，返回结构化 dict。"""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError(
            "未安装 openai SDK，请运行: pip install openai  "
            "（或使用 --mock 模式测试输出管线）"
        )

    if not api_key:
        raise RuntimeError(
            "未配置 OPENAI_API_KEY，请在 .env 中设置，或使用 --mock 模式测试。"
        )

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    system_prompt = load_system_prompt(prompt_path)
    image_b64 = encode_image_to_base64(image_path)
    mime = mime_type_for(image_path)

    user_content = [
        {
            "type": "text",
            "text": (
                "请解析这张拆卡报告截图，按照系统要求输出严格的 JSON。"
                "注意区分顶部商品标题与图片下方单卡描述栏，并自动补全英文翻译。"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
        },
    ]

    # 部分模型支持 response_format json_object，不支持时自动降级
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
    )
    try:
        kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
    except Exception:
        # 不支持 response_format 的模型（如部分兼容接口）降级重试
        kwargs.pop("response_format", None)
        response = client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content
    return extract_json(content)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="球星卡拆卡报告截图 AI 解析器（Vision 多模态）"
    )
    parser.add_argument(
        "-i", "--image",
        required=True,
        help="拆卡报告截图路径（jpg/png/webp 等）",
    )
    parser.add_argument(
        "-m", "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o"),
        help="视觉模型名称，默认 gpt-4o（也可用 gpt-4o-mini / 兼容接口模型）",
    )
    parser.add_argument(
        "-o", "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help=f"输出 JSON 路径，默认 {DEFAULT_OUTPUT_PATH}",
    )
    parser.add_argument(
        "--prompt",
        default=str(DEFAULT_PROMPT_PATH),
        help=f"系统 Prompt 文件路径，默认 {DEFAULT_PROMPT_PATH}",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Mock 模式：不调用 API，直接返回示例 JSON（用于无 Key 时测试输出管线）",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="输出格式化（缩进）的 JSON（默认开启）",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    # 确保输出目录存在
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.mock:
        print("[MOCK] 未调用 API，返回示例数据用于测试输出管线。", file=sys.stderr)
        result = MOCK_RESULT
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL") or None
        try:
            print(f"[INFO] 解析图片: {args.image}", file=sys.stderr)
            print(f"[INFO] 使用模型: {args.model}", file=sys.stderr)
            if base_url:
                print(f"[INFO] API BaseURL: {base_url}", file=sys.stderr)
            result = parse_card(
                image_path=args.image,
                model=args.model,
                prompt_path=Path(args.prompt),
                base_url=base_url,
                api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] 解析失败: {exc}", file=sys.stderr)
            return 1

    # 保存 + 打印
    indent = 2 if args.pretty else None
    json_str = json.dumps(result, ensure_ascii=False, indent=indent)
    output_path.write_text(json_str, encoding="utf-8")
    print(f"[OK] 结果已保存至: {output_path}", file=sys.stderr)
    print(json_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
