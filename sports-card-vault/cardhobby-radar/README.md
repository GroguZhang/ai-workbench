# CardHobby Radar · 卡淘捡漏与 Topps 官方发售监控守护进程

> 24 小时全自动运行，采用 **规则初筛 + DeepSeek 语义终审 + 成交基准影子对账 + Topps 官方发售雷达** 复合架构，秒级打假、精准捡漏、拦截残卡、第一时间推送官方原盒放货与抽签。

## 核心架构与功能特性

```
                               ┌──────────────────────────────────────────────────────────┐
                               │                    全天候实时监控多源数据                 │
                               └────────────────────────────┬─────────────────────────────┘
                                                            │
                     ┌──────────────────────────────────────┴──────────────────────────────────────┐
                     ▼                                                                             ▼
    【卡淘全网在售数据】 (每日数万条拍卖)                                           【Topps 中国官方发售与抽签通道】
                     │                                                             ├─ 微信小程序「Topps收藏卡」
                     ▼                                                             ├─ 微信公众号「TOPPS中国」发售推文
    【Stage 1: 毫秒级规则高速初筛】                                                 └─ 淘宝/天猫「TOPPS官方旗舰店」
      ├─ 英文名 Token 精准消歧（彻底拦截单名撞车/音译前缀）                                            │
      ├─ 8 档限量编号与特殊编识别 (👑球衣同号编+25% / 首编 / 尾编)                                      ▼
      ├─ 三级卡品分级折价 (轻微×0.90 / 中等×0.75 / 严重×0.50，剔除免责词)                 【ToppsDropRadar 智能发售雷达】
      ├─ 假签名与特卡冒充拦截 (Kaboom/Downtown/Color Blast 等纯特卡)                 ├─ 搜狗微信全网发售推文监听
      ├─ 贴纸签流动性折算 (-20%) 与 兑换板折价 (-45%)                                 ├─ DeepSeek 结构化信息抽取
      └─ 动态 EB 收缩估值校准与利润封顶截断                                              (系列名/渠道/原价/套利溢价/截止时间)
                     │                                                             ├─ 状态指纹库去重防轰炸
                     ▼ 候选真漏池 (约 10~25 张潜在目标)                                └─ 活跃发售同步至 Card Vault
    【Stage 2: DeepSeek 大模型智能终审门禁】                                                       │
      ├─ 真假手签判定：拦截 Signature Class/Series 等非签字特卡伪装                                   │
      ├─ 跨界同名判定：拦截 WWE 摔角手 Tatum Paxley、Vinicius Souza                                  │
      ├─ 货号与数据打假：拦截卖家仓位号 (如 多米8/45)、新秀统计数据                                       │
      └─ 风险声明拦截：精准识别“面签不保真”、“印刷签名”等免责声明                                          │
                     │                                                                             │
                     └──────────────────────────────┬──────────────────────────────────────────────┘
                                                    │
                                                    ▼
                       PushPlus (专属红色发售/捡漏卡片) / Bark (警报级锁屏提醒) / 企业微信
```

### 1. 三级卡品精细化惩罚 (3-Tier Condition Penalty)
* **分离商家免责套话与真实物理损伤**：卡淘商家常在标题标注“微瑕 / 卡品如图 / 默认白边”避免纠纷。系统建立两级词表，将免责套话剥离，**消除对近 50% 正常好卡的误杀**。
* **分级折价系数**：
  * **轻微瑕疵**（轻微白边、偏印、印刷线）：估值红线 $\times 0.90$
  * **中等瑕疵**（划痕、折痕、压痕）：估值红线 $\times 0.75$
  * **严重残卡**（角损、破角、破损、残卡、水渍）：估值红线 $\times 0.50$

