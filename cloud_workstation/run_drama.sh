#!/usr/bin/env bash
# ==============================================================================
# 示例工作流：MiniMax-H3 短剧流水线（命令行使用，桌面端不依赖它）
#
#   1. 把本地 cloud_workstation/ 剧本、工作流与启动脚本同步到 Google Drive (0 CCU)
#   2. 调用通用运行器 scripts/wsl_run_remote.sh：抢卡 / 复用会话、挂载云盘、在远端运行
#      ComfyUI 启动脚本与 run_drama.py；运行器负责看门狗、退出强杀与 CCU 对账
#   3. 成功后把最新成片拉取到本地 outputs/
#
# 用法: bash cloud_workstation/run_drama.sh [--workflow-dir DIR] [--config FILE] [--workflow FILE]
#                                          [--profile NAME] [运行器参数，如 -g G4 / -a 账号 / --dry-run]
# ==============================================================================
set -Eeuo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
RUNNER="$PROJECT_DIR/scripts/wsl_run_remote.sh"

LAUNCH_SCRIPT="${H3_LAUNCH_SCRIPT:-/content/drive/MyDrive/AI_Workstation/scripts/launch_g4_comfyui_fast.py}"
LAUNCH_PROFILE="${H3_PROFILE:-scheme_a}"
WORKFLOW_DIR="${H3_WORKFLOW_DIR:-/content/drive/MyDrive/AI_Workstation/workflows/02_eastern_fantasy_xianxia}"
DRAMA_SCRIPT="${H3_DRAMA_SCRIPT:-run_drama.py}"
DRAMA_CONFIG="${H3_DRAMA_CONFIG:-drama_config.json}"
DRAMA_WORKFLOW="${H3_DRAMA_WORKFLOW:-workflow_h3_fl2va_turbo.json}"

PASS=()
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) LAUNCH_PROFILE="$2"; shift 2 ;;
        --workflow-dir) WORKFLOW_DIR="$2"; shift 2 ;;
        --config) DRAMA_CONFIG="$2"; shift 2 ;;
        --workflow) DRAMA_WORKFLOW="$2"; shift 2 ;;
        -c|--cmd) echo "[ERROR] run_drama.sh 自己生成远端命令，不接受 -c；自定义命令请直接用 scripts/wsl_run_remote.sh" >&2; exit 2 ;;
        --dry-run) DRY_RUN=1; PASS+=("$1"); shift ;;
        *) PASS+=("$1"); shift ;;
    esac
done

REMOTE_CMD="python3 -u ${LAUNCH_SCRIPT} --profile ${LAUNCH_PROFILE} && cd ${WORKFLOW_DIR} && python3 -u ${DRAMA_SCRIPT} --config ${DRAMA_CONFIG} --workflow ${DRAMA_WORKFLOW}"

if [[ "$DRY_RUN" -ne 1 ]]; then
    echo "[run_drama] 同步本地剧本配置与脚本到 Google Drive (0 CCU)..."
    python3 -u "$HERE/local/sync_workstation_to_drive.py"
fi

set +e
bash "$RUNNER" --mount-drive "${PASS[@]}" -c "$REMOTE_CMD"
RC=$?
set -e

if [[ "$RC" -eq 0 ]] && [[ "$DRY_RUN" -ne 1 ]]; then
    echo "[run_drama] 正在从 Google Drive 拉取最新成品视频到本地 outputs/ 目录 (0 CCU)..."
    python3 -u "$HERE/local/pull_latest_outputs.py" || true
fi
exit "$RC"
