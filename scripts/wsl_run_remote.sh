#!/usr/bin/env bash
# ==============================================================================
# Colab 远端命令运行器 + 双重防幽灵计费 (WSL2)
#
# 功能：
#   1. 记录启动前 CCU 基准余额 + 启动独立后台死人开关看门狗 (防 kill -9 / 终端崩溃 / 超时挂死)
#   2. 注册 bash EXIT/INT/TERM/HUP 全信号兜底 trap，任何报错或中断必强杀实例
#   3. 复用已锁定的会话 (--reuse-session)，或 colab new 拉起指定 GPU 实例
#   4. 可选 (--mount-drive)：colab dfs mount 零浏览器静默挂载 Google Drive (/content/drive)
#   5. 在远端脱离 WebSocket 后台运行 -c 指定的 Shell 命令，自动重连拉日志，严格透传远端退出码
#   6. 跑完（或异常退出）自动强杀全部残留实例、校验 0 活跃会话并输出 CCU 对账单
# ==============================================================================
set -Eeuo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
# 网络代理：统一读取 ~/.google-ai-manager/settings.json（面板「设置 → 网络代理」可视化配置，默认自动探测）
_GAM_NETCFG="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)/providers/netcfg.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$_GAM_NETCFG" ]]; then
    eval "$(python3 "$_GAM_NETCFG" shell-env)" || true
fi
unset _GAM_NETCFG
if [[ -x "/usr/local/bin/wsl-open-browser" ]]; then
    export BROWSER="/usr/local/bin/wsl-open-browser"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

# ------------------------------ 可配置参数 ------------------------------
SESSION_NAME="${COLAB_SESSION:-gpuwork}"
GPU_TYPE="${COLAB_GPU:-G4}"
FALLBACK_GPU="${COLAB_FALLBACK_GPU:-A100}"
GPU_RETRY_MINUTES="${COLAB_GPU_RETRY_MINUTES:-10}"
ACCOUNT_NAME="${COLAB_ACCOUNT:-}"
MAX_MINUTES="${COLAB_MAX_MINUTES:-180}"
KEEP_ALIVE="${COLAB_KEEP_ALIVE:-0}"
REUSE_SESSION="${COLAB_REUSE_SESSION:-0}"
MOUNT_DRIVE="${COLAB_MOUNT_DRIVE:-0}"
REMOTE_SHELL_CMD="${COLAB_REMOTE_CMD:-}"

usage() {
    cat <<EOF
用法: $(basename "$0") [选项]

选项:
  -s, --session NAME       Colab 会话名 (默认: $SESSION_NAME)
  -g, --gpu TYPE           首选 GPU 型号: G4 / A100 / L4 / H100 / T4 (默认: $GPU_TYPE)
  --fallback-gpu TYPE      首选 GPU 抢卡超时后的备选型号，传 none 禁用 (默认: $FALLBACK_GPU)
  --gpu-retry-minutes MIN  首选 GPU 缺货时最多抢卡时长/分钟 (默认: $GPU_RETRY_MINUTES)
  -a, --account NAME       指定 colab-ext 账户配置名
  -c, --cmd "SHELL_CMD"    要在远端执行的完整 Shell 命令（必填，工作目录 /content）
  --mount-drive            运行前挂载 Google Drive 到 /content/drive（默认不挂载）
  --max-minutes MIN        看门狗最大允许运行时长/分钟，超时强杀防跑飞 (默认: $MAX_MINUTES)
  --reuse-session          复用已锁定的现有 Colab 会话（跳过清理与重新抢卡，适合监控占卡接管）
  --keep-alive             跑完不自动关机（仅调试用，⚠️ 会持续消耗 CCU！）
  --dry-run                仅检查本地环境、授权与 CCU 状态，不实际拉起 GPU
  -h, --help               显示帮助
EOF
}

DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--session) SESSION_NAME="$2"; shift 2 ;;
        -g|--gpu) GPU_TYPE="$2"; shift 2 ;;
        --fallback-gpu) FALLBACK_GPU="$2"; shift 2 ;;
        --gpu-retry-minutes) GPU_RETRY_MINUTES="$2"; shift 2 ;;
        -a|--account) ACCOUNT_NAME="$2"; shift 2 ;;
        -c|--cmd) REMOTE_SHELL_CMD="$2"; shift 2 ;;
        --mount-drive) MOUNT_DRIVE=1; shift ;;
        --max-minutes) MAX_MINUTES="$2"; shift 2 ;;
        --reuse-session) REUSE_SESSION=1; shift ;;
        --keep-alive) KEEP_ALIVE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERROR] 未知参数: $1" >&2; usage; exit 2 ;;
    esac
