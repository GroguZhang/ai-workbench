# CardHobby Radar · 卡淘捡漏监控守护进程

> 24 小时轮询卡淘在售列表，按「球员 × 编数档位」的买入上限做比价，把低于阈值的标的推送出来。

## 工作原理

```
watchlist.json          关键词 / 球员 / 英文名 / 必含词 / 排除词 / 多档位买入上限
      │
      ▼
卡淘搜索接口 ──► 原始候选 ──► 规则引擎 ──► 命中 ──► hits.jsonl
                                │
                                ├─ 同名球星消歧（英文名 token 比对）
                                ├─ 假编/假尾编识别（正则 + 白名单）
                                ├─ 系列噪声词与非自动排除
                                └─ 价格校准（calibration.json / learned_rules.json）
                                        │
                                        ▼
                                PushPlus / Bark / 企业微信 Webhook
```

## 快速开始

```bash
cp .env.example .env
cp config/watchlist.example.json config/watchlist.json   # 换成你自己关注的球员
docker compose up -d
docker logs -f cardhobby-radar
```

## 运行模式

```bash
python3 cardhobby_sniper_radar.py --mode daemon --run-on-start   # 常驻轮询
python3 cardhobby_sniper_radar.py --keyword "Alcaraz"            # 单关键词试跑
python3 cardhobby_sniper_radar.py --backtest                     # 用历史数据回测
```

## watchlist.json 结构（节选）

```json
[
  {
    "keyword": "阿尔卡拉斯",
    "alt_keywords": ["阿卡"],
    "en_name": "Carlos Alcaraz",
    "sport": "网球",
    "tier_group": "T0_超巨",
    "must_contain_any": ["alcaraz", "carlos", "阿卡", "阿尔卡拉斯"],
    "exclude_words": ["chronicles", "ruben", "足球", "鲁本"],
    "tiers": {
      "s_1_10":   { "max_buy_rmb": 6050, "ebay_ref_rmb": 15080, "desc": "/10 及以下极低编签" },
      "s_15_25":  { "max_buy_rmb": 3080, "ebay_ref_rmb": 7800,  "desc": "/25 橙折/超低编签" }
    }
  }
]
```

仓库内的 `config/watchlist.example.json` 只含 3 位球星的示例档位；真实的 `config/watchlist.json` 已加入 `.gitignore`，**你的盯盘范围与出价策略不会被公开**。

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `PUSHPLUS_TOKEN` | 空 | PushPlus 微信推送 token，留空跳过 |
| `BARK_URL` | 空 | Bark 推送地址，形如 `https://api.day.app/xxxx` |
| `WECHAT_WORK_WEBHOOK` | 空 | 企业微信群机器人 Webhook，留空跳过 |
| `DEFAULT_SHIPPING_RMB` | `12.0` | 兜底首重运费（接口未返回时使用） |
| `USE_REAL_POSTAGE` | `1` | `1` 优先用卡淘真实运费 |
| `PUSH_WHEN_EMPTY` | `0` | `0` 零命中不推送 |
| `REQUEST_DELAY_SEC` | `2.0` | 关键词间请求间隔，请勿调得过低 |
| `CARDHOBBY_PAGE_SIZE` | `60` | 单页条数 |
| `CARDHOBBY_COOKIE` / `CARDHOBBY_TOKEN` | 空 | 需要登录态时填写 |
| `CALIB_*` / `BLIND_*` / `CONDITION_PENALTY` | 见源码 | 价格校准与「盲盒估值」相关调参 |

## 输出文件（全部在 `.gitignore` 内）

| 文件 | 内容 |
| :--- | :--- |
| `data/hits.jsonl` | 命中记录（个人真实捡漏流水） |
| `data/raw_candidates.jsonl` | 原始候选快照 |
| `data/closed_fills.jsonl` | 已成交回填，用于校准 |
| `data/calibration.json` | 校准系数 |
| `data/learned_rules.json` | 人工标注沉淀的规则 |
| `data/watch_track.json` | 盯盘跟踪状态 |
| `data/radar_snapshots.json` | 快照 |
| `logs/` | 运行日志 |

> 以上文件均含个人真实交易信息，**严禁提交**。

## 与 Card Vault 的联动

两个容器通过**方向对等的只读挂载**交换数据，任一方都改不到对方的数据库：

- `card-vault` 只读挂载 `cardhobby_radar/data` → 展示命中
- `cardhobby-radar` 只读挂载 `card-vault/data/radar_ctl` → 接收手动刷新请求

---

MIT License · 见仓库根 `LICENSE`
