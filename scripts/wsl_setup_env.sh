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

# 若 Windows 开启了本地 Clash 代理但环境变量未注入（例如 su - 清理了环境），自动探测本机常用端口
if [[ -z "${HTTPS_PROXY:-}${https_proxy:-}" ]]; then
    for port in 7897 7890 10809; do
        if (echo >/dev/tcp/127.0.0.1/$port) >/dev/null 2>&1; then
            export HTTP_PROXY="http://127.0.0.1:${port}"
            export HTTPS_PROXY="http://127.0.0.1:${port}"
            export http_proxy="$HTTP_PROXY"
            export https_proxy="$HTTPS_PROXY"
            echo "  -> 自动探测并启用本地代理: $HTTP_PROXY"
            break
        fi
    done
fi

# 写入 ~/.bashrc 保证交互终端自动加载 PATH、BROWSER 与智能代理兜底
MARKER="# >>> colab-h3 env >>>"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" <<'EOF'

# >>> colab-h3 env >>>
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export BROWSER="/usr/local/bin/wsl-open-browser"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
if [[ -z "${HTTPS_PROXY:-}${https_proxy:-}" ]]; then
    for _p in 7897 7890 10809; do
        if (echo >/dev/tcp/127.0.0.1/$_p) >/dev/null 2>&1; then
            export HTTP_PROXY="http://127.0.0.1:${_p}"
            export HTTPS_PROXY="http://127.0.0.1:${_p}"
            export http_proxy="$HTTP_PROXY"
            export https_proxy="$HTTPS_PROXY"
            break
        fi
    done
    unset _p
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