done

ACCT_ARGS=()
if [[ -n "$ACCOUNT_NAME" ]]; then
    ACCT_ARGS=(-a "$ACCOUNT_NAME")
fi

if [[ -z "$REMOTE_SHELL_CMD" ]] && [[ "$DRY_RUN" -ne 1 ]]; then
    echo "[ERROR] 请用 -c \"SHELL_CMD\" 指定要在远端运行的命令" >&2
    usage
    exit 2
fi

RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="$LOG_DIR/run_${SESSION_NAME}_${RUN_TS}.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$RUN_LOG"
}

# ------------------------ 防幽灵计费清理钩子 ------------------------
CLEANUP_DONE=0
WATCHDOG_PID=""

cleanup() {
    local exit_code=$?
    if [[ "$CLEANUP_DONE" -eq 1 ]]; then
        return
    fi
    CLEANUP_DONE=1
    set +e

    echo "" | tee -a "$RUN_LOG"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        return
    fi

    if [[ "$KEEP_ALIVE" -eq 1 ]]; then
        log "⚠️ [WARN] 已启用 --keep-alive，保留会话 '$SESSION_NAME' 不自动关机！调试完毕请务必执行 stop 强杀！"
        # Disarm watchdog explicitly when keep-alive is requested
        mkdir -p "$HOME/.cache/colab-h3"
        date -Iseconds > "$HOME/.cache/colab-h3/watchdog.disarm"
    else
        log "🛡️ [防幽灵计费守卫] 触发退出清理 (exit_code=$exit_code)，正在强杀实例并核对 CCU..."
        python3 "$SCRIPT_DIR/colab_meter.py" stop "${ACCT_ARGS[@]}" 2>&1 | tee -a "$RUN_LOG"
        local stop_rc=${PIPESTATUS[0]}
        if [[ "$stop_rc" -ne 0 ]]; then
            log "🚨 [CRITICAL] colab_meter.py stop 返回异常 ($stop_rc)，尝试使用官方 colab stop -s ${SESSION_NAME} 二次兜底..."
            colab "${ACCT_ARGS[@]}" stop -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG" || true
        fi
    fi

    if [[ -n "$WATCHDOG_PID" ]] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
    fi
    log "📄 完整运行日志已保存至: $RUN_LOG"
    exit "$exit_code"
}

trap cleanup EXIT
trap 'log "收到中断信号 (SIGINT/Ctrl+C)，立即中止并回收 GPU..."; exit 130' INT
trap 'log "收到终止信号 (SIGTERM)，立即中止并回收 GPU..."; exit 143' TERM
trap 'log "收到终端挂断信号 (SIGHUP)，立即中止并回收 GPU..."; exit 129' HUP

# ------------------------ 1. 预检环境与授权 ------------------------
log "=================================================================="
log "🚀 Colab ${GPU_TYPE} 远端命令运行器启动"
log "   Session     : $SESSION_NAME"
log "   GPU         : $GPU_TYPE"
log "   Watchdog TTL: ${MAX_MINUTES} 分钟"
log "   Mount Drive : $([[ "$MOUNT_DRIVE" -eq 1 ]] && echo yes || echo no)"
log "   Log File    : $RUN_LOG"
log "=================================================================="

if ! command -v colab >/dev/null 2>&1; then
    log "[ERROR] 未找到 colab 命令，请先初始化 WSL2 环境。"
    exit 1
fi

# 检查 Colab OAuth 与 DriveFS 凭据是否就绪（自动适配多账户与跨账户共享 Drive）
if ! colab "${ACCT_ARGS[@]}" whoami >/dev/null 2>&1; then
    log "[ERROR] Colab 账号尚未登录！请先双击运行 1_首次授权_Auth.bat 或执行 bash scripts/wsl_auth.sh"
    exit 1
fi

if [[ "$MOUNT_DRIVE" -eq 1 ]]; then
DFS_CRED="$(colab "${ACCT_ARGS[@]}" account current --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dfs_credentials_path",""))' 2>/dev/null || true)"
if [[ -z "$DFS_CRED" ]] || [[ ! -s "$DFS_CRED" ]]; then
    log "[ERROR] 未检测到零浏览器 DriveFS 授权凭据 (${DFS_CRED:-unknown})！"
    log "        请先双击运行 1_首次授权_Auth.bat 或执行: colab dfs consent"
    exit 1
