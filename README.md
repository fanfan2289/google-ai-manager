# Google AI Manager

> **Disclaimer**: Unofficial open-source community tool. Not affiliated with, endorsed by, or sponsored by Google LLC.

**Google AI Manager** 是一款面向 Google AI 生态的原生桌面算力与额度管理工具（Windows Edge WebView2 + 系统托盘常驻），目前已完成 **Colab GPU 余量监控、自动抢卡锁卡、静默挂载 Google Drive、超时自动关机防扣费** 以及 **云端工作流一键接管** 核心能力，并正持续演进为支持**多 Google 账号**与**多服务额度聚合**（Colab CCU、Google Drive 容量、Gemini / AI Studio API、GCP / Vertex 等）的统一管理面板。

---

## 核心特性（v2.0-hold 当前已实现）

- **原生轻量桌面端 + 系统托盘常驻**（`desktop_app/colab_gpu_tools.py`）
  - 基于 Windows 原生 Edge WebView2（`pywebview`）+ `pystray`，无浏览器地址栏、无黑框控制台、支持单实例唤醒。
  - 关闭窗口默认最小化到系统托盘继续后台监控；托盘图标随状态变色（绿=监控中、琥珀=已锁卡倒计时、灰=暂停、红=残留告警）。
- **GPU 余量监控 + 命中即占卡 + 超时自动关机**（`scripts/colab_gpu_monitor.py`）
  - 支持监控 `G4 (96GB)`、`A100`、`H100`、`L4`、`T4`，503 缺货全程 `0 CCU` 消耗，SQLite 持久化记录并生成 24 小时放卡规律热力图。
  - 双模式一键切换：**「命中即占卡」** vs **「仅监控秒释（<1s 自动释放，0 扣费）」**。
  - **自动静默挂盘**：抢到 GPU 锁定后自动执行 `colab dfs mount` 挂载 Google Drive，云盘模型与文件直接就绪。
  - **无人认领自动关机**：默认 15 分钟倒计时（界面支持 `10 / 15 / 20 / 30 分钟` 一键切换与 `+10 / +20 分钟` 续期），到期未使用由独立后台看门狗强杀并复核 `0` 残留。
- **三重防幽灵计费守卫**（`scripts/colab_meter.py`）
  1. 服务端全量枚举强杀（包括网页端/异常断连遗留的 `?` 孤儿实例）并轮询复核至 `0` 活跃会话。
  2. Bash 全信号兜底（`EXIT / INT / TERM / HUP`）+ Windows `finally` 双重钩子。
  3. 独立后台死人开关看门狗（`colab_meter.py watch`），防止终端强关或进程异常挂死持续扣 CCU。
- **云端工作流一键接管**（`scripts/wsl_run_h3.sh` + `cloud_workstation/`）
  - 支持 `--reuse-session` 直接复用已锁定并挂好云盘的 GPU 会话执行云端任务（内置 ComfyUI / MiniMax-H3 工作流示例），跑完自动拉取产物并强杀关机对账。

---

## 路线图（Roadmap）

- [x] **v2.0**：Colab GPU 余量监控、命中即占卡、自动静默挂盘、15 分钟超时自动关机、Edge WebView2 原生桌面端 + 系统托盘
- [ ] **Phase 1（多账号与通用化）**：
  - [x] **M1** `providers/` 额度插件层（Colab CCU、Google One 存储容量）+ 可视化网络代理设置（自动探测 / 手动 / 直连，配置保存在 `~/.google-ai-manager/`）
  - [x] **M2** 图形化多 Google 账号中心（添加 / 切换 / 删除 / 标签备注 / OAuth + 云盘授权引导）+ 多账号额度聚合看板
  - [ ] **M3** 指定账号抢卡 + 主号 CCU 不足 / 限流时自动切换备用号
  - [ ] **M4** Antigravity / Gemini CLI 额度、Pro 权益中心、远端任务模板、首次启动环境自检
- [ ] **Phase 2（多服务额度插件生态）**：
  - `providers/` 插件抽象：接入 Gemini / AI Studio API Key 池、GCP / Vertex AI 配额等其他 Google AI 额度
  - 核心监控与抢卡脱离 WSL2 强依赖，支持纯原生 Windows / macOS / Linux 运行与自动构建安装包

