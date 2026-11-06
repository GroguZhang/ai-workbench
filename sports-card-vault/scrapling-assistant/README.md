# Scrapling Assistant · 卡片实拍图解析微服务

> 把一张卡淘商品页 / 实拍图，变成结构化字段：球员、年份系列、卡种、编数、平行版本，并生成 eBay 与卡淘搜索词。

## 为什么单独拆一个服务

卡片实拍图的识别需要调用视觉大模型，耗时和失败率都远高于普通请求。把它从 `card-vault` 里拆出来独立成服务后：

- 主应用不会因为一次视觉调用卡住整站
- 视觉模型可以单独换（DeepSeek / OpenAI 兼容接口都行）
- 失败可重试，不影响档案库写入

## 快速开始

```bash
cp .env.example .env      # 填 DSH_API_BASE / DSH_API_KEY
docker compose up -d --build
curl http://localhost:8091/health
```

## API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/health` | 健康检查，回显当前模型与上游地址 |
| POST | `/api/scrape/cardhobby` | 抓取卡淘搜索列表（`keyword`, `max_items`, `mode`） |
| POST | `/api/ai/parse-card` | 纯文本解析：给商品标题，抽结构化字段 |
| POST | `/api/ai/vision-card` | 视觉解析：给图片，抽版型与编数 |

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `DSH_API_BASE` | `http://localhost:18899/v1` | OpenAI 兼容的视觉模型网关地址 |
| `DSH_API_KEY` | 空 | 对应网关的 Key（**请放 `.env`，不要写进代码**） |
| `DSH_MODEL` | `deepseek-official/deepseek-flash` | 使用的模型 |
| `VAULT_API_BASE` | `http://card-vault:8000` | Card Vault 地址，用于回写解析结果 |

> 早期版本把 Key 直接写在了 `app.py` 的 `os.getenv` 兜底值里，已改为纯环境变量注入。

## 端口说明

容器内监听 `8090`；对外映射为 **`8091`**，避免与宿主机上其他占用 `8090` 的服务冲突。改端口只需编辑 `docker-compose.yml` 的 `ports`。

---

MIT License · 见仓库根 `LICENSE`
