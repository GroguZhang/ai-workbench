# DrZ 健身仪表盘

基于「健身与身体数据分析智能体」逻辑的本地 Web 应用。
苹果健康 / 训记 Open API 数据 → 群晖同步 CSV → 本地缓存 → 7 日均线分析 → LLM 深度诊断。

**支持多机部署**：代码随群晖 Drive 同步，任何一台机器双击 `run.bat` 即可用，数据自动一致。

## 快速启动（任何机器）

**Windows**: 双击 `run.bat`
**macOS / Linux**: `./run.sh`

首次运行自动在本机创建虚拟环境并安装依赖（约 2-3 分钟），之后秒起。
浏览器访问 `http://localhost:8501`。

## 多机架构（重要）

```
群晖同步目录（随 Drive 在多机间同步）
├── FitnessDashboard/     ← 本项目：代码、页面、README、.env.example
└── 健身记录/data/body_records.csv   ← 唯一权威数据源（21 列）

各机器本地（不同步，绝不进同步盘）
└── ~/.drzfitness/
    ├── .venv/       ← 每台机器各自的虚拟环境
    ├── fitness.db   ← SQLite 缓存（可随时从 CSV 重建）
    └── .env         ← API Key（不落 NAS）
```

**为什么这样拆**：
1. SQLite 写入时会被同步客户端抓取到损坏的中间状态——数据库绝不能放同步目录
2. `.env` 含 API Key，放同步目录等于落盘 NAS
3. venv 体积大且平台相关，各机器自建

**数据一致性机制**：打开仪表盘时自动做一次双向同步——先从权威 CSV 拉取（另一台机器的录入优先），再把合并结果写回 CSV。SQLite 只是本机缓存，坏了删掉即可从 CSV 重建。

命令行手动同步：`python sync_workspace.py`（`--status` 看路径诊断）

## 功能

| 模块 | 页面 | 功能 |
|---|---|---|
| 数据录入 | 📝 | 手动表单 / CSV·JSON 导入导出 / 历史记录管理 / 写回权威 CSV |
| 趋势分析 | 📈 | 睡眠+7h目标线 / 心率·HRV / 步数 / 训练容量周对比 / 消耗走势 |
| AI 教练 | 🤖 | 一键深度诊断（LLM）/ 恢复状态评估 / 方案归档 |
| 设置 | ⚙️ | API Key 配置 / 多机同步状态 / 路径诊断 |

## 新机器部署（三步）

1. 确认群晖 Drive 已把本目录同步到本机
2. 双击 `run.bat`（首次自动建环境）
3. 到「⚙️ 设置」页填入 API Key（或编辑 `~/.drzfitness/.env`）

搞定。数据会自动从 CSV 拉下来。

## 配置大模型 API

编辑 `~/.drzfitness/.env`（或通过「设置」页）：

```
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

支持所有 OpenAI 兼容接口：DeepSeek / OpenAI / 本地 Ollama。

## 内置分析纪律（不可绕过）

1. 不用单日读数做判断，一律看 7 日移动平均斜率
2. BIA 体脂秤：水分波动连带污染肌肉量/骨骼肌/FFM（同一算式的输出，非独立证据）
3. 体脂量(kg) 是唯一可信的短期信号
4. 前 14 天数据不计入 TDEE 计算
5. 内脏脂肪等级单级跳变无意义

## 服务守护（自动拉起）

Streamlit 是普通进程，会被系统清理或崩溃后无人接管，因此配了守护：

- **watchdog.py** —— 检测 8501 端口，未监听则用 `~/.drzfitness/.venv` 重新拉起（子进程脱离父进程存活）
- **Windows 计划任务** `DrZ-FitnessDashboard-Watchdog` —— 每 10 分钟巡检一次，隐藏运行无窗口

```bash
python watchdog.py --check   # 只检测，输出 ALIVE / DOWN
python watchdog.py           # 检测并在挂掉时拉起（正常情况计划任务会自己做）
```

**幂等性**：服务存活时重复执行不会起第二个实例（端口占用检测）。

计划任务管理（PowerShell）：

```powershell
Get-ScheduledTask -TaskName "DrZ-FitnessDashboard-Watchdog"                         # 查看
Unregister-ScheduledTask -TaskName "DrZ-FitnessDashboard-Watchdog" -Confirm:$false  # 撤销
```

> 注：watchdog.py 会随群晖同步到其他机器，但**只有注册了计划任务的那台才自动巡检**，不会冲突。

## 排障

| 症状 | 处置 |
|---|---|
| 打不开 8501 | 等 10 分钟让守护自愈，或手动 `python watchdog.py` |
| 权威 CSV 未找到 | 设置环境变量 `DRZ_WORKSPACE_CSV` 指向 CSV 绝对路径 |
| SQLite 损坏 | 删掉 `~/.drzfitness/fitness.db`，重启即从 CSV 重建 |
| 想换本机运行时目录 | 环境变量 `DRZ_LOCAL_DIR` |
| 改代码后回归验证 | 见 skill fitness-daily-ingest 中的 AppTest 命令 |