---

## 目录结构

```text
google-ai-manager/
├── providers/                    # 额度插件层：colab / google_storage / accounts_hub(多账号) / netcfg(代理) / paths(~/.google-ai-manager)
├── tests/                        # 离线单元测试（python -m unittest discover -s tests）
├── desktop_app/
│   ├── colab_gpu_tools.py        # Windows 原生 Edge WebView2 桌面外壳 + 系统托盘
│   └── colab_gpu_tools.ico       # 应用图标
├── scripts/
│   ├── colab_gpu_monitor.py      # GPU 探测、命中锁卡、自动挂盘、1s 倒计时看门狗与本地 HTTP/SSE 服务
│   ├── monitor_ui.html           # 桌面客户端前端界面（左侧边栏 + 多视图仪表盘）
│   ├── colab_meter.py            # CCU 对账、服务端全量强杀与死人开关看门狗
│   ├── wsl_run_h3.sh             # 云端任务流水线（支持 --reuse-session 复用已锁 GPU）
│   ├── wsl_auth.sh               # Colab + DriveFS 首次 OAuth 授权脚本
│   ├── wsl_setup_env.sh          # WSL2 环境初始化脚本
│   ├── wsl_download_models.sh    # 零 GPU 成本拉取模型到 Google Drive
│   ├── sync_workstation_to_drive.py
│   ├── pull_latest_outputs.py
│   └── Invoke-ColabH3.ps1        # PowerShell 统一调用入口
├── cloud_workstation/            # 示例云端工作流脚本与配置模板
├── 0_下载模型到云盘_DownloadModels.bat
├── 1_首次授权_Auth.bat
├── 2_一键运行短剧_RunH3.bat
├── 3_紧急强杀与CCU对账_StopAll.bat
├── 4_查看状态与CCU余额_Status.bat
├── 5_显卡余量监控看板_Monitor.bat
├── requirements-desktop.txt      # Windows 桌面壳 Python 依赖
├── AGENTS.md / CLAUDE.md         # AI Agent 协作上下文指南
└── LICENSE
```

---

## 快速开始（Windows + WSL2）

### 1. 环境准备
- **Windows 侧**：安装 Python 3.11+（系统自带 Microsoft Edge WebView2 Runtime），创建桌面端虚拟环境：
  ```powershell
  py -3.12 -m venv .venv-desktop
  .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt
  ```
- **WSL2 侧（Ubuntu）**：初始化 `uv` + `google-colab-cli` + `colab-ext`：
  ```powershell
  wsl.exe -d Ubuntu -e bash scripts/wsl_setup_env.sh
  ```

### 2. 首次授权 Google 账号
双击运行 `1_首次授权_Auth.bat`（或运行 `.\scripts\Invoke-ColabH3.ps1 auth`）：
1. 完成 Colab CLI 的 Google OAuth 登录（凭据仅保存在本机 WSL `~/.config/colab-cli/`，权限 `0600`，不会上传仓库）。
2. 完成 `colab dfs consent` 云盘静默挂载授权（如出现权限复选框请勾选 Drive 文件访问权限）。

### 3. 启动桌面客户端
双击 **`5_显卡余量监控看板_Monitor.bat`** 即可启动原生桌面客户端（传入 `--web` 参数可回退至浏览器模式 `http://127.0.0.1:8765`）。

---

## 常用辅助入口

- `3_紧急强杀与CCU对账_StopAll.bat`：一键强杀账号下所有云端 VM（含孤儿实例），复核 `0` 活跃会话并输出 CCU 余额。
- `4_查看状态与CCU余额_Status.bat`：查询当前活跃实例、实时费率（`CCU/hr`）与剩余 CCU。
- PowerShell 命令行预检：
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\Invoke-ColabH3.ps1 dry-run
  ```

---

## 安全与隐私说明

- 所有 OAuth Token、Drive 凭据、运行日志（`logs/`）、SQLite 探测库（`*.db`）、锁卡状态（`active_hold.json`）均已被 `.gitignore` 严格排除，**请勿将任何凭据或日志文件提交至公开仓库**。
- 欢迎通过 Issue / PR 共建多账号与多 Google AI 额度模块。
