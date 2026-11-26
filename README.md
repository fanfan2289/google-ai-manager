# Google AI Manager

> **Disclaimer**: Unofficial open-source community tool. Not affiliated with, endorsed by, or sponsored by Google LLC.
>
> [English README](./README.en.md)

**Google AI Manager** 是一款面向 Google AI 生态的原生桌面算力与额度管理工具（Windows Edge WebView2 + 系统托盘常驻），目前已完成 **Colab GPU 余量监控、自动抢卡锁卡、超时自动关机防扣费、在锁定 GPU 上运行命令** 以及 **多账号额度管理** 核心能力，并正持续演进为支持**多 Google 账号**与**多服务额度聚合**（Colab CCU、Google Drive 容量、Gemini / AI Studio API、GCP / Vertex 等）的统一管理面板。

---

## v2.4 新增：Google Flow 积分（实验性）

- 「设置 → 实验功能」开启后，在「账号管理」的账号卡片上点「🔗 连接 Flow」：桌面端为该账号打开一个**专用的 Edge 浏览器配置**（`%LOCALAPPDATA%\GoogleAIManager\flow\<账号>`，与你平时的浏览器完全隔离），在其中登录一次 `flow.google.com` 即可。
- 之后桌面端每 30 分钟在后台无窗口打开 Flow，在**页面内**调用 Flow 自己的只读余额查询（与头像菜单显示的余额一致），只把数字交给后端；登录令牌不离开浏览器页面，本工具不读取、不保存、不记录。登录的 Google 账号与本工具账号不一致时不显示数字。
- 权益中心与账号卡片显示实时余额；读取失败时保留上次的余额并提示原因。非官方接口，随时可能失效；查询方法参考 [gflow-cli](https://github.com/ffroliva/gflow-cli)（未复用代码）。

## v2.3 变更：Colab 会话管理 + 通用化（不再绑定短剧工作流）

- **☁️ Colab 会话管理**（新页面）：列出所有账号正在运行（正在计费）的 Colab 实例，区分「本工具锁卡 / 命令行 / 未跟踪（网页端或遗留）」；每个会话可在 Colab 网页中打开、打开终端（`colab ssh`，无 SSH 密钥时用 `colab repl`）、查看 GPU / 内存 / 磁盘状态、浏览 / 上传 / 下载文件、查看执行历史、重启内核、单独停止（不影响同账号的其他实例）；新建会话必须设置自动关机时长，并由锁卡看门狗守护。功能设计参考了 [colab-playground](https://github.com/scarletkc/colab-playground) 与 [colab-monitor](https://github.com/hohieuai/colab-monitor)（未复用其代码）。
- **⌨ 在锁定的 GPU 上运行命令**：取代任务模板，「占卡」页面直接填一条 Shell 命令，命令结束后自动关机对账，超时由看门狗强杀；运行器自己控制会话 / GPU / 账号 / 超时参数，命令无法关闭自动关机。
- **☁️ 云盘挂载改为按需**：锁卡后默认不再自动挂载 Google Drive，可在锁卡卡片上点「挂载云盘」，或在设置中重新打开自动挂载；从旧版升级时会自动关闭一次。
- **短剧工作流降级为示例**：`cloud_workstation/`（工作流、剧本与素材）原样保留，改由命令行 `cloud_workstation/run_drama.sh`（或 `2_一键运行短剧_RunH3.bat`）运行；桌面端与后端不再包含任何短剧专用功能。
- 默认锁卡会话名由 `h3work` 改为 `gpuwork`；`scripts/wsl_run_h3.sh` → `scripts/wsl_run_remote.sh`（通用远端命令运行器），`Invoke-ColabH3.ps1` → `Invoke-GAM.ps1`。

## v2.2 新增：多账号与 Google AI Pro 额度中心（Phase 1）

- **👤 多 Google 账号中心**：图形化添加 / 重新登录 / 云盘授权（复制授权码即可，无需命令行）、标签备注、切换、带保护的删除；全部账号 CCU / 实时消耗 / VM / 存储聚合看板。
- **🔁 多账号自动切换**：主号 CCU 低于阈值、429 限流、实例数上限或授权失效时，探测与抢卡自动改用备用号；占卡、看门狗、释放与紧急强杀始终按「占卡的那个账号」执行，支持按账号 / 全部账号强杀。
- **🎁 Pro 权益中心**：Google One 存储、Colab CCU、Antigravity 5 小时 / 每周窗口、Gemini CLI 等实时额度，以及 Flow 积分、AI 积分、Cloud 赠金、Firebase Studio 等官方入口。
- **🛸 Antigravity 额度**：只读 Antigravity Tools 本地缓存，按邮箱关联账号，**不读取、不使用任何令牌**。
- **🌐 可视化代理**与 **🩺 首次启动环境自检**（Colab CLI、账号授权、云盘权限、网络、目录权限等，附修复建议）。

---

## 核心特性（v2.0-hold 起已实现）

- **原生轻量桌面端 + 系统托盘常驻**（`desktop_app/colab_gpu_tools.py`）
  - 基于 Windows 原生 Edge WebView2（`pywebview`）+ `pystray`，无浏览器地址栏、无黑框控制台、支持单实例唤醒。
  - 关闭窗口默认最小化到系统托盘继续后台监控；托盘图标随状态变色（绿=监控中、琥珀=已锁卡倒计时、灰=暂停、红=残留告警）。
- **GPU 余量监控 + 命中即占卡 + 超时自动关机**（`scripts/colab_gpu_monitor.py`）
  - 支持监控 `G4 (96GB)`、`A100`、`H100`、`L4`、`T4`，503 缺货全程 `0 CCU` 消耗，SQLite 持久化记录并生成 24 小时放卡规律热力图。
  - 双模式一键切换：**「命中即占卡」** vs **「仅监控秒释（<1s 自动释放，0 扣费）」**。
  - **按需挂载云盘**：锁卡后可一键执行 `colab dfs mount` 挂载 Google Drive（也可在设置中开启锁卡后自动挂载）。
  - **无人认领自动关机**：默认 15 分钟倒计时（界面支持 `10 / 15 / 20 / 30 分钟` 一键切换与 `+10 / +20 分钟` 续期），到期未使用由独立后台看门狗强杀并复核 `0` 残留。
- **三重防幽灵计费守卫**（`scripts/colab_meter.py`）
  1. 服务端全量枚举强杀（包括网页端/异常断连遗留的 `?` 孤儿实例）并轮询复核至 `0` 活跃会话。
  2. Bash 全信号兜底（`EXIT / INT / TERM / HUP`）+ Windows `finally` 双重钩子。
  3. 独立后台死人开关看门狗（`colab_meter.py watch`），防止终端强关或进程异常挂死持续扣 CCU。
- **远端命令运行器**（`scripts/wsl_run_remote.sh`）
  - 支持 `--reuse-session` 直接复用已锁定的 GPU 会话执行任意 Shell 命令（可选 `--mount-drive`），跑完自动强杀关机对账。
  - `cloud_workstation/` 是一个示例工作流（ComfyUI + MiniMax-H3 短剧），通过 `cloud_workstation/run_drama.sh` 调用运行器。

---

## 路线图（Roadmap）

- [x] **v2.0**：Colab GPU 余量监控、命中即占卡、自动静默挂盘、15 分钟超时自动关机、Edge WebView2 原生桌面端 + 系统托盘
- [x] **Phase 1（多账号与通用化，v2.2）**：
  - [x] **M1** `providers/` 额度插件层（Colab CCU、Google One 存储容量）+ 可视化网络代理设置（自动探测 / 手动 / 直连，配置保存在 `~/.google-ai-manager/`）
  - [x] **M2** 图形化多 Google 账号中心（添加 / 切换 / 删除 / 标签备注 / OAuth + 云盘授权引导）+ 多账号额度聚合看板
  - [x] **M3** 指定账号抢卡 + 主号 CCU 不足 / 限流 / 实例上限 / 授权失效时自动切换备用号 + 按账号 / 全部账号强杀
  - [x] **M4** Antigravity / Gemini CLI 额度、Pro 权益中心、首次启动环境自检、中英文 README
- [x] **v2.3**：Colab 会话管理页面、去除短剧专用功能（改为示例工作流）、锁卡后运行命令、云盘按需挂载
- [x] **v2.4**：Google Flow 积分（实验性，专用浏览器配置 + 页面内只读查询）
- [ ] **会话管理后续**：余额下限自动停机、端口转发（实例端口映射到本机 127.0.0.1）、一键部署插件
- [ ] **Phase 2（多服务额度插件生态）**：
  - 接入 Jules 任务数、Gemini / AI Studio API Key 池、GCP / Vertex AI 配额等其他 Google AI 额度
  - 核心监控与抢卡脱离 WSL2 强依赖，支持纯原生 Windows / macOS / Linux 运行与自动构建安装包

---

## 目录结构

```text
google-ai-manager/
├── providers/                    # 额度插件层：colab / google_storage / antigravity / gemini_cli / accounts_hub(多账号)
│                                 #   benefits(权益中心) / selfcheck / remote_runner(运行命令) / colab_sessions(会话管理) / flow_credits / netcfg / paths
├── tests/                        # 离线单元测试（python -m unittest discover -s tests）
├── desktop_app/
│   ├── colab_gpu_tools.py        # Windows 原生 Edge WebView2 桌面外壳 + 系统托盘
│   ├── flow_credits.py           # Flow 积分读取（专用 Edge 配置 + 页面内只读查询）
│   └── colab_gpu_tools.ico       # 应用图标
├── scripts/
│   ├── colab_gpu_monitor.py      # GPU 探测、命中锁卡、按需挂盘、1s 倒计时看门狗与本地 HTTP/SSE 服务
│   ├── monitor_ui.html           # 桌面客户端前端界面（左侧边栏 + 多视图仪表盘）
│   ├── colab_meter.py            # CCU 对账、服务端全量强杀与死人开关看门狗
│   ├── wsl_run_remote.sh         # 通用远端命令运行器（-c 命令，支持 --reuse-session / --mount-drive）
│   ├── wsl_auth.sh               # Colab + DriveFS 首次 OAuth 授权脚本
│   ├── wsl_setup_env.sh          # WSL2 环境初始化脚本
│   └── Invoke-GAM.ps1            # PowerShell 统一调用入口
├── cloud_workstation/            # 示例工作流：ComfyUI + MiniMax-H3 短剧（命令行使用）
│   ├── run_drama.sh              # 同步到云盘 → 调用运行器 → 拉取成片到 outputs/
│   ├── local/                    # 仅本机运行的辅助脚本（同步、拉取成片、下载模型），不会同步到云盘
│   ├── scripts/                  # 在 Colab 上运行的脚本（ComfyUI 启动）
│   └── workflows/                # 工作流、剧本与素材
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
推荐：启动桌面客户端后打开「👤 账号管理 → 添加账号」，按向导在浏览器授权并粘贴授权码即可（可添加多个账号）。

命令行方式：双击运行 `1_首次授权_Auth.bat`（或运行 `.\scripts\Invoke-GAM.ps1 auth`）：
1. 完成 Colab CLI 的 Google OAuth 登录（凭据仅保存在本机 WSL `~/.config/colab-cli/`，权限 `0600`，不会上传仓库）。
2. 完成 `colab dfs consent` 云盘静默挂载授权（如出现权限复选框请勾选 Drive 文件访问权限）。

### 3. 启动桌面客户端
双击 **`5_显卡余量监控看板_Monitor.bat`** 即可启动原生桌面客户端（传入 `--web` 参数可回退至浏览器模式 `http://127.0.0.1:8765`）。首次启动后可在「🩺 环境自检」查看环境是否就绪。

---

## 常用辅助入口

- `3_紧急强杀与CCU对账_StopAll.bat`：一键强杀账号下所有云端 VM（含孤儿实例），复核 `0` 活跃会话并输出 CCU 余额。
- `4_查看状态与CCU余额_Status.bat`：查询当前活跃实例、实时费率（`CCU/hr`）与剩余 CCU。
- PowerShell 命令行：
  ```powershell
  # 预检授权与 CCU（不开机）
  powershell -ExecutionPolicy Bypass -File .\scripts\Invoke-GAM.ps1 dry-run
  # 抢一台 GPU 运行一条命令，跑完自动关机
  powershell -ExecutionPolicy Bypass -File .\scripts\Invoke-GAM.ps1 run -g L4 -c "nvidia-smi"
  ```
- 示例短剧工作流：`0_下载模型到云盘_DownloadModels.bat`（下载模型）、`2_一键运行短剧_RunH3.bat`（运行 `cloud_workstation/run_drama.sh`）。

---

## 安全与隐私说明

- 所有 OAuth Token、Drive 凭据、运行日志（`logs/`）、SQLite 探测库（`*.db`）、锁卡状态（`active_hold.json`）均已被 `.gitignore` 严格排除，**请勿将任何凭据或日志文件提交至公开仓库**。
- 账号凭据由 colab-ext 保存在 WSL 本机；本工具自身的设置、账号备注保存在 `~/.google-ai-manager/`（目录 0700 / 文件 0600），不进仓库。
- Antigravity / Gemini CLI 仅读取本地缓存与状态文件，从不读取或使用它们的令牌；本地接口拒绝跨站请求。
- 欢迎通过 Issue / PR 共建多账号与多 Google AI 额度模块。
