@echo off
rem Google AI Manager desktop app (add --web to open the dashboard in the browser instead)
chcp 65001 >nul
set "ROOT=%~dp0.."
if /I "%~1"=="--web" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\Invoke-GAM.ps1" monitor --open
    exit /b %ERRORLEVEL%
)
start "" "%ROOT%\.venv-desktop\Scripts\pythonw.exe" "%ROOT%\desktop_app\colab_gpu_tools.py"
