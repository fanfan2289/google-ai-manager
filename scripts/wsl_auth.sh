#!/usr/bin/env bash
# First-time Google Colab CLI + Zero-Browser Google DriveFS OAuth Wizard (WSL2)
set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
# 网络代理：统一读取 ~/.google-ai-manager/settings.json（面板「设置 → 网络代理」可视化配置，默认自动探测）
_GAM_NETCFG="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)/providers/netcfg.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$_GAM_NETCFG" ]]; then
    eval "$(python3 "$_GAM_NETCFG" shell-env)" || true
fi
unset _GAM_NETCFG
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure Windows browser bridge is wired for Python `webbrowser.open()` in WSL2
if [[ -x "/usr/local/bin/wsl-open-browser" ]]; then
    export BROWSER="/usr/local/bin/wsl-open-browser"
fi

echo "=================================================================="
echo "  Colab CLI + Google Drive (DriveFS) 首次授权向导 (WSL2)"
echo "=================================================================="
echo ""

if ! command -v colab >/dev/null 2>&1; then
    echo "[ERROR] 未找到 colab 命令，请先完成 WSL 环境初始化。" >&2
    exit 1
fi

echo "[1/3] 检查扩展注入状态 (colab ext doctor)..."
colab ext doctor || true
echo ""

ACTIVE_ACCT="$(colab account current --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("account","default"))' 2>/dev/null || echo default)"
echo "[2/3] 验证 / 登录 Colab 计算账号 (OAuth2, 当前配置: ${ACTIVE_ACCT})..."
echo "  提示：若尚未登录，终端会打印一条 Google 授权链接（并尝试在 Windows 浏览器打开）。"
echo "  登录目标 Google 账号后，将网页显示的授权码粘贴回终端按回车即可。"
echo ""
if colab whoami >/dev/null 2>&1; then
    echo "  -> 当前已登录 Colab 账号："
    colab whoami
    if [[ "${FORCE_REAUTH:-0}" == "1" ]]; then
        colab account login "$ACTIVE_ACCT"
        colab whoami
    else
        read -r -p "  是否切换/重新登录其他 Colab 算力账号？[y/N]: " login_ans
        if [[ "${login_ans:-N}" =~ ^[Yy]$ ]]; then
            colab account login "$ACTIVE_ACCT"
            colab whoami
        fi
    fi
else
    colab account login "$ACTIVE_ACCT"
    echo "  -> Colab 账号登录成功："
    colab whoami
fi
echo ""

echo "[3/3] 授权 Google Drive 零浏览器静默挂载凭据 (colab dfs consent)..."
echo "  ⚠️ 重要提醒：Windows 浏览器弹出授权页面后："
echo "     若页面出现复选框，切记手动勾选「查看、编辑、创建和删除您的所有 Google 云端硬盘文件」，再点继续/允许！"
echo "     点允许后，浏览器会自动回调 localhost 完成保存（无需手动复制粘贴授权码）。"
echo ""

DFS_CRED_FILE="$(colab account current --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dfs_credentials_path",""))' 2>/dev/null || echo "$HOME/.config/colab-cli/accounts/${ACTIVE_ACCT}/dfs_credentials.json")"
if [[ -s "$DFS_CRED_FILE" ]] && [[ "${FORCE_REAUTH:-0}" != "1" ]]; then
    echo "  -> 检测到已存在 DriveFS 凭据 ($DFS_CRED_FILE)："
    colab dfs show
    if ! colab dfs token >/dev/null 2>&1; then
        echo ""
        echo "  ⚠️ 警告：当前已存云盘凭据校验失败（通常是授权页未勾选「查看、编辑、创建和删除您的所有 Google 云端硬盘文件」复选框导致）！"
        echo "  -> 即将自动重新弹出授权页面，请务必勾选该复选框后再点继续！"
        colab dfs consent
    else
        read -r -p "  是否重新授权覆盖？[y/N]: " ans
        if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
            colab dfs consent
        fi
    fi
else
    colab dfs consent
fi

echo ""
echo "  -> 正在校验 Google Drive 读写权限是否真正授予..."
if ! colab dfs token >/dev/null 2>&1; then
    echo ""
    echo "[ERROR] DriveFS 令牌校验失败！" >&2
    echo "  原因：Google 授权页面的「查看、编辑、创建和删除您的所有 Google 云端硬盘文件」复选框未被勾选。" >&2
    echo "  请重新运行本脚本，并在浏览器弹出页中【手动勾选所有复选框】后再点继续！" >&2
    exit 1
fi
echo "  -> ✅ Google Drive 完整读写权限校验通过！"

echo ""
echo "=================================================================="
echo "  授权验证汇总 (CCU 余额与当前实例状态)"
echo "=================================================================="
python3 "$SCRIPT_DIR/colab_meter.py" status --all-accounts
echo ""
echo "[OK] 首次授权已全部完成！之后可在桌面端或命令行直接使用，无需再弹浏览器。"
