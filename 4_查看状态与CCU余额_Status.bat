@echo off
chcp 65001 >nul
title Colab 查看活跃会话与 CCU 余额
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-ColabH3.ps1" status %*
echo.
pause
