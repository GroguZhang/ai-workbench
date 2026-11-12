# AI Skills · 提示词与自建 Agent 技能

> 这里收集我在日常 AI 协作中沉淀下来的、可复用的 **Agent Skill**（System Prompt + 可执行脚本）。
> 每个 skill 都是一个自包含目录：`SKILL.md` 描述触发条件与方法论，配套脚本负责实测。

## 已收录

| Skill | 用途 | 触发场景 |
| :--- | :--- | :--- |
| [gemini-api-probe](./gemini-api-probe/) | 实测 Gemini API 真实可用性：验活 Key、拉可用模型、探测 thinking 参数、把失败分成五类 | 遇到 Gemini 报错、模型选型、`User location is not supported`、`Failed to fetch` |
| [publication-scan-ocr](./publication-scan-ocr/) | 扫描出版物 → 可搜索 PDF + 分章节索引 + 教研派生件（速查表 / Prompt 模板 / 课件母版），附 11 个可执行脚本 | 收到纯图片扫描件、要求"能搜正文""做教参库/资源库""建曲目索引" |
| [music-courseware-standard](./music-courseware-standard/) | 音乐课堂课件双轨制硬规范：学生向极简页面 + 教师向演讲者备注，附 9 项交付自检清单 | 生成 / 修改 / 重做音乐课件、教学 PPT、课堂演示文稿 |

## 为什么值得单独成 skill

模型名与参数几个月就会失效一次，而「凭记忆猜」是最贵的错误来源。这类知识写成 skill 的价值在于：**下次遇到同类问题时，不需要重新踩一遍坑，直接按方法论实测出当下的事实。**

另外两个 skill 的共同点：它们约束的不是「怎么写代码」，而是**工程判断**——什么时候该停下取证、什么指标算异常、哪些"优化"其实是白丢信息。

## Skill 目录约定

```
<skill-name>/
├── SKILL.md     # YAML front-matter(name/description/触发词) + 方法论正文
├── README.md    # 可选：给 GitHub 浏览者的入口说明（为什么需要它 / 实测数据 / 用法）
└── *.sh|*.js|scripts/   # 可选：把「实测」这一步脚本化
```

---

MIT License · 见仓库根 `LICENSE`
