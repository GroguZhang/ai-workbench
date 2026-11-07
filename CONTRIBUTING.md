# 贡献指南

这是一个**个人作品集仓库**，不是通用框架。所以先说清楚什么受欢迎、什么大概率不会被合入。

## 受欢迎的

- **Bug 报告**：尤其是数据正确性问题——比如某个标题被解析错的限量编、某个筛选规则把好卡误杀
- **可复现的错误用例**：给一条真实的（脱敏后）输入 + 期望输出，比任何长篇描述都有用
- **测试补充**：本项目的测试基本都是「先踩坑、再钉住」，同类思路的测试非常欢迎
- **文档纠错**：错别字、命令跑不通、表格渲染坏掉

## 大概率不会被合入的

- **大规模重构 / 换技术栈**：生产系统在跑，重写风险远大于收益
- **引入新框架或依赖**：当前刻意保持轻量（部分模块只用标准库，测试零依赖就能跑）
- **纯风格调整**：格式化、改名、加抽象层

如果确实认为某个方向值得改，**先开 issue 讨论**，不要直接提 PR。

---

## 本地跑测试

每个能测的模块都是独立可跑的，互不依赖：

```bash
# card-vault（需要先装 requirements）
cd sports-card-vault/card-vault && pip install -r requirements.txt pytest && python -m pytest

# cardhobby-radar（仅标准库）
cd sports-card-vault/cardhobby-radar && python -m pytest

# vision-break-parser（仅标准库）
cd sports-card-vault/vision-break-parser && python -m pytest

# scrapling-assistant（需要 fastapi + httpx）
cd sports-card-vault/scrapling-assistant && pip install fastapi httpx pytest && python -m pytest

# fitness-dashboard（仅标准库）
cd productivity-apps/fitness-dashboard && python -m pytest
```

## 提交前自查

1. `git status` 里没有 `.env`、`*.db`、`uploads/`、`*.jsonl`
2. 没有硬编码的 Key、Token、私有 IP、本机绝对路径
3. 改动涉及判断逻辑时，附上对应的测试

具体的安全规范见 [SECURITY.md](./SECURITY.md)。
