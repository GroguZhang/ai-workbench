# AI Skills · 提示词与自建 Agent 技能

> 这里收集我在日常 AI 协作中沉淀下来的、可复用的 **Agent Skill**（System Prompt + 可执行脚本）。
> 每个 skill 都是一个自包含目录：`SKILL.md` 描述触发条件与方法论，配套脚本负责实测。

## 已收录

| Skill | 用途 | 触发场景 |
| :--- | :--- | :--- |
| [gemini-api-probe](./gemini-api-probe/) | 实测 Gemini API 真实可用性：验活 Key、拉可用模型、探测 thinking 参数、把失败分成五类 | 遇到 Gemini 报错、模型选型、`User location is not supported`、`Failed to fetch` |

## 为什么值得单独成 skill

模型名与参数几个月就会失效一次，而「凭记忆猜」是最贵的错误来源。这类知识写成 skill 的价值在于：**下次遇到同类问题时，不需要重新踩一遍坑，直接按方法论实测出当下的事实。**

## Skill 目录约定

```
<skill-name>/
├── SKILL.md     # YAML front-matter(name/description/触发词) + 方法论正文
└── *.sh|*.js    # 可选：把「实测」这一步脚本化
```

---

MIT License · 见仓库根 `LICENSE`
