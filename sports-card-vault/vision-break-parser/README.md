# 球星卡拆卡报告截图 AI 解析器 MVP

利用 **Vision / 多模态大模型 API**（OpenAI gpt-4o 或任意 OpenAI 兼容接口），自动解析国内拼盒/拆卡平台（HobbyStock、麦兜兜卡社、FDC 等）的拆卡报告截图，输出结构化 JSON 及用于 eBay / 卡淘搜索的标准英文 Search Query。

## ✨ 功能

- 输入一张本地拆卡报告截图（jpg / png / webp）。
- 自动区分**顶部商品标题栏**与**图片下方单卡描述栏**。
- 提取：年份、运动、联盟、品牌系列、盒型、球队中英文、球员中英文、卡种、卡号、平行版本、编数。
- 自动补全中→英翻译（太阳→Phoenix Suns，特签→Signatures 等）。
- 生成 `ebay_query`（纯英文）与 `katao_query`（中英混合）搜索词。
- 输出严格 JSON，保存至 `./output/result.json`。

## 📂 项目结构

```
.
├── parse_card.py          # 主执行脚本
├── make_sample.py         # 生成测试用样例截图（可选）
├── prompts/
│   └── system_prompt.txt  # 多模态 System Prompt（可自由编辑调优）
├── screenshots/           # 放入待解析截图
├── output/                # 解析结果输出目录
├── .env.example           # 环境变量模板
├── requirements.txt       # Python 依赖
└── README.md
```

## 🚀 快速开始

### 1. 安装依赖

```bash
# 建议使用 Python 3.10+ 虚拟环境
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 `OPENAI_API_KEY`（必填）。如使用代理或自建网关，填写 `OPENAI_BASE_URL`。

```env
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.openai.com/v1   # 可选
OPENAI_MODEL=gpt-4o                          # 可选，默认 gpt-4o
```

### 3. 准备测试截图（可选）

若没有真实截图，可生成一张模拟 HobbyStock 风格的样例图：

```bash
python make_sample.py
# 生成 ./screenshots/card_sample.jpg
```

### 4. 运行解析

```bash
# 真实解析（需要 API Key）
python parse_card.py --image ./screenshots/card_sample.jpg

# 指定模型与输出路径
python parse_card.py --image ./screenshots/card_sample.jpg --model gpt-4o --output ./output/result.json

# 无 API Key 时测试输出管线（Mock 模式）
python parse_card.py --image ./screenshots/card_sample.jpg --mock
```

运行后控制台打印 JSON，并保存到 `./output/result.json`。

## 📤 输出示例

```json
{
  "raw_text_extracted": "2025-26 篮球 NBA Topps 盗梦 Inception Hobby #1\n太阳 Phoenix Suns 瑞安·邓恩 Ryan Dunn Rise to Fame Signatures",
  "break_info": {
    "platform": "HobbyStock",
    "product_title": "2025-26 篮球 NBA Topps 盗梦 Inception Hobby #1",
    "year": "2025-26",
    "sport": "Basketball",
    "league": "NBA",
    "brand_series": "Topps Inception",
    "box_type": "Hobby"
  },
  "card_info": {
    "team_cn": "太阳",
    "team_en": "Phoenix Suns",
    "player_cn": "瑞安·邓恩",
    "player_en": "Ryan Dunn",
    "set_subset": "Rise to Fame Signatures",
    "card_number": "RF-RD",
    "parallel_feature": "Auto",
    "numbered": null
  },
  "search_queries": {
    "ebay_query": "2025-26 Topps Inception Ryan Dunn Rise to Fame Signatures RF-RD",
    "katao_query": "25-26 Topps Inception 太阳 瑞安邓恩 特签"
  }
}
```

## 🛠️ 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i, --image` | 待解析截图路径（必填） | — |
| `-m, --model` | 视觉模型名称 | `gpt-4o` |
| `-o, --output` | 输出 JSON 路径 | `./output/result.json` |
| `--prompt` | System Prompt 文件路径 | `./prompts/system_prompt.txt` |
| `--mock` | Mock 模式，不调用 API，返回示例 JSON | 关闭 |
| `--pretty` | 输出格式化 JSON（缩进） | 开启 |

## 🔧 调优 Prompt

解析质量很大程度上取决于 Prompt。所有提取规则、中英映射、搜索词格式都集中在 `prompts/system_prompt.txt`，可直接编辑后重新运行，无需改代码。

例如要新增球队映射，在「提取规则」中追加：`76人→Philadelphia 76ers`。

## 🧩 兼容其他 Vision 模型

本脚本基于 OpenAI SDK，兼容任意 OpenAI Chat Completions 协议的视觉接口：

- **OpenAI gpt-4o / gpt-4o-mini**：开箱即用。
- **DeepSeek / 通义千问 VL / 智谱 GLM-4V** 等：若提供 OpenAI 兼容端点，设置 `OPENAI_BASE_URL` 与对应 `OPENAI_MODEL` 即可。
- **Claude 3.5**：协议不同，需改用 `anthropic` SDK，参考脚本中 `parse_card()` 自行适配 messages 结构。

## ❓ 常见问题

- **`未配置 OPENAI_API_KEY`**：先 `cp .env.example .env` 并填入 Key，或加 `--mock` 测试输出管线。
- **JSON 解析失败**：模型偶发返回带 Markdown 代码块的内容，脚本已自动剥离；如仍失败，可在 `prompts/system_prompt.txt` 末尾加强「只返回纯 JSON」的约束。
- **中文字体缺失（make_sample.py）**：脚本优先用 Windows 自带微软雅黑，缺失时自动回退默认字体。
- **图片过大**：建议单张 < 5MB；如需压缩可在 `parse_card.py` 中加入尺寸校验。

## 📄 依赖

- `openai>=1.40.0`
- `python-dotenv>=1.0.0`
- `Pillow>=10.0.0`（仅 make_sample.py 需要）
