#!/usr/bin/env bash
# ==============================================================================
# WSL2 Ubuntu 初始化安装脚本 (root 阶段 + 普通用户阶段)
# 安装内容：
#   1. 创建默认普通用户 ubuntu (免密 sudo + /etc/wsl.conf 默认登录)
#   2. 配置 Windows 浏览器桥接 (/usr/local/bin/wsl-open-browser & xdg-open)
#   3. 配置代理透传保护 (NO_PROXY=localhost,127.0.0.1,::1)
#   4. 安装 uv + Python 3.12 + google-colab-cli + colab-ext (支持 colab dfs ...)
# ==============================================================================
set -euo pipefail

STAGE="${1:-root}"
TARGET_USER="ubuntu"

if [[ "$STAGE" == "root" ]]; then
    echo "=== [1/4] 配置 WSL2 系统级环境 (root) ==="

    # 1. 创建普通用户 ubuntu（若尚未创建）
    if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
        useradd -m -s /bin/bash -G sudo "$TARGET_USER"
        echo "  -> 已创建用户: $TARGET_USER"
    fi
    echo "${TARGET_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${TARGET_USER}"
    chmod 0440 "/etc/sudoers.d/90-${TARGET_USER}"

    # 2. 写入 /etc/wsl.conf 将 ubuntu 设为默认登录用户
    cat > /etc/wsl.conf <<EOF
[user]
default=${TARGET_USER}

[interop]
enabled=true
appendWindowsPath=true
EOF

    # 3. 创建安全的 Windows 默认浏览器打开桥接器（支持含 & 的长 OAuth URL）
    cat > /usr/local/bin/wsl-open-browser <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PS_EXE="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
if [[ -x "$PS_EXE" ]] && [[ $# -ge 1 ]]; then
    "$PS_EXE" -NoProfile -NonInteractive -Command 'Start-Process $args[0]' -- "$1" >/dev/null 2>&1
    exit 0
fi
exit 1
EOF
    chmod 0755 /usr/local/bin/wsl-open-browser
    if [[ ! -e /usr/local/bin/xdg-open ]]; then
        ln -sf /usr/local/bin/wsl-open-browser /usr/local/bin/xdg-open
    fi

    # 4. 配置全局环境变量钩子（确保非交互 shell 与交互 shell 均生效）
    cat > /etc/profile.d/colab-h3.sh <<'EOF'
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export BROWSER="/usr/local/bin/wsl-open-browser"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
EOF
    chmod 0644 /etc/profile.d/colab-h3.sh

    # 确保 curl / ca-certificates / tar 存在
    if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
        apt-get update -y
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl tar git
    fi

    echo "=== [2/4] 切换到用户 ${TARGET_USER} 安装 uv + google-colab-cli + colab-ext ==="
    SCRIPT_REAL="$(readlink -f "$0")"
    exec su - "$TARGET_USER" -c "bash '$SCRIPT_REAL' user"
fi

# ------------------------------ 普通用户阶段 ------------------------------
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export BROWSER="/usr/local/bin/wsl-open-browser"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

# 网络代理：统一读取 ~/.google-ai-manager/settings.json（面板「设置 → 网络代理」可视化配置，默认自动探测）
GAM_NETCFG_ABS="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)/providers/netcfg.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$GAM_NETCFG_ABS" ]]; then
    eval "$(python3 "$GAM_NETCFG_ABS" shell-env)" || true
fi
echo "  -> 网络代理: ${HTTPS_PROXY:-直连}"

# 写入 ~/.bashrc 保证交互终端自动加载 PATH、BROWSER 与代理设置（与面板共用同一份配置）
MARKER="# >>> colab-h3 env >>>"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" <<EOF

# >>> colab-h3 env >>>
export PATH="\$HOME/.local/bin:/usr/local/bin:\$PATH"
export BROWSER="/usr/local/bin/wsl-open-browser"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
if command -v python3 >/dev/null 2>&1 && [[ -f "${GAM_NETCFG_ABS}" ]]; then
    eval "\$(python3 "${GAM_NETCFG_ABS}" shell-env 2>/dev/null)"
fi
# <<< colab-h3 env <<<
EOF
fi

echo "=== 测试 Google / GitHub / PyPI 网络连通性 ==="
curl -sSLo /dev/null --max-time 15 https://colab.research.google.com
echo "  -> Google Colab 连通正常"
curl -sSLo /dev/null --max-time 15 https://pypi.org/simple/
echo "  -> PyPI 连通正常"

echo "=== [3/4] 安装 uv 与 google-colab-cli (Python 3.12) ==="
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

uv tool install --python 3.12 --upgrade google-colab-cli
colab version

echo "=== [4/4] 安装 colab-ext (兼容 colab dfs consent / colab dfs mount) ==="
COLAB_EXT_REPO="deluxebear/colab-dfs-ext" curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh

echo "=== 验证 colab CLI 与 colab-ext 状态 ==="
colab version
colab ext doctor
colab dfs --help >/dev/null
echo "[SUCCESS] WSL2 + colab CLI + colab-ext 环境全部配置完成！"
