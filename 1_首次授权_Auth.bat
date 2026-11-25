@echo off
chcp 65001 >nul
title Colab H3 - 首次 Google 授权 (WSL2)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-ColabH3.ps1" auth %*
echo.
pause