fi
if ! colab "${ACCT_ARGS[@]}" dfs token >/dev/null 2>&1; then
    log "[ERROR] DriveFS 云盘凭据校验失败（通常是授权时未勾选「查看、编辑、创建和删除您的所有 Google 云端硬盘文件」复选框）！"
    log "        为避免白扣 CCU，已提前中止。请在「账号管理」重新授权云盘并务必勾选云盘复选框！"
    exit 1
fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[DRY-RUN] 预检通过！正在查询当前账号状态与 CCU 余额..."
    python3 "$SCRIPT_DIR/colab_meter.py" status "${ACCT_ARGS[@]}" 2>&1 | tee -a "$RUN_LOG"
    log "[DRY-RUN] 将要执行的远端命令: $REMOTE_SHELL_CMD"
    exit 0
fi

# 启动前先确保没有残留幽灵实例，并记录 CCU 对账基线
if [[ "$REUSE_SESSION" -eq 1 ]]; then
    log "[Step 0/3] ♻️ 复用已锁定会话模式 (--reuse-session)，跳过清理历史实例，仅记录 CCU 基准..."
    python3 "$SCRIPT_DIR/colab_meter.py" start "${ACCT_ARGS[@]}" -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG"
else
    log "[Step 0/3] 清理历史残留实例 & 记录启动前 CCU 基准..."
    python3 "$SCRIPT_DIR/colab_meter.py" stop "${ACCT_ARGS[@]}" 2>&1 | tee -a "$RUN_LOG"
    python3 "$SCRIPT_DIR/colab_meter.py" start "${ACCT_ARGS[@]}" -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG"
fi

# 启动独立后台看门狗（即使当前 bash 被 kill -9 强杀，看门狗仍会在 8 秒内自动强杀云端 GPU！）
nohup python3 "$SCRIPT_DIR/colab_meter.py" watch \
    "${ACCT_ARGS[@]}" \
    --parent-pid "$$" \
    --max-minutes "$MAX_MINUTES" \
    >/dev/null 2>&1 &
WATCHDOG_PID=$!
log "🐕 后台防幽灵计费看门狗已就绪 (PID=$WATCHDOG_PID, 最大保护时长=${MAX_MINUTES}m)"

# ------------------------ 2. 智能抢卡并静默挂云盘 ------------------------
ALLOCATED_GPU=""
if [[ "$REUSE_SESSION" -eq 1 ]]; then
    log "[Step 1/3] ♻️ 正在复核已锁定会话 '${SESSION_NAME}' 是否在线..."
    for _r in 1 2 3; do
        if timeout 35 colab "${ACCT_ARGS[@]}" exec -s "$SESSION_NAME" --timeout 20 <<<'print("REUSE_OK")' 2>&1 | grep -q "REUSE_OK"; then
            ALLOCATED_GPU="$GPU_TYPE"
            log "✅ 已成功接管锁定中的 ${ALLOCATED_GPU} 会话 '${SESSION_NAME}' (第 ${_r} 次尝试)！"
            break
        fi
        log "⚠️ 复核锁定会话 '${SESSION_NAME}' WebSocket 抖动 (${_r}/3)，3 秒后重试..."
        sleep 3
    done
    if [[ -z "$ALLOCATED_GPU" ]]; then
        log "⚠️ 连续 3 次未能连接到既有会话 '${SESSION_NAME}'，将回退至重新抢卡流程..."
    fi
fi