### 2. 资深藏家进化版规则 (Collector Evolutions)
* **球衣同号编神级溢价**：内置 42 位顶流球星专属球衣号知识库（如文班亚马 #1、东契奇 #77、亚马尔 #19、库里 #30 等），命中同号球衣编自动附加 **+25% 收藏溢价**，首编/尾编附加 **+15% 溢价**。
* **特卡冒充签字拦截防线**：全面封堵 Kaboom、Downtown、Color Blast、Press Proof 等纯特卡系列蹭搜签字。
* **印刷签名识别 (`is_printed_auto`)**：拦截“印签”、“印刷签名”假签字。
* **贴纸签流动性惩罚**：针对贴纸签流动性较差的特点，自动施加 $\times 0.80$ 折扣并在通知中提示。

### 3. Topps 官方放货与抽签监控雷达 (`ToppsDropRadar`)
* **全渠道监听**：覆盖微信小程序 **「Topps收藏卡」**、微信公众号 **「TOPPS中国」** 与天猫 **「TOPPS官方旗舰店」**。
* **超高套利空间捕获**：官方原盒发售（如 Topps Chrome、UCC、Merlin 等 Hobby 盒）往往比二级市场便宜 30%~60% 以上，雷达全天候巡检（每 15 分钟），第一时间推送。
* **DeepSeek 结构化抽取**：自动解析推文提取系列、价格、套利空间与报名截止时间。
* **S 级图文推送**：PushPlus 专属醒目红色渐变卡片与 Bark 警报铃声锁屏强提醒。

---

## 运行模式与 CLI 命令

```bash
# 常驻守护进程（每日 17:00 摸底 + 19:20 终极名单，23:00 AI 对账复盘，每 15 分钟 Topps 放货巡检）
python3 cardhobby_sniper_radar.py --mode daemon --run-on-start

# 单独运行一次 Topps 官方放货/抽签全网扫描并推送
python3 cardhobby_sniper_radar.py --mode topps

# 独立运行 ToppsDropRadar 网络探针 (查看近期官方发售，不发推送)
python3 topps_drop_radar.py --probe --lookback 72

# 手动触发每日截标大模型深度复盘与自主学习
python3 cardhobby_sniper_radar.py --mode learn-closed --lookback-hours 48

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

### Topps 官方监控：系列白名单与通知阈值

四项全部留空 / 填 `0` = 维持原有「全系列、全量推送」行为，**改策略不用改代码**。

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `TOPPS_SERIES_WHITELIST` | 空 | 只监控指定卡盒系列，逗号分隔。中英文与昵称等价（`sapphire` 即「蓝宝石」）。留空 = 全部系列 |
| `TOPPS_MAX_OFFICIAL_PRICE` | `0` | 官方原价上限（元），超过不推送。`0` = 不限价 |
| `TOPPS_DEADLINE_WITHIN_HOURS` | `0` | 仅在距登记截止 N 小时内推送。`0` = 发现即推 |
| `TOPPS_MIN_SCORE` | `0` | 最低优先级分（0–100）。`0` = 不卡分 |

可用的标准系列名：`chrome` / `sapphire` / `bowman` / `merlin` / `finest` / `flagship` /
`ucc` / `monarch` / `sterling` / `pristine` / `cosmic` / `museum` / `dynasty` /
`inception` / `tribute` / `definitive` / `tier_one` / `allen_ginter` / `topps_now` /
`chrome_black` / `gpk`。未收录的自定义系列名按子串匹配，无需改代码。

```bash
# 示例：只盯 Chrome 与蓝宝石、3000 元以内、24 小时内截止、优先级 40 分以上
TOPPS_SERIES_WHITELIST=chrome,sapphire
TOPPS_MAX_OFFICIAL_PRICE=3000
TOPPS_DEADLINE_WITHIN_HOURS=24
TOPPS_MIN_SCORE=40
```

**门禁行为**：白名单在初筛阶段硬过滤（不进入解析）；原价上限、提醒窗口、优先级分为软门禁。
被暂缓的条目不写入 `seen_articles`，下一轮会重新评估——截止时间越近分数越高，到点仍会正常推送。
优先级评分维度：命中关注系列 +40、有明确原价 +20、官方抽签 +15、距截止在窗口内 +30、
含溢价信号 +15、含稀缺/首发信号 +10。

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
