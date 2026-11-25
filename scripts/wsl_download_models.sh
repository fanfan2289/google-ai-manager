#!/usr/bin/env bash
# ==============================================================================
# Zero-GPU-Cost MiniMax-H3 Model Downloader (Colab CPU -> 5TB Google Drive)
# (Upgraded with Detached Remote Job + Auto-Reconnect Live Log Tailing)
#
# 1. 先用本机 Drive API 同步 cloud_workstation/ 脚本与东方玄幻工作流到 5TB 云盘 (0 CCU)
# 2. 启动独立后台防幽灵计费看门狗 + bash 全信号 trap
# 3. 拉起极低费率 Colab CPU 实例 (session=h3setup，不占用任何 GPU)
# 4. 在远端脱离 WebSocket 后台运行下载器，本地每 12 秒增量拉取日志（彻底免疫代理断连！）
# 5. 完成后（或任何报错中断）立刻强杀 CPU 实例并输出 CCU 对账单
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

SESSION_NAME="h3setup"
MAX_MINUTES="${COLAB_DL_MAX_MINUTES:-90}"
RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="$LOG_DIR/download_models_${RUN_TS}.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$RUN_LOG"
}

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
    log "🛡️ [防幽灵计费守卫] 下载流程结束 (exit_code=$exit_code)，正在强杀 CPU 实例并核对 CCU..."
    python3 -u "$SCRIPT_DIR/colab_meter.py" stop 2>&1 | tee -a "$RUN_LOG"
    if [[ -n "$WATCHDOG_PID" ]] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
    fi
    log "📄 下载日志已保存至: $RUN_LOG"
    exit "$exit_code"
}

trap cleanup EXIT
trap 'log "收到中断信号 (SIGINT)，立即停止并回收 CPU 实例..."; exit 130' INT
trap 'log "收到终止信号 (SIGTERM)，立即停止并回收 CPU 实例..."; exit 143' TERM
trap 'log "收到挂断信号 (SIGHUP)，立即停止并回收 CPU 实例..."; exit 129' HUP

log "=================================================================="
log "📦 MiniMax-H3 (~62 GB) 零 GPU 成本高速下载到 5TB 云盘 (断连自愈版)"
log "   Log File: $RUN_LOG"
log "=================================================================="

log "[Step 0/3] 同步本地东方玄幻工作流与启动脚本到 Google Drive (0 CCU)..."
python3 -u "$SCRIPT_DIR/sync_workstation_to_drive.py" 2>&1 | tee -a "$RUN_LOG"

log "[Step 1/3] 清理残留实例、记录 CCU 基线并启动看门狗 (TTL=${MAX_MINUTES}m)..."
python3 -u "$SCRIPT_DIR/colab_meter.py" stop 2>&1 | tee -a "$RUN_LOG"
python3 -u "$SCRIPT_DIR/colab_meter.py" start -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG"

nohup python3 -u "$SCRIPT_DIR/colab_meter.py" watch \
    --parent-pid "$$" \
    --max-minutes "$MAX_MINUTES" \
    >/dev/null 2>&1 &
WATCHDOG_PID=$!

log "[Step 2/3] 正在拉起轻量 Colab CPU 实例 (session=${SESSION_NAME})..."
colab new -s "$SESSION_NAME" 2>&1 | tee -a "$RUN_LOG"

DFS_CRED_PATH="$(colab account current --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["dfs_credentials_path"])')"
CREDS_B64="$(base64 -w0 "$DFS_CRED_PATH")"
DL_SCRIPT_B64="$(base64 -w0 "$SCRIPT_DIR/download_models_to_drive.py")"

log "[Step 3/3] 启动远端后台下载任务（支持跳过已传文件 + 代理断连自动重连）..."
colab exec -s "$SESSION_NAME" --timeout 60 <<PYEOF 2>&1 | tee -a "$RUN_LOG"
import base64, os, subprocess, sys
from pathlib import Path

Path("/tmp/download_models_to_drive.py").write_bytes(base64.b64decode("${DL_SCRIPT_B64}"))
Path("/tmp/h3_dl.log").write_text("", encoding="utf-8")
Path("/tmp/h3_dl.rc").unlink(missing_ok=True)

wrapper = r'''
import os, subprocess, sys
from pathlib import Path
env = os.environ.copy()
rc = subprocess.call([sys.executable, "-u", "/tmp/download_models_to_drive.py"], env=env)
Path("/tmp/download_models_to_drive.py").unlink(missing_ok=True)
Path("/tmp/h3_dl.rc").write_text(str(rc), encoding="utf-8")
'''
Path("/tmp/h3_dl_wrap.py").write_text(wrapper, encoding="utf-8")

env = os.environ.copy()
env["DRIVE_CREDS_JSON"] = base64.b64decode("${CREDS_B64}").decode("utf-8")
log_fh = open("/tmp/h3_dl.log", "wb")
p = subprocess.Popen(
    [sys.executable, "-u", "/tmp/h3_dl_wrap.py"],
    env=env,
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
Path("/tmp/h3_dl.pid").write_text(str(p.pid), encoding="utf-8")
print(f"[remote] Detached downloader started (PID={p.pid})", flush=True)
PYEOF

OFFSET=0
CONSECUTIVE_ERRS=0
REMOTE_RC=""

while true; do
    sleep 12
    set +e
    POLL_OUT="$(timeout 65 colab exec -s "$SESSION_NAME" --timeout 45 <<PYEOF 2>&1
import os
from pathlib import Path
log_p = Path("/tmp/h3_dl.log")
rc_p = Path("/tmp/h3_dl.rc")
pid_p = Path("/tmp/h3_dl.pid")
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
        log "⚠️ [reconnect] 轮询波动 ($CONSECUTIVE_ERRS/12)，远端仍在安全下载，5 秒后自动重连..."
        if [[ "$CONSECUTIVE_ERRS" -ge 12 ]]; then
            log "❌ [ERROR] 连续 12 次重连失败，中止并回收 CPU 实例。"
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
        log "❌ [ERROR] 远端下载进程意外退出（未写入退出码）。"
        exit 1
    fi
done

if [[ "$REMOTE_RC" != "0" ]]; then
    log "❌ [ERROR] 模型下载脚本返回异常退出码 ($REMOTE_RC)"
    exit "$REMOTE_RC"
fi

log "✅ 全部 MiniMax-H3 模型权重与东方玄幻短剧工作流已安全存入你的 5TB Google Drive！"