if [[ -z "$ALLOCATED_GPU" ]]; then
    log "[Step 1/3] 正在申请首选 GPU=${GPU_TYPE} 实例 (缺货最多抢卡 ${GPU_RETRY_MINUTES} 分钟，备选=${FALLBACK_GPU})..."

    RETRY_DEADLINE=$(( $(date +%s) + GPU_RETRY_MINUTES * 60 ))
    ATTEMPT=0

    while true; do
        ATTEMPT=$((ATTEMPT + 1))
        set +e
        COLAB_CLI_ASSIGN_RETRIES=0 COLAB_CLI_RETRY_BACKOFF=5 \
            colab "${ACCT_ARGS[@]}" new -s "$SESSION_NAME" --gpu "$GPU_TYPE" 2>&1 | tee -a "$RUN_LOG"
        NEW_RC=${PIPESTATUS[0]}
        set -e

        if [[ "$NEW_RC" -eq 0 ]]; then
            ALLOCATED_GPU="$GPU_TYPE"
            log "✅ 成功抢到首选 ${ALLOCATED_GPU} 实例 (第 ${ATTEMPT} 次尝试)！"
            break
        fi

        NOW_TS=$(date +%s)
        if [[ "$NOW_TS" -ge "$RETRY_DEADLINE" ]]; then
            log "⏰ 首选 ${GPU_TYPE} 已连续抢卡 ${GPU_RETRY_MINUTES} 分钟仍无空闲节点。"
            break
        fi

        LEFT_SEC=$((RETRY_DEADLINE - NOW_TS))
        WAIT_SEC=30
        if [[ "$LEFT_SEC" -lt "$WAIT_SEC" ]]; then
            WAIT_SEC="$LEFT_SEC"
        fi
        log "⏳ ${GPU_TYPE} 暂时满载 (第 ${ATTEMPT} 次尝试，0 扣费)，剩余抢卡时间约 $((LEFT_SEC / 60))m$((LEFT_SEC % 60))s，${WAIT_SEC} 秒后继续抢卡..."
        sleep "$WAIT_SEC"
    done
fi

if [[ -z "$ALLOCATED_GPU" ]]; then
    if [[ -n "$FALLBACK_GPU" ]] && [[ "${FALLBACK_GPU,,}" != "none" ]] && [[ "${FALLBACK_GPU^^}" != "${GPU_TYPE^^}" ]]; then
        log "🔄 自动切换至备选 GPU: ${FALLBACK_GPU} ..."
        colab "${ACCT_ARGS[@]}" new -s "$SESSION_NAME" --gpu "$FALLBACK_GPU" 2>&1 | tee -a "$RUN_LOG"
        ALLOCATED_GPU="$FALLBACK_GPU"
        log "✅ 成功拉起备选 ${ALLOCATED_GPU} 实例！"
    else
        log "❌ 未能申请到 ${GPU_TYPE} 且未配置备选 GPU，本次退出（0 扣费）。"
        exit 1
    fi
fi

if [[ "$MOUNT_DRIVE" -eq 1 ]]; then
log "[Step 2/3] 检查并挂载 Google Drive -> /content/drive (当前硬件: ${ALLOCATED_GPU})..."
DRIVE_ALREADY_OK=0
if [[ "$REUSE_SESSION" -eq 1 ]]; then
    for _d in 1 2; do
        if timeout 35 colab "${ACCT_ARGS[@]}" exec -s "$SESSION_NAME" --timeout 20 <<<'import os; print("DRIVE_READY" if os.path.isdir("/content/drive/MyDrive") else "NO_DRIVE")' 2>&1 | grep -q "DRIVE_READY"; then
            DRIVE_ALREADY_OK=1
            break
        fi
        sleep 2
    done
fi
if [[ "$DRIVE_ALREADY_OK" -eq 1 ]]; then
    log "✅ Google Drive 已挂载 (/content/drive/MyDrive)，跳过重复挂载"
else
    timeout 120 colab "${ACCT_ARGS[@]}" dfs mount -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG"
fi
else
    log "[Step 2/3] 未要求挂载 Google Drive（需要时加 --mount-drive），跳过"
fi

# ------------------------ 3. 断连自愈流式执行远端命令 ------------------------
log "[Step 3/3] 开始执行远端命令（脱离 WebSocket 后台运行 + 自动重连拉日志）..."
log "   远端命令: $REMOTE_SHELL_CMD"

REMOTE_B64="$(printf '%s' "$REMOTE_SHELL_CMD" | base64 -w0)"

LAUNCH_OK=0
for _l in 1 2 3; do
    set +e
    timeout 60 colab "${ACCT_ARGS[@]}" exec -s "$SESSION_NAME" --timeout 40 <<PYEOF 2>&1 | tee -a "$RUN_LOG"
import base64, os, subprocess, sys
from pathlib import Path

cmd = base64.b64decode("${REMOTE_B64}").decode("utf-8")
Path("/tmp/gam_job.sh").write_text("#!/usr/bin/env bash\nset -e\ncd /content\n" + cmd + "\n", encoding="utf-8")
Path("/tmp/gam_job.log").write_text("", encoding="utf-8")
Path("/tmp/gam_job.rc").unlink(missing_ok=True)

wrapper = r'''
import os, subprocess
from pathlib import Path
print(f"[remote] Node={os.uname().nodename}", flush=True)
subprocess.run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true", shell=True)
rc = subprocess.call(["bash", "/tmp/gam_job.sh"])
Path("/tmp/gam_job.rc").write_text(str(rc), encoding="utf-8")
'''
Path("/tmp/gam_job_wrap.py").write_text(wrapper, encoding="utf-8")

