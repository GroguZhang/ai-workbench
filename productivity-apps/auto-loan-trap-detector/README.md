# 🚗 Auto-Loan-Trap-Detector (车贷金融防坑验谎仪)

![Version](https://img.shields.io/badge/Version-3.0.0-blue.svg)
![Build](https://img.shields.io/badge/Deployment-UniCloud_Serverless-success.svg)
![Workflow](https://img.shields.io/badge/Workflow-AI_Agent_Powered-purple.svg)

一个基于**纯现金流（Cash Flow）**和 **IRR（内部收益率）算法**的前端轻量级计算工具，专为破解 2026 年汽车消费市场复杂的“长贷短还”、“贴息对赌”等金融套路而设计。

## 💡 起因与背景 (Background)

这个项目的诞生源于真实的线下博弈：**陪女朋友去 4S 店买车。**

在与销售交锋的过程中，我发现当下的汽车金融市场套路已经发生了底层变异。过去那种明目张胆收取“金融服务费”、“GPS费”的低级套路已被淘汰，取而代之的是利用**“资金时间价值盲区”**和**“消费者流动性对赌”**的高级话术。

销售通常会抛出极具迷惑性的条件：
> “贷 20 万分 5 年，单利只要 2.4%，店里直接给你返现 1.5 万当贴息。满两年你可以一次性还清尾款，没有任何违约金。”

普通消费者面对这些数字根本无法算出真实的资金成本，很容易被“低月供”和“高返现”蒙蔽。为了打破这种信息差，我决定从“纯现金流”的底层逻辑出发，用 AI 工作流在极短时间内手搓了这个“验谎仪”。

## 🎯 核心业务逻辑 (Core Business Logic)

本项目彻底抛弃了传统房贷/车贷计算器“强迫用户输入利率”的陈旧交互，完全贴合线下 4S 店的真实销售话术盲区，设计了三大核心推演引擎：

1. **基准防坑镜（常规直贷）**：标准的等额本息测算，作为识别其他所有异形方案真实成本的“健康对照组”。
2. **单利扒皮机（4S店常规报价）**：针对销售用“等本等息”伪装低息的套路。引入牛顿迭代法求解 IRR，一秒扒下“单利费率”的伪装，还原真实年化复利。
3. **流动性对赌推演（贴息局底牌揭秘）**：这是本工具的核心差异化功能。针对当前最泛滥的“长贷短还”气球贷，系统会自动进行**双情景推演**：
   - **情景 A（对赌成功）**：按时结清尾款，计算出真实的资金红利（甚至能算出薅了机构多少羊毛）。
   - **情景 B（对赌失败）**：模拟用户资金断裂、被迫还满总期数的灾难性后果，直接计算出将被反噬的高昂额外利息绝对值。

## 🧠 产品与架构思考 (Product & Architectural Thinking)

本项目不仅仅是一段代码，更是**AI 时代超级个体工作流**的落地实践。

* **自适应双擎输入 (Dual-Engine Input)**：考虑到线下销售常常故意隐瞒利率、只报月供，我在前端引入了状态机模型。用户既可以通过“假利率”让 AI 反推月供，也可以直接无视话术，输入“真实月供”让系统逆向测算。完美兼容所有残缺的报价单。
* **Serverless 极速部署**：放弃了沉重的后端架构。采用单文件 HTML/CSS/JS 纯前端构建，直接托管于 DCloud (UniCloud) 的 Serverless 环境，利用边缘 CDN 节点实现全网免备案秒开。
* **防呆设计与防御性编程**：内置 `validateSanity` 校验逻辑，防止用户漏输零导致数学模型崩溃，给予友好的 UI 容错提示。

## 🚀 迭代路径 (Iteration History)

* **v1.0**：实现基础的等额本息与等本等息算法，验证 IRR 数学模型。
* **v2.0**：重构 UI，全面对标原生 App 的暗黑极客质感；加入按期数均摊的“年度还款明细”流水引擎。
* **v3.0**：引入“双擎输入”分段选择器，解决信息残缺问题；优化负利息（赚钱）场景的动态 UI 突变反馈。

## 🛠️ 技术栈 (Tech Stack)

* **Frontend**: HTML5, Vanilla JavaScript, CSS3 (CSS Variables for Theming)
* **Algorithm**: Newton-Raphson method for Internal Rate of Return (IRR)
* **Deployment**: UniCloud Static Web Hosting (CDN Accelerated)
* **Workflow**: LLM Prompt Engineering & AI Pair Programming

---
*声明：本项目为纯公益技术分享，底层数据均在本地浏览器运算，绝不收集任何用户隐私数据。*
