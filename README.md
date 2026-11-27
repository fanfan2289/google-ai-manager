# Google AI Manager

> **声明**：非官方开源社区工具，与 Google LLC 无关联，也未获其认可或赞助。
>
> [English README](./README.en.md)

**Google AI Manager** 是一款给 **Google AI Pro** 用户用的 Windows 桌面应用（Edge WebView2 + 系统托盘，后端运行在 WSL2）。它把一个或多个 Google 账号每月发放的额度集中到一个面板里，同时提供 Colab GPU 抢卡、锁卡和防扣费保护。

当前版本：**v2.9**

---

## 它能做什么

### 🏠 总览：AI Pro 五项每月额度
| 额度 | 显示内容 | 数据来源 |
|---|---|---|
| ⚡ **Colab 计算单元** | CCU 余额、实时消耗、运行中的实例 | colab 命令行（官方客户端） |
| 🎬 **Flow 积分** | 余额、近 24 小时 / 7 天用量、余额曲线、还能生成约多少条 Veo 视频 | Flow 网页自身的只读查询（实验性） |
| ☁️ **Google Cloud 赠金** | $300 免费试用状态、开发者计划每月 $10、每笔剩余金额与到期日 | 开发者计划权益页与 Cloud 结算页（实验性） |
| 💾 **云盘存储** | 已用 / 总量、云盘文件 / Gmail 与相册 / 回收站构成 | Drive v3 官方接口 |
| 🛸 **Antigravity（反重力）** | Gemini、Claude / GPT 的 5 小时与每周剩余 | Antigravity Tools 本地缓存（只读） |

首页下方的「需要处理」会列出：锁卡中的 GPU、环境自检问题、未领取或快到期的赠金、Flow 余额不足、回收站过大等。

### 👤 多账号
- **一键添加 / 重新登录**：点「开始 OAuth 授权」，在默认浏览器里登录 Google 即自动完成，无需复制授权码；账号名按邮箱自动生成。
- 云盘授权同样一键完成；支持显示名称、标签、颜色、备注，以及带保护的删除。
- 多账号额度汇总；抢卡时主号余额不足、被限流或授权失效会自动切换到备用号。

### ⚡ Colab 算力（一个入口，四个子页）
- **抢卡与锁卡**：探测 G4 / A100 / H100 / L4 / T4 余量（缺货不扣费），命中后可自动锁卡；无人认领到时自动关机。
- **会话管理**：列出所有账号正在计费的实例（本工具锁卡 / 命令行 / 未跟踪），可在 Colab 网页打开、开终端、看 GPU 状态、上传下载文件、重启内核、单独停止、带自动关机的新建会话。
- **运行命令**：在锁定的 GPU 上运行一条命令，结束后自动关机对账。
- **探测统计**：24 小时放卡规律热力图与探测记录。

### 🛡️ 防扣费保护
- 探测命中但不锁卡时，实例在 `finally` 中立即释放并复核为 0。
- 锁卡状态持久化到 `logs/active_hold.json`，由独立的 1 秒看门狗按时关机。
- 运行命令时另有独立的死人开关看门狗，以及 Bash 信号兜底、Windows 侧二次复核。
- 右上角 🛑 一键强杀：关闭相关账号下的全部实例（含孤儿实例）并复核。

### 其他
- 浅色 / 深色主题切换；可视化网络代理设置（自动探测 / 手动 / 直连）；首次启动环境自检。
- 设置分为：通用、网络代理、Colab 与防扣费、实验功能、关于。

---

## 快速开始（Windows + WSL2）

1. **桌面端**（Windows，Python 3.11+）：
   ```powershell
   py -3.12 -m venv .venv-desktop
   .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt
   ```
2. **后端**（WSL2 Ubuntu）：安装 `uv`、`google-colab-cli` 与 `colab-ext`：
   ```powershell
   wsl.exe -d Ubuntu -e bash scripts/wsl_setup_env.sh
   ```
3. **启动**：双击 `desktop_app\start.bat`，或给它建一个桌面快捷方式（加 `--web` 参数改用浏览器打开 `http://127.0.0.1:8765`）。
4. **添加账号**：打开「👤 账号管理 → 添加账号 → 开始 OAuth 授权」。
5. **可选**：在「设置 → 实验功能」打开 Flow 积分与 Cloud 赠金读取，然后在账号卡片上点「🔗 连接」，在弹出的专用 Edge 窗口里登录一次。

### 命令行入口（可选）
统一用 `.\scripts\Invoke-GAM.ps1 <模式>` 调用：

