# CardHobby Radar · 卡淘捡漏监控守护进程 (DeepSeek AI 智能增强版)

> 24 小时轮询卡淘在售拍卖，采用 **规则初筛 + DeepSeek 语义终审** 两阶段架构，结合 **每日截标真实对账自主学习** 机制，秒级打假、精准捡漏、自进化成长。

## 核心架构与工作原理

```
[卡淘全网在售数据] (每日数万条拍品)
        │
        ▼
【Stage 1: 毫秒级规则高速初筛】 (零成本，~4分钟扫完全库)
  ├─ 英文名 Token 精准消歧（彻底拦截单名撞车/音译前缀）
  ├─ 8 档限量编号与特殊编识别 (首编/尾编/卡号/PSA分过滤)
  ├─ 瑕疵卡、整包整盒、兑换板、拼盒多人散包过滤
  └─ 动态 EB 收缩估值校准与利润封顶截断
        │
        ▼ 候选真漏池 (约 10~25 张潜在目标)
【Stage 2: DeepSeek 大模型智能终审门禁】 (耗时 2~3 秒，成本 ~¥0.001)
  ├─ 真假手签判定：拦截 Signature Class/Series 等非签字特卡伪装
  ├─ 跨界同名判定：拦截 WWE 摔角手 Tatum Paxley、Vinicius Souza
  ├─ 货号与数据打假：拦截卖家仓位号 (如 多米8/45)、新秀统计数据
  └─ 风险声明拦截：精准识别“面签不保真”、“印刷签名”等免责声明
        │
        ├─ 拦截项 ──► 写入 llm_rejections.jsonl (零噪音免打扰)
        └─ 通过项 ──► 标注 [🤖 AI终审保真]
                          │
                          ▼
              PushPlus / Bark / 企业微信 Webhook 实时强提醒

──────────────────────────────────────────────────────────────────

【每日闭环: 真实截标对账自学习与规则进化引擎】 (--mode learn-closed)
  └─ 回抓 closed_fills.jsonl 终局真实成交价
  └─ 自动抓取“系统高估但实际极低或流拍”的严重偏差离群样本
  └─ DeepSeek 归因诊断并提炼出精准规则热补丁：
      • new_global_non_auto (全局非签字系列黑名单)
      • new_bogus_serial_keywords (假编号与货号特征词)
      • new_risk_phrases (高危免责短语)
      • player_excludes (特定球员专属排除词)
  └─ 自动写入 learned_rules.json 并生成 daily_learning_report.md
  └─ 无需重启，雷达下一轮扫描立即生效！
```

## 快速开始

```bash
cp .env.example .env
# 填写 DEEPSEEK_API_KEY 与推送 Token
cp config/watchlist.example.json config/watchlist.json   # 换成你自己关注的球员
docker compose up -d
docker logs -f cardhobby-radar
```

## 运行模式与 CLI 命令

```bash
# 常驻守护进程（每日 17:00 摸底 + 19:20 终极名单，23:00 自动夜间 AI 对账自学习）
python3 cardhobby_sniper_radar.py --mode daemon --run-on-start

# 手动触发每日截标大模型深度复盘与自主学习
python3 cardhobby_sniper_radar.py --mode learn-closed --lookback-hours 48

# 单关键词抓取探测
python3 cardhobby_sniper_radar.py --keyword "Alcaraz"

# 纯算法逻辑自检（不依赖网络，不发请求）
python3 cardhobby_sniper_radar.py --mode self-check

# 离线规则回测（用历史数据评估规则拦截率）
python3 cardhobby_sniper_radar.py --backtest
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

## 环境变量配置

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 官方 API Key（开启智能打假必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 推荐使用 deepseek-chat，极速且低成本 |
| `AI_GATEKEEPER_ENABLED` | `1` | 是否开启 Stage 2 终审门禁 |
| `AI_DAILY_LEARN_ENABLED` | `1` | 是否开启每日截标自学习引擎 |
| `PUSHPLUS_TOKEN` | 空 | PushPlus 微信推送 token，留空跳过 |
| `BARK_URL` | 空 | Bark 推送地址，形如 `https://api.day.app/xxxx` |
| `WECHAT_WORK_WEBHOOK` | 空 | 企业微信群机器人 Webhook，留空跳过 |
| `DEFAULT_SHIPPING_RMB` | `12.0` | 兜底首重运费（接口未返回时使用） |
| `USE_REAL_POSTAGE` | `1` | `1` 优先用卡淘真实运费 |
| `PUSH_WHEN_EMPTY` | `0` | `0` 零命中不推送 |
| `REQUEST_DELAY_SEC` | `2.0` | 关键词间请求间隔，请勿调得过低 |
| `CARDHOBBY_PAGE_SIZE` | `60` | 单页条数 |

## 数据输出文件（全部在 `.gitignore` 内）

| 文件 | 内容与说明 |
| :--- | :--- |
| `data/hits.jsonl` | 最终通过 AI 终审的捡漏命中流水 |
| `data/llm_rejections.jsonl` | **AI 终审拦截日志**（记录每张被拦卡片的错误类型与研判深因） |
| `data/daily_learning_report.md` | **每日截标 AI 深度复盘报告**（含离群对账明细与自动提炼的规则补丁） |
| `data/learned_rules.json` | 沉淀的规则库（含用户人工标注与 AI 自学习热更新规则） |
| `data/closed_fills.jsonl` | 截标后的真实成交价对账流水 |
| `data/calibration.json` | 基于真实市场成交的 EB 收缩档位校准系数 |
| `data/watch_track.json` | 截标监控队列状态 |
| `logs/cardhobby_radar.log` | 详细运行日志 |

> 以上文件均含个人实时交易或市场对账流水，**严禁提交至版本控制系统**。

## 测试与质量保障

限量编识别、球员名匹配、评级/卡品/竞争烈度判定是系统的基础，已有回归测试覆盖（仅依赖标准库）：

```bash
pip install pytest
python -m pytest
```

---

MIT License · 见仓库根 `LICENSE`
