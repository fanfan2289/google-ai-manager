@echo off
chcp 65001 >nul
if /I "%~1"=="--web" (
    shift
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-ColabH3.ps1" monitor --open %*
    exit /b %ERRORLEVEL%
)
start "" "%~dp0.venv-desktop\Scripts\pythonw.exe" "%~dp0desktop_app\colab_gpu_tools.py"