| 模式 | 作用 |
|---|---|
| `stop` | 强杀所有账号的全部 Colab 实例并复核 |
| `status` | 查看各账号的实例与 CCU 余额 |
| `run -g L4 -c "nvidia-smi"` | 抢一台 GPU 运行一条命令，跑完自动关机 |
| `auth` | 命令行方式登录 Colab 与授权云盘（一般用界面的一键授权即可） |
| `monitor --open` | 只启动后端并用浏览器打开面板 |

---

## 目录结构

```text
google-ai-manager/
├── desktop_app/
│   ├── start.bat               # 启动桌面端（双击或建快捷方式）
│   ├── colab_gpu_tools.py      # Windows 桌面外壳：WebView2 窗口 + 系统托盘 + JS 桥
│   └── flow_credits.py         # 专用 Edge 配置的网页读取（Flow 积分、Cloud 赠金）
├── scripts/
│   ├── colab_gpu_monitor.py    # 后端：探测、锁卡、看门狗、会话管理、REST + SSE 接口（127.0.0.1:8765）
│   ├── monitor_ui.html         # 前端界面（单文件）
│   ├── colab_meter.py          # CCU 对账、全量强杀、死人开关看门狗
│   ├── gam_oauth_loopback.py   # 一键登录（本机回调）
│   ├── wsl_run_remote.sh       # 在 GPU 上运行命令的通用运行器
│   ├── wsl_setup_env.sh / wsl_auth.sh / Invoke-GAM.ps1
│   └── static/                 # 关于页图片
├── providers/                  # 额度插件：colab / google_storage / antigravity / flow_credits / gcp_credits
│                               #   accounts_hub(多账号) / colab_sessions / remote_runner / benefits / selfcheck / netcfg / paths
└── tests/                      # 单元测试
```

---

## 安全与隐私

- **令牌**：Google 账号凭据由 colab-ext 保存在 WSL 本机（权限 0600），本工具不接触、不保存任何令牌。一键登录只把一次性授权码交给官方 `colab` 命令行。
- **网页读取**：Flow 积分与 Cloud 赠金在每个账号专用的 Edge 配置里、在网页内部完成查询，只把数字和状态传回应用；登录状态、Cookie、邮箱、结算账号编号都不离开浏览器页面。这是非官方方式，网页改版可能失效，默认关闭。
- **本地数据**：设置、账号备注、额度历史保存在 `~/.google-ai-manager/`（目录 0700 / 文件 0600）。
- **不进仓库**：`logs/`、`outputs/`、`downloads/`、`*.db`、`active_hold.json`、`gpu_monitor_config.json`、`.venv-desktop/` 均已加入 `.gitignore`。
- 本地接口只监听 `127.0.0.1`，并拒绝跨站请求。

---

## 开发

```powershell
# 单元测试（离线，不访问网络、不需要凭据）
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests

# 后端引擎测试（模拟 Colab，在 WSL 的 colab 环境里运行）
wsl.exe -d Ubuntu -e bash -lc '$HOME/.local/share/uv/tools/google-colab-cli/bin/python -m unittest discover -s tests'

# 安全实测：在空闲端口启动只读后端（不探测、不开机）
wsl.exe -d Ubuntu -e python3 -u scripts/colab_gpu_monitor.py --port 8766 --no-probe
```

架构说明与开发约定见 [AGENTS.md](./AGENTS.md)。

---

## 版本记录

| 版本 | 主要内容 |
|---|---|
| v2.9 | 设置分页 + 关于页；修复长页面滚动不到底；清理模型下载与根目录 .bat 等遗留脚本 |
| v2.8 | Colab 合并为一个入口；Flow 余额趋势与提醒；Cloud 免费试用与赠金到期提醒；云盘空间构成；浅色主题 |
| v2.7 | 一键 OAuth 添加 / 登录账号；顶部横向导航 |
| v2.6 | 界面改为额度管理面板（总览首页） |
| v2.5 | Google Cloud 赠金（实验性）；去掉已不适用的 Gemini CLI |
| v2.4 | Google Flow 积分（实验性） |
| v2.3 | Colab 会话管理；去掉短剧专用功能，改为通用的「运行命令」 |
| v2.2 | 多账号中心、自动切换、Pro 权益中心、Antigravity 额度、环境自检 |
| v2.0 | Colab GPU 监控、命中即锁卡、超时自动关机、桌面端与托盘 |

## 路线图
- 余额下限自动停机、实例端口转发到本机
- Gemini / AI Studio API Key 管理与用量
- 纯 Windows 运行（不依赖 WSL2）与安装包

---

## 许可与支持

[MIT License](./LICENSE)。觉得有用的话，可以在应用的「设置 → 关于」页面请作者喝杯咖啡。