log_fh = open("/tmp/gam_job.log", "wb")
p = subprocess.Popen(
    [sys.executable, "-u", "/tmp/gam_job_wrap.py"],
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
Path("/tmp/gam_job.pid").write_text(str(p.pid), encoding="utf-8")
print(f"[remote] Detached remote job started (PID={p.pid})", flush=True)
PYEOF
    L_RC=${PIPESTATUS[0]}
    set -e
    if [[ "$L_RC" -eq 0 ]]; then
        LAUNCH_OK=1
        break
    fi
    log "⚠️ 启动远端命令 WebSocket 抖动 (${_l}/3, rc=${L_RC})，4 秒后重试..."
    sleep 4
done
if [[ "$LAUNCH_OK" -ne 1 ]]; then
    log "❌ 连续 3 次启动远端命令失败，立即中止并回收 GPU。"
    exit 1
fi

OFFSET=0
CONSECUTIVE_ERRS=0
REMOTE_RC=""

while true; do
    sleep 10
    set +e
    POLL_OUT="$(timeout 65 colab "${ACCT_ARGS[@]}" exec -s "$SESSION_NAME" --timeout 45 <<PYEOF 2>&1
import os
from pathlib import Path
log_p = Path("/tmp/gam_job.log")
rc_p = Path("/tmp/gam_job.rc")
pid_p = Path("/tmp/gam_job.pid")
offset = ${OFFSET}
new_offset = offset
chunk = ""
if log_p.is_file():
    size = log_p.stat().st_size
    if size > offset:
        with open(log_p, "rb") as f:
            f.seek(offset)
            data = f.read(min(size - offset, 131072))
        new_offset = offset + len(data)
        chunk = data.decode("utf-8", errors="replace")
state = "RUNNING"
rc_val = ""
if rc_p.is_file():
    state = "DONE"
    rc_val = rc_p.read_text().strip()
elif pid_p.is_file():
    try:
        os.kill(int(pid_p.read_text().strip()), 0)
    except Exception:
        state = "DEAD"
print(f"__META__:{state}:{new_offset}:{rc_val}")
if chunk:
    print(chunk, end="")
PYEOF
)"
    POLL_RC=$?
    set -e

    if [[ "$POLL_RC" -ne 0 ]] || ! grep -q "^__META__:" <<<"$POLL_OUT"; then
        CONSECUTIVE_ERRS=$((CONSECUTIVE_ERRS + 1))
        log "⚠️ [reconnect] WebSocket 临时抖动 ($CONSECUTIVE_ERRS/12)，远端任务仍在运行，5 秒后自动重连..."
        if [[ "$CONSECUTIVE_ERRS" -ge 12 ]]; then
            log "❌ [ERROR] 连续 12 次重连失败，为防幽灵计费立即强杀实例！"
            exit 1
        fi
        sleep 5
        continue
    fi

    CONSECUTIVE_ERRS=0
    META_LINE="$(grep "^__META__:" <<<"$POLL_OUT" | head -n1)"
    STATE="$(cut -d: -f2 <<<"$META_LINE")"
    NEW_OFFSET="$(cut -d: -f3 <<<"$META_LINE")"
    RC_VAL="$(cut -d: -f4 <<<"$META_LINE")"
    BODY="$(grep -v "^__META__:" <<<"$POLL_OUT" || true)"

    if [[ -n "$NEW_OFFSET" ]] && [[ "$NEW_OFFSET" =~ ^[0-9]+$ ]]; then
        OFFSET="$NEW_OFFSET"
    fi
    if [[ -n "$BODY" ]]; then
        printf '%s\n' "$BODY" | tee -a "$RUN_LOG"
    fi

    if [[ "$STATE" == "DONE" ]]; then
        REMOTE_RC="${RC_VAL:-1}"
        break
    elif [[ "$STATE" == "DEAD" ]]; then
        log "❌ [ERROR] 远端进程意外中止（未写入退出码），立即回收 GPU。"
        exit 1
    fi
done

if [[ "$REMOTE_RC" != "0" ]]; then
    log "❌ [ERROR] 远端命令返回非零退出码 (exit_code=$REMOTE_RC)！"
    exit "$REMOTE_RC"
fi

log "✅ 远端命令顺利跑完！即将自动强杀实例并核对 CCU..."
