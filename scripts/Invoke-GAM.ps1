param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("auth", "download", "run", "drama", "stop", "status", "dry-run", "monitor")]
    [string]$Mode,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PassThruArgs
)

$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PosixWinPath = $ProjectRoot -replace '\\', '/'
$WslProjectRoot = (wsl.exe -d Ubuntu -e wslpath -u "$PosixWinPath").Trim()

if (-not $WslProjectRoot) {
    Write-Error "无法通过 wslpath 解析项目路径，请确认 WSL2 Ubuntu 已正常启动。"
    exit 1
}

switch ($Mode) {
    "auth" {
        wsl.exe -d Ubuntu -e bash "$WslProjectRoot/scripts/wsl_auth.sh" @PassThruArgs
        exit $LASTEXITCODE
    }
    "download" {
        try {
            wsl.exe -d Ubuntu -e bash "$WslProjectRoot/cloud_workstation/local/wsl_download_models.sh" @PassThruArgs
            exit $LASTEXITCODE
        }
        finally {
            wsl.exe -d Ubuntu -e python3 "$WslProjectRoot/scripts/colab_meter.py" stop --all
        }
    }
    "status" {
        wsl.exe -d Ubuntu -e python3 "$WslProjectRoot/scripts/colab_meter.py" status --all-accounts @PassThruArgs
        exit $LASTEXITCODE
    }
    "stop" {
        wsl.exe -d Ubuntu -e python3 "$WslProjectRoot/scripts/colab_meter.py" stop --all --all-accounts @PassThruArgs
        exit $LASTEXITCODE
    }
    "dry-run" {
        wsl.exe -d Ubuntu -e bash "$WslProjectRoot/scripts/wsl_run_remote.sh" --dry-run @PassThruArgs
        exit $LASTEXITCODE
    }
    "monitor" {
        wsl.exe -d Ubuntu -e python3 "$WslProjectRoot/scripts/colab_gpu_monitor.py" @PassThruArgs
        exit $LASTEXITCODE
    }
    { $_ -in "run", "drama" } {
        # run   = 通用远端命令运行器（需 -c "SHELL_CMD"）
        # drama = 示例短剧流水线（cloud_workstation/run_drama.sh）
        $runScript = if ($Mode -eq "drama") { "$WslProjectRoot/cloud_workstation/run_drama.sh" } else { "$WslProjectRoot/scripts/wsl_run_remote.sh" }
        $exitCode = 1
        try {
            wsl.exe -d Ubuntu -e bash "$runScript" @PassThruArgs
            $exitCode = $LASTEXITCODE
        }
        finally {
            # Windows 侧二次兜底（防止 Ctrl+C 强退 wsl.exe 时漏掉清理）
            if ($PassThruArgs -notcontains "--keep-alive" -and $PassThruArgs -notcontains "--dry-run") {
                Write-Host "`n[Windows 兜底复核] 正在确认云端无任何残留 GPU 实例..." -ForegroundColor Cyan
                wsl.exe -d Ubuntu -e python3 "$WslProjectRoot/scripts/colab_meter.py" stop --all
            }
        }
        exit $exitCode
    }
}
