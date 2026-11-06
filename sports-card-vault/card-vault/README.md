# Card Vault · 球星卡私人档案库

> FastAPI + OpenCV 的单人收藏档案系统：录入 → 图片处理 → 居中度预评级 → 估值追踪。

## 能力

| 模块 | 说明 |
| :--- | :--- |
| 档案录入 | 球员 / 年份系列 / 卡种 / 编数 / 成本价 / 估值，支持富文本备注 |
| 图片处理 | OpenCV 透视拉正（两点定框）、任意角度旋转纠正、原图/处理图双存 |
| 居中度测量 | 自动检测卡框四边留白，输出 Centering 比例并给出预评级参考 |
| 资产看板 | 在库数量、成本/估值总计、按球员与运动类型的分布 |
| 价格历史 | `card_price_history` 记录区间中位价、样本数、评级档位与时间窗 |
| 捡漏联动 | `/radar` 页展示雷达命中，支持标记、批准、忽略、重扫 |

## 快速开始

```bash
docker compose up -d --build
# 打开 http://localhost:8088
```

仓库内自带 **`data/cards.mock.db`**（2 张虚构球星卡的演示数据），克隆后即可直接看到完整界面，无需准备真实藏品。

想重建一份干净的 mock 库：

```bash
python init_mock_db.py        # 输出 data/cards.mock.db
cp data/cards.mock.db data/cards.db
```

> 实际运行时数据库名是 `cards.db`；`cards.db` 与 `uploads/` 已被 `.gitignore` 屏蔽，请勿提交真实藏品数据。

## 目录

```
card-vault/
├── app/
│   ├── main.py            # FastAPI 主应用（路由 / 数据访问 / 业务逻辑）
│   ├── photos.py          # OpenCV 图像处理：拉正、旋转、居中度检测
│   └── templates/
│       ├── index.html     # 主看板与录入界面
│       └── radar.html     # 捡漏雷达联动看板
├── init_mock_db.py        # 生成演示用 mock 数据库
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 主要 API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/` | 主看板页面 |
| GET | `/radar` | 雷达联动页面 |
| GET | `/api/cards` | 列表（支持 `sport` / `status` / `sort` 过滤） |
| POST | `/api/cards` | 新建卡片 |
| GET | `/api/cards/{card_id}` | 详情 |
| DELETE | `/api/cards/{card_id}` | 删除 |
| POST | `/api/cards/{card_id}/perspective-crop` | 两点定框透视拉正 |
| POST | `/api/cards/{card_id}/calculate-centering` | 计算居中度 |
| POST | `/api/cards/{card_id}/save-centering` | 保存居中度结论 |
| GET | `/api/cards/{card_id}/centering` | `/corners` | 读取测量结果 |
| GET | `/api/cards/{card_id}/price-history` | 价格历史 |
| POST | `/api/cards/refresh-quotes` | `/api/cards/{card_id}/apply-quote` | 行情刷新与采纳 |
| POST | `/api/ai/parse-title` | `/api/ai/vision-archive` | AI 解析标题 / 视觉归档 |
| POST | `/api/photo/preview` | 图片预览（不落库） |
| GET/POST | `/api/radar/*` | 命中列表、标记、批准、报告、重扫 |

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `DATA_DIR` | `/app/data` | 数据库与雷达数据目录 |
| `UPLOAD_DIR` | `/app/uploads` | 藏品实拍图目录（**勿入版本库**） |
| `SCRAPLING_API_BASE` | `http://scrapling-crawler:8090` | 视觉解析微服务地址 |

---

MIT License · 见仓库根 `LICENSE`
